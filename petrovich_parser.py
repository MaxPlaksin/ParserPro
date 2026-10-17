#!/usr/bin/env python3
"""
Парсер каталога petrovich.ru (API catalog v5 через сессию браузера).

Данные:
  data/products.db — таблицы catalog_sections (группы/подгруппы), categories, products
  output/petrovich/{раздел}/products.xlsx
  storage/images/petrovich/{code}/001.jpg

Примеры:
  python petrovich_parser.py --section 12101 --limit 3
  python petrovich_parser.py --section 12101 --max-sections 2 --limit 5
  python petrovich_parser.py --section 12101
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import aiohttp

from common import (
    CloakSession,
    DomainRateLimiter,
    atomic_write_json,
    category_dir,
    download_images,
    export_products,
    rate_limit_delay,
    setup_logging,
)
from db.hooks import persist_petrovich_row
from db.repository import get_repository

SITE_URL = "https://petrovich.ru"
API_HOST = "https://api.petrovich.ru"
DEFAULT_SECTION = 12101
DEFAULT_OUTPUT = "output/petrovich"
PAGE_SIZE = 40
REQUEST_DELAY = 3.0
SECTION_DELAY = 8.0
SAVE_EVERY_N = 5

setup_logging()
logger = logging.getLogger("petrovich_scraper")

FETCH_API_JS = """
async (path) => {
  const base = window.pet4Data?.apiHost || 'https://api.petrovich.ru';
  const city = window.pet4Data?.city?.code || 'spb';
  let url = path.startsWith('http') ? path : `${base}${path}`;
  const sep = url.includes('?') ? '&' : '?';
  url = `${url}${sep}city_code=${city}&client_id=pet_site&pet_case=camel`;
  const response = await fetch(url, { credentials: 'include' });
  let json = null;
  try { json = await response.json(); } catch (e) {}
  return { status: response.status, json };
}
"""

TAG_RE = re.compile(r"<[^>]+>")


def pick(obj: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return default


def abs_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return urljoin(SITE_URL, url)
    return url


def strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = TAG_RE.sub(" ", unescape(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def section_kind(section_code: int | str, root_code: int | str, depth: int) -> str:
    if str(section_code) == str(root_code):
        return "group"
    if depth <= 1:
        return "subgroup"
    return "subgroup"


@dataclass
class SectionTarget:
    code: int
    title: str
    path: str
    url: str
    product_qty: int


class PaginationExhausted(Exception):
    """API вернул 416: offset за пределами списка товаров."""


class PetrovichApiClient:
    def __init__(self, page, *, limiter: DomainRateLimiter):
        self._page = page
        self._limiter = limiter

    async def fetch(self, path: str) -> dict[str, Any]:
        await self._limiter.wait()
        payload = await self._page.evaluate(FETCH_API_JS, path)
        status = int(payload.get("status") or 0)
        body = payload.get("json") or {}
        if status == 416:
            raise PaginationExhausted(path)
        if status != 200:
            raise RuntimeError(f"API HTTP {status} for {path}: {body}")
        state = pick(body, "state", default={}) or {}
        code = pick(state, "code", default=0)
        if code not in (0, 200, 20001):
            raise RuntimeError(f"API state {code} for {path}: {state}")
        return body.get("data") or {}

    async def get_section(self, section_code: int | str) -> dict[str, Any]:
        data = await self.fetch(f"/catalog/v5/sections/{section_code}")
        section = data.get("section") or data
        return section

    async def iter_products(self, section_code: int | str, *, page_size: int = PAGE_SIZE):
        offset = 0
        path = f"/catalog/{section_code}/"
        while True:
            path_q = quote(path, safe="")
            q = (
                f"/catalog/v5/sections/{section_code}/products"
                f"?limit={page_size}&offset={offset}&sort=popularity_desc&path={path_q}"
            )
            try:
                data = await self.fetch(q)
            except PaginationExhausted:
                break
            products = data.get("products") or []
            if not products:
                break
            yield products
            if len(products) < page_size:
                break
            offset += page_size

    async def get_product(self, product_code: int | str) -> dict[str, Any]:
        data = await self.fetch(f"/catalog/v5/products/{product_code}")
        return data.get("product") or data


def flatten_properties(properties: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(properties, list):
        return out
    for prop in properties:
        title = str(pick(prop, "title", default="")).strip()
        if not title:
            continue
        values = pick(prop, "value", default=[]) or []
        parts: list[str] = []
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    val = str(pick(item, "title", default="")).strip()
                    link = abs_url(str(pick(item, "link", default="") or ""))
                    if val and link:
                        parts.append(f"{val} ({link})")
                    elif val:
                        parts.append(val)
                elif item not in (None, ""):
                    parts.append(str(item))
        elif values not in (None, ""):
            parts.append(str(values))
        if parts:
            out[title] = ", ".join(parts)
    return out


def collect_resource_links(product: dict[str, Any]) -> dict[str, Any]:
    links: dict[str, Any] = {
        "product_page": abs_url(str(pick(product, "link", default="") or "")),
        "misc": {},
    }
    for key in ("links",):
        raw = pick(product, key, default={})
        if isinstance(raw, dict):
            for k, v in raw.items():
                if v:
                    links["misc"][k] = abs_url(str(v))
    fast_links = []
    for item in pick(product, "fastLinks", "fast_links", default=[]) or []:
        if not isinstance(item, dict):
            continue
        fast_links.append(
            {
                "title": pick(item, "title", default=""),
                "type": pick(item, "type", default=""),
                "url": abs_url(str(pick(item, "url", default="") or "")),
                "description": pick(item, "description", default=""),
            }
        )
    if fast_links:
        links["fast_links"] = fast_links

    certs = []
    for cert in pick(product, "certificates", default=[]) or []:
        if not isinstance(cert, dict):
            continue
        files = []
        for f in pick(cert, "files", default=[]) or []:
            if isinstance(f, dict) and f.get("url"):
                files.append({"url": abs_url(str(f["url"])), "ext": f.get("ext")})
        certs.append({"type": pick(cert, "type", default=""), "files": files})
    if certs:
        links["certificates"] = certs

    detailed = pick(product, "certificatesDetailed", "certificates_detailed", default=[])
    if detailed:
        links["certificates_detailed"] = detailed

    for key in ("instructions", "instructionsAdditional", "instructions_additional"):
        val = pick(product, key, default=None)
        if val:
            links[key] = val

    videos = pick(product, "videos", default=None)
    if videos:
        links["videos"] = videos

    promos = pick(product, "promos", default=[]) or []
    promo_links = []
    for promo in promos:
        if not isinstance(promo, dict):
            continue
        promo_links.append(
            {
                "title": pick(promo, "title", default=""),
                "description": pick(promo, "description", default=""),
                "url": abs_url(str(pick(promo, "descLink", "desc_link", default="") or "")),
            }
        )
    if promo_links:
        links["promos"] = promo_links

    images = [abs_url(u) for u in (pick(product, "images", default=[]) or []) if u]
    if images:
        links["images"] = images

    return links


def extract_prices(product: dict[str, Any]) -> tuple[float | None, float | None]:
    price = pick(product, "price", default={}) or {}
    price_alt = pick(product, "priceAlt", "price_alt", default={}) or {}
    show_alt = bool(pick(product, "isShowAltUnitFirst", "is_show_alt_unit_first", default=False))
    main = price_alt if show_alt else price
    retail = pick(main, "retail", default=None)
    prev = pick(main, "previousPriceRetail", "previous_price_retail", default=None)
    return (float(retail) if retail is not None else None, float(prev) if prev is not None else None)


def breadcrumbs_path(product: dict[str, Any]) -> str:
    crumbs = pick(product, "breadcrumbs", default=[]) or []
    titles = [str(pick(c, "title", default="")).strip() for c in crumbs if isinstance(c, dict)]
    titles = [t for t in titles if t]
    return " > ".join(titles)


def product_to_row(
    product: dict[str, Any],
    *,
    list_section: SectionTarget,
    root_section_code: int,
) -> dict[str, Any]:
    code = pick(product, "code", default="")
    product_id = str(code)
    section = pick(product, "section", default={}) or {}
    section_code = pick(section, "code", default=list_section.code)
    path = breadcrumbs_path(product) or list_section.path
    chars = flatten_properties(pick(product, "properties", default=[]))
    brand = chars.get("Бренд", "")
    price, old_price = extract_prices(product)
    desc_html = str(pick(product, "description", default="") or "")
    ext_html = str(pick(product, "extendedDescription", "extended_description", default="") or "")
    desc_plain_obj = pick(product, "descriptionNoHtml", "description_no_html", default={}) or {}
    desc_short = ""
    if isinstance(desc_plain_obj, dict):
        desc_short = str(desc_plain_obj.get("description") or "")

    resource_links = collect_resource_links(product)
    product_url = resource_links.get("product_page") or urljoin(SITE_URL, f"/product/{code}/")
    images = resource_links.get("images") or []

    group_code = root_section_code
    subgroup_code = section_code
    crumbs = pick(product, "breadcrumbs", default=[]) or []
    if len(crumbs) >= 2 and isinstance(crumbs[1], dict):
        subgroup_code = pick(crumbs[1], "code", default=section_code)

    row: dict[str, Any] = {
        "product_id": product_id,
        "code": code,
        "nomenclature_code": product_id,
        "name": pick(product, "title", default=""),
        "url": product_url,
        "product_url": product_url,
        "sku": product_id,
        "brand": brand,
        "sale_price": price,
        "price": price,
        "old_price": old_price,
        "in_stock": not bool(pick(product, "isNotInStock", "is_not_in_stock", default=False)),
        "rating": pick(product, "rating", default=None),
        "reviews_count": pick(product, "reviewCount", "review_count", default=None),
        "description_short": desc_short,
        "description_detailed": strip_html(desc_html),
        "description_detailed_html": desc_html,
        "extended_description": strip_html(ext_html),
        "raw_full_text": strip_html(desc_html + "\n" + ext_html),
        "category_id": str(section_code),
        "category_name": pick(section, "title", default=list_section.title),
        "category_path": path,
        "section_code": section_code,
        "group_section_code": group_code,
        "subgroup_section_code": subgroup_code,
        "product_guid": pick(product, "productGuid", "product_guid", default=""),
        "barcode": pick(product, "barcode", default=""),
        "external_id": pick(product, "externalId", "external_id", default=""),
        "photo_urls": images,
        "photos": "|".join(images),
        "resource_links": resource_links,
        "certificates": resource_links.get("certificates"),
        "certificates_detailed": resource_links.get("certificates_detailed"),
        "instructions": resource_links.get("instructions"),
        "instructions_additional": resource_links.get("instructions_additional"),
        "fast_links": resource_links.get("fast_links"),
        "links": resource_links.get("misc"),
        "videos": resource_links.get("videos"),
        "characteristics": chars,
    }
    for k, v in chars.items():
        row[f"spec__{k}"] = v
    return row


async def collect_section_targets(
    client: PetrovichApiClient,
    root_code: int,
    *,
    max_sections: int | None,
) -> list[SectionTarget]:
    targets: list[SectionTarget] = []
    seen: set[int] = set()
    repo = get_repository()

    async def walk(code: int, parent_code: int | None, depth: int, titles: list[str]) -> None:
        if code in seen:
            return
        seen.add(code)

        section = await client.get_section(code)
        title = str(pick(section, "title", default=code))
        path = " > ".join(titles + [title]) if titles else title
        url = urljoin(SITE_URL, f"/catalog/{code}/")
        qty = int(pick(section, "productQty", "product_qty", default=0) or 0)
        kind = section_kind(code, root_code, depth)

        logger.info("Раздел %s | %s | товаров %s | глубина %s", code, title, qty, depth)
        repo.upsert_catalog_section(
            source="petrovich",
            section_code=str(code),
            parent_section_code=str(parent_code) if parent_code is not None else None,
            name=title,
            path=path,
            url=url,
            kind=kind,
            depth=depth,
            product_qty=qty,
        )

        children = pick(section, "children", default=[]) or []
        # Товары лежат в конечных разделах; родительская выдача их дублирует.
        if qty > 0 and not children:
            targets.append(SectionTarget(code=code, title=title, path=path, url=url, product_qty=qty))

        for child in children:
            child_code = int(pick(child, "code", default=0) or 0)
            if not child_code:
                continue
            await walk(child_code, code, depth + 1, titles + [title])

    await walk(root_code, None, 0, [])
    if max_sections is not None:
        return targets[: max(1, max_sections)]
    return targets


async def scrape_section_products(
    client: PetrovichApiClient,
    session_http: aiohttp.ClientSession,
    target: SectionTarget,
    *,
    root_code: int,
    limit: int | None,
    download_images: bool,
    output_root: Path,
    save_db: bool,
    seen_ids: set[str] | None = None,
    skip_existing: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if seen_ids is None:
        seen_ids = set()
    folder = category_dir(output_root, target.path, str(target.code))

    count = 0
    async for batch in client.iter_products(target.code):
        for stub in batch:
            code = pick(stub, "code", default=None)
            if code is None:
                continue
            pid = str(code)
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            if skip_existing and get_repository().product_exists("petrovich", pid):
                logger.info("Пропуск уже сохранённого %s", pid)
                continue

            product = await client.get_product(code)
            row = product_to_row(product, list_section=target, root_section_code=root_code)

            if download_images and row.get("photo_urls"):
                img_dir = folder / "images" / pid
                saved = await download_images(
                    session_http,
                    row["photo_urls"],
                    img_dir,
                    referer=row["url"],
                )
                row["local_images"] = "|".join(saved)

            rows.append(row)
            count += 1

            if save_db:
                persist_petrovich_row(row, category_dir=folder)

            if count % SAVE_EVERY_N == 0:
                export_products(rows, folder / "products.xlsx")
                atomic_write_json(folder / "checkpoint.json", {"section": target.code, "count": count})

            if limit is not None and count >= limit:
                return rows

    if rows:
        export_products(rows, folder / "products.xlsx")
    return rows


async def run(args: argparse.Namespace) -> int:
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    profile_dir = str(output_root / "browser_profile")
    limiter = DomainRateLimiter(args.delay)

    all_rows: list[dict] = []
    async with CloakSession(headless=args.headless, profile_dir=profile_dir) as cloak:
        page = await cloak.new_page()
        await page.goto(SITE_URL + "/", wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(3000)

        client = PetrovichApiClient(page, limiter=limiter)
        targets = await collect_section_targets(
            client,
            args.section,
            max_sections=args.max_sections,
        )
        logger.info("Секций с товарами: %s (корень %s)", len(targets), args.section)

        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as http:
            remaining = args.limit
            seen_ids: set[str] = set()
            for index, target in enumerate(targets, 1):
                if remaining is not None and remaining <= 0:
                    break
                logger.info(
                    "[%s/%s] %s (%s) ~%s товаров",
                    index,
                    len(targets),
                    target.title,
                    target.code,
                    target.product_qty,
                )
                section_rows = await scrape_section_products(
                    client,
                    http,
                    target,
                    root_code=args.section,
                    limit=remaining,
                    download_images=args.download_images,
                    output_root=output_root,
                    save_db=not args.no_db,
                    seen_ids=seen_ids,
                    skip_existing=args.skip_existing,
                )
                all_rows.extend(section_rows)
                if remaining is not None:
                    remaining -= len(section_rows)
                await rate_limit_delay(args.category_delay)

    summary = {
        "section_root": args.section,
        "sections_scraped": len(targets),
        "products": len(all_rows),
        "db": str(get_repository().db_path) if not args.no_db else None,
    }
    atomic_write_json(output_root / "summary.json", summary)
    export_products(all_rows, output_root / "products_all.xlsx")
    logger.info("Готово: %s товаров, summary=%s", len(all_rows), summary)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсер каталога Petrovich.ru")
    parser.add_argument("--section", type=int, default=DEFAULT_SECTION, help="Код корневого раздела каталога")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="Каталог выгрузки")
    parser.add_argument("--limit", type=int, default=None, help="Лимит товаров (всего)")
    parser.add_argument("--max-sections", type=int, default=None, help="Лимит секций с товарами")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help="Пауза между API-запросами")
    parser.add_argument("--category-delay", type=float, default=SECTION_DELAY, help="Пауза между секциями")
    parser.add_argument("--download-images", action="store_true", help="Скачивать фото в storage/images")
    parser.add_argument("--no-db", action="store_true", help="Не писать в SQLite")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Не запрашивать карточки, которые уже есть в базе",
    )
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
