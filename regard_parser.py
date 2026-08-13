#!/usr/bin/env python3
"""
Парсер каталога regard.ru.

Структура:
  output/regard/{категория}/products.xlsx
  output/regard/{категория}/checkpoint.json
  output/regard/checkpoint_global.json
  storage/images/regard/{source_id}/001.jpg

Источник данных: __NEXT_DATA__.props.initialState (SSR Next.js)
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
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs

import aiohttp

from common import (
    BlockBackoff,
    CategoryProgress,
    CloakSession,
    DomainRateLimiter,
    GlobalCheckpoint,
    atomic_write_json,
    category_dir,
    download_images,
    export_products,
    rate_limit_delay,
    safe_name,
    setup_logging,
)
from db.hooks import persist_regard_row
from normalizer.content_filters import clean_html_description, clean_review_noise
from parser_policy import (
    REGARD_CATEGORY_BLOCK_RETRIES,
    REGARD_SESSION_RESTARTS,
    apply_regard_policy,
)
from storage_paths import canonical_image_dir

BASE_URL = "https://www.regard.ru"
DEFAULT_OUTPUT = "output/regard"
SAVE_EVERY_N_ROWS = 5
BLOCKED_STATUS_CODES = {403, 429}
IMAGE_SIZE = 1200

setup_logging()
logger = logging.getLogger("regard_scraper")

EXTRACT_LISTING_JS = """
() => {
  const st = window.__NEXT_DATA__?.props?.initialState || {};
  const groupId = String(st.listing?.groupId || '');
  const block = st.listing?.data?.[groupId];
  const pagination = st.pagination || {};
  const pageIndex = Number(pagination.pageIndex ?? block?.pageIndex ?? 0);
  const perPage = Number(pagination.perPage || 24);
  let goods = [];
  const rawPages = block?.pages;
  let pageEntries = [];
  if (Array.isArray(rawPages)) {
    pageEntries = rawPages;
  } else if (rawPages && typeof rawPages === 'object') {
    pageEntries = Object.keys(rawPages)
      .sort((a, b) => Number(a) - Number(b))
      .map((key) => rawPages[key]);
  }
  if (pageEntries.length) {
    const matched = pageEntries.find((_, idx) => idx === pageIndex) || pageEntries[pageEntries.length - 1];
    if (Array.isArray(matched?.data)) goods = matched.data;
    else if (Array.isArray(matched)) goods = matched;
  }
  if (!goods.length && Array.isArray(block?.data)) goods = block.data;
  return {
    groupId,
    pageIndex,
    perPage,
    recordsFiltered: block?.recordsFiltered ?? null,
    goods,
  };
}
"""

EXTRACT_PRODUCT_JS = """
() => window.__NEXT_DATA__?.props?.initialState?.product?.data || null
"""

EXTRACT_CATALOG_JS = """
() => {
  const catalog = window.__NEXT_DATA__?.props?.initialState?.catalog?.data || {};
  const out = [];
  for (const [key, node] of Object.entries(catalog)) {
    if (!node || typeof node !== 'object') continue;
    const id = String(node.id ?? key);
    const slug = node.seo_url || node.seoUrl || '';
    const title = node.title || node.full_title || node.name || slug;
    const parentId = node.parent_id ?? node.parentId ?? null;
    out.push({ id, slug, title, parent_id: parentId ? String(parentId) : '' });
  }
  return out;
}
"""

EXTRACT_LD_PRODUCT_JS = """
() => {
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(s.textContent || '');
      if (data && data['@type'] === 'Product') return data;
      if (Array.isArray(data)) {
        for (const item of data) if (item && item['@type'] === 'Product') return item;
      }
    } catch (e) {}
  }
  return null;
}
"""


class BlockedError(Exception):
    """403/429 или страница блокировки."""


@dataclass
class RegardCategory:
    category_id: str
    slug: str
    name: str
    path: str = ""
    url: str = ""

    @property
    def cat_key(self) -> str:
        return f"{self.category_id}|{self.slug}"


def product_url(product_id: str, seo_slug: str) -> str:
    slug = seo_slug.strip("/") or "product"
    return f"{BASE_URL}/product/{product_id}/{slug}"


def category_url(category_id: str, slug: str, *, page: int = 1) -> str:
    slug = slug.strip("/") or "category"
    url = f"{BASE_URL}/catalog/{category_id}/{slug}"
    if page > 1:
        return f"{url}?page={page}"
    return url


def spec_key(group: str, name: str) -> str:
    group = group.strip()
    name = name.strip()
    if group:
        return f"{group} | {name}"
    return name


def regard_image_url(raw: str, *, size: int = IMAGE_SIZE) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http"):
        parsed = urlparse(raw)
        if "/api/photo/vendor/" in parsed.path or "/_next/static/" in parsed.path:
            return ""
        return raw
    img_id = raw.strip("/").split("/")[-1]
    if not img_id.isdigit():
        m = re.search(r"(\d+)$", raw)
        img_id = m.group(1) if m else ""
    if not img_id:
        return ""
    return f"{BASE_URL}/api/site/cacheimg/goods/{img_id}/{size}"


def collect_product_images(data: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    photos = data.get("photos") or {}
    raw_list = photos.get("urls") if isinstance(photos, dict) else []
    if isinstance(raw_list, list):
        for item in raw_list:
            url = regard_image_url(str(item))
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    photo = data.get("photo")
    if photo:
        url = regard_image_url(str(photo))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def format_category_path(category_array: Any, fallback: str = "") -> str:
    if not category_array:
        return fallback
    parts: list[str] = []
    if isinstance(category_array, list):
        for item in category_array:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name") or item.get("full_title") or ""
                if title:
                    parts.append(str(title).strip())
            elif item:
                parts.append(str(item).strip())
    return " > ".join(p for p in parts if p) or fallback


def flatten_characteristics(groups: Any) -> tuple[dict[str, str], list[dict[str, str]], str | None, str | None]:
    specs: dict[str, str] = {}
    spec_groups: list[dict[str, str]] = []
    model: str | None = None
    mpn: str | None = None
    if not isinstance(groups, list):
        return specs, spec_groups, model, mpn

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_title = str(group.get("title") or "").strip()
        rows = group.get("data") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            value = str(row.get("value") or "").strip()
            postfix = str(row.get("postfix") or "").replace("&nbsp", " ").strip()
            if postfix and not value.endswith(postfix.strip()):
                value = f"{value}{postfix}".strip()
            if not name or not value:
                continue
            key = spec_key(group_title, name)
            specs[f"spec__{key}"] = value
            spec_groups.append({"group": group_title, "name": name, "value": value})
            name_lower = name.lower()
            if name_lower == "модель" and not model:
                model = value
            if name_lower in {"mpn", "ean", "gtin", "штрих-код", "штрихкод"} and not mpn:
                mpn = value
    return specs, spec_groups, model, mpn


def parse_availability(data: dict[str, Any], ld_product: dict[str, Any] | None) -> tuple[bool | None, str]:
    availability = ""
    if ld_product:
        offers = ld_product.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        availability = str(offers.get("availability") or "")
    in_stock: bool | None = None
    if "InStock" in availability:
        in_stock = True
    elif "OutOfStock" in availability:
        in_stock = False
    elif data.get("preorder"):
        in_stock = None
    return in_stock, availability


def build_row_from_product(
    data: dict[str, Any],
    *,
    category: CategoryProgress,
    ld_product: dict[str, Any] | None = None,
) -> dict[str, Any]:
    product_id = str(data.get("id") or "")
    seo_slug = str(data.get("seo_url") or "product")
    url = product_url(product_id, seo_slug)

    spec_fields, spec_groups, model_from_specs, mpn_from_specs = flatten_characteristics(
        data.get("characteristics")
    )
    in_stock, availability_status = parse_availability(data, ld_product)

    vendor_code = str(data.get("vendorcode") or "").strip()
    brand = str(data.get("vendor") or "").strip()
    if ld_product and not brand:
        brand_obj = ld_product.get("brand")
        if isinstance(brand_obj, dict):
            brand = str(brand_obj.get("name") or "").strip()

    ld_sku = ""
    if ld_product and ld_product.get("sku") is not None:
        ld_sku = str(ld_product.get("sku")).strip()

    sku = ld_sku or product_id

    brief = clean_review_noise(unescape(str(data.get("brief") or "")))
    postscript = clean_html_description(str(data.get("postscript") or ""))
    raw_parts = [brief, postscript]
    raw_full_text = clean_review_noise("\n\n".join(p for p in raw_parts if p))[:50000]

    category_path = format_category_path(data.get("category_array"), category.category_path)
    photo_urls = collect_product_images(data)

    row: dict[str, Any] = {
        "product_id": product_id,
        "regard_product_id": product_id,
        "name": str(data.get("full_title") or data.get("title") or ""),
        "brand": brand,
        "sku": sku,
        "vendor_code": vendor_code,
        "model": model_from_specs or "",
        "category_id": category.category_id,
        "category_name": category.category_name,
        "category_path": category_path or category.category_path,
        "url": url,
        "sale_price": data.get("price"),
        "old_price": data.get("price_old") or data.get("discounted_price"),
        "in_stock": in_stock,
        "availability_status": availability_status,
        "preorder": data.get("preorder"),
        "show_flag": data.get("show_flag"),
        "rating_count": data.get("reviews_count"),
        "rating_star": data.get("reviews_stars"),
        "description_short": brief,
        "description_detailed": postscript,
        "raw_full_text": raw_full_text,
        "photo_urls": photo_urls,
        "photo_count": len(photo_urls),
        "spec_count": len(spec_groups),
        "spec_groups": spec_groups,
        "grpid": data.get("grpid"),
    }
    if mpn_from_specs:
        row["mpn"] = mpn_from_specs
    row.update(spec_fields)
    return row


def build_row_from_listing_item(
    item: dict[str, Any],
    *,
    category: CategoryProgress,
) -> dict[str, Any]:
    product_id = str(item.get("id") or "")
    seo_slug = str(item.get("seo_url") or "product")
    brief = clean_review_noise(unescape(str(item.get("brief") or "")))
    photo_urls = collect_product_images(item)
    return {
        "product_id": product_id,
        "regard_product_id": product_id,
        "name": str(item.get("full_title") or item.get("title") or ""),
        "brand": str(item.get("vendor") or ""),
        "sku": product_id,
        "vendor_code": str(item.get("vendorcode") or ""),
        "category_id": category.category_id,
        "category_name": category.category_name,
        "category_path": category.category_path,
        "url": product_url(product_id, seo_slug),
        "sale_price": item.get("price"),
        "old_price": item.get("price_old"),
        "description_short": brief,
        "photo_urls": photo_urls,
        "photo_count": len(photo_urls),
    }


def is_blocked(title: str, html: str) -> bool:
    lowered = (title or "").lower()
    if any(x in lowered for x in ("403", "429", "доступ запрещ", "captcha", "robot")):
        return True
    if "cf-browser-verification" in html.lower():
        return True
    return False


def discover_categories_from_catalog(nodes: list[dict[str, Any]]) -> list[RegardCategory]:
    by_id = {n["id"]: n for n in nodes if n.get("id")}
    categories: list[RegardCategory] = []
    seen: set[str] = set()

    def path_for(cat_id: str) -> str:
        parts: list[str] = []
        current = by_id.get(cat_id)
        guard = 0
        while current and guard < 20:
            title = str(current.get("title") or current.get("slug") or "").strip()
            if title:
                parts.append(title)
            parent = str(current.get("parent_id") or "")
            if not parent or parent == cat_id:
                break
            current = by_id.get(parent)
            guard += 1
        return " > ".join(reversed(parts))

    for node in nodes:
        cat_id = str(node.get("id") or "")
        slug = str(node.get("slug") or "").strip()
        if not cat_id or not slug or cat_id in seen:
            continue
        if cat_id in {"sale"} or slug == "sale":
            continue
        seen.add(cat_id)
        name = str(node.get("title") or slug.replace("-", " ")).strip()
        path = path_for(cat_id)
        categories.append(
            RegardCategory(
                category_id=cat_id,
                slug=slug,
                name=name,
                path=path or name,
                url=category_url(cat_id, slug),
            )
        )
    return categories


class RegardScraper:
    def __init__(
        self,
        page,
        checkpoint: GlobalCheckpoint,
        *,
        http_session: aiohttp.ClientSession,
        rate_limiter: DomainRateLimiter,
        download_images_flag: bool = True,
        category_delay: float = 20.0,
    ):
        self.page = page
        self.checkpoint = checkpoint
        self.http = http_session
        self.rate_limiter = rate_limiter
        self.download_images_flag = download_images_flag
        self.category_delay = category_delay
        self.failed = 0
        self.deferred = 0

    async def _goto_safe(self, url: str) -> None:
        await self.rate_limiter.wait()
        response =         await self.page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(6)
        if response and response.status in BLOCKED_STATUS_CODES:
            raise BlockedError(f"HTTP {response.status}: {url}")
        title = await self.page.title()
        html = await self.page.content()
        if is_blocked(title, html):
            raise BlockedError(f"blocked page: {url}")

    async def discover_categories(self) -> list[RegardCategory]:
        await self._goto_safe(BASE_URL)
        nodes = await self.page.evaluate(EXTRACT_CATALOG_JS)
        categories = discover_categories_from_catalog(nodes or [])
        logger.info("Regard categories discovered: %s", len(categories))
        return categories

    async def scrape_category_listing(
        self,
        category: RegardCategory,
        *,
        max_products: int | None,
        cat_state: CategoryProgress,
    ) -> None:
        collected: set[str] = set(self.checkpoint.completed_ids)
        collected.update(str(p.get("product_id", "")) for p in self.checkpoint.pending)

        page_num = max(1, int(cat_state.list_page or 1))
        list_complete = bool(cat_state.list_complete)

        while not list_complete:
            if max_products and len(collected) >= max_products:
                list_complete = True
                break

            url = category_url(category.category_id, category.slug, page=page_num)
            await self._goto_safe(url)
            payload = await self.page.evaluate(EXTRACT_LISTING_JS)
            goods = payload.get("goods") or []
            records_filtered = payload.get("recordsFiltered")
            per_page = int(payload.get("perPage") or 24)

            if not goods:
                list_complete = True
                break

            new_count = 0
            for item in goods:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or "")
                if not pid or pid in collected:
                    continue
                if max_products and len(collected) >= max_products:
                    break
                seo_slug = str(item.get("seo_url") or "product")
                self.checkpoint.pending.append(
                    {"product_id": pid, "url": product_url(pid, seo_slug), "seo_url": seo_slug}
                )
                collected.add(pid)
                new_count += 1

            logger.info(
                "Regard listing %s page=%s goods=%s new=%s pending=%s",
                category.slug,
                page_num,
                len(goods),
                new_count,
                len(self.checkpoint.pending),
            )

            if max_products and len(collected) >= max_products:
                list_complete = True
                break

            if records_filtered is not None and page_num * per_page >= int(records_filtered):
                list_complete = True
                break
            if len(goods) < per_page:
                list_complete = True
                break

            page_num += 1
            cat_state.list_page = page_num

        cat_state.list_complete = list_complete

    async def scrape_product_detail(
        self,
        item: dict[str, Any],
        category: CategoryProgress,
        cat_dir: Path,
    ) -> dict[str, Any] | None:
        url = str(item.get("url") or "")
        if not url:
            return None
        await self._goto_safe(url)
        data = await self.page.evaluate(EXTRACT_PRODUCT_JS)
        ld_product = await self.page.evaluate(EXTRACT_LD_PRODUCT_JS)
        if not data:
            raise RuntimeError(f"product.data missing: {url}")

        row = build_row_from_product(data, category=category, ld_product=ld_product)

        if self.download_images_flag and row.get("photo_urls"):
            img_dir = canonical_image_dir("regard", row["product_id"])
            prefix = f"storage/images/regard/{row['product_id']}"
            local = await download_images(
                self.http,
                list(row["photo_urls"]),
                img_dir,
                referer=url,
                path_prefix=prefix,
            )
            row["local_images"] = " | ".join(local)
            row["local_image_count"] = len(local)

        persist_regard_row(row, cat_dir)
        row.pop("photo_urls", None)
        return row

    async def process_pending(
        self,
        category: RegardCategory,
        cat_dir: Path,
        checkpoint_path: Path,
        *,
        max_products: int | None,
    ) -> None:
        cat_state = self.checkpoint.category
        if not cat_state:
            return

        while self.checkpoint.pending:
            if max_products and len(self.checkpoint.completed_set) >= max_products:
                break
            item = self.checkpoint.pending.pop(0)
            pid = str(item.get("product_id") or "")
            if pid in self.checkpoint.completed_set:
                continue
            try:
                row = await self.scrape_product_detail(item, cat_state, cat_dir)
                if row:
                    self.checkpoint.add_row(row)
            except BlockedError:
                self.checkpoint.pending.insert(0, item)
                raise
            except Exception as exc:
                self.failed += 1
                logger.warning("Regard detail failed %s: %s", pid, exc)
                self.checkpoint.add_row(
                    {
                        "product_id": pid,
                        "url": item.get("url", ""),
                        "category_path": cat_state.category_path,
                        "details_error": str(exc),
                    }
                )

            if len(self.checkpoint.rows) % SAVE_EVERY_N_ROWS == 0:
                export_products(self.checkpoint.rows, cat_dir / "products.xlsx")
            self.checkpoint.save(checkpoint_path)
            logger.info(
                "Regard detail progress: %s done, %s pending",
                len(self.checkpoint.rows),
                len(self.checkpoint.pending),
            )

        if self.checkpoint.rows:
            export_products(self.checkpoint.rows, cat_dir / "products.xlsx")
        self.checkpoint.save(checkpoint_path)

    async def scrape_category(
        self,
        category: RegardCategory,
        *,
        max_products: int | None,
        cat_dir: Path,
        checkpoint_path: Path,
    ) -> None:
        if self.checkpoint.category is None or self.checkpoint.category.category_id != category.category_id:
            self.checkpoint.category = CategoryProgress(
                category_id=category.category_id,
                category_name=category.name,
                category_path=category.path or category.name,
                category_slug=category.slug,
                category_url=category.url,
                list_page=1,
                list_complete=False,
            )
            if not self.checkpoint.pending and not self.checkpoint.completed_ids:
                self.checkpoint.pending = []
                self.checkpoint.rows = []
                self.checkpoint.completed_ids = []

        cat_state = self.checkpoint.category
        if (
            cat_state.list_complete
            and not self.checkpoint.pending
            and not self.checkpoint.rows
        ):
            cat_state.list_complete = False
            cat_state.list_page = 1
        if not cat_state.list_complete:
            await self.scrape_category_listing(category, max_products=max_products, cat_state=cat_state)
            self.checkpoint.save(checkpoint_path)

        await self.process_pending(category, cat_dir, checkpoint_path, max_products=max_products)


async def scrape_once(args: argparse.Namespace) -> dict[str, int]:
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    global_cp_path = output_root / "checkpoint_global.json"

    global_cp: dict[str, Any] = {}
    if global_cp_path.exists():
        global_cp = json.loads(global_cp_path.read_text(encoding="utf-8"))
    completed_categories: set[str] = set(global_cp.get("completed_categories", []))

    stats = {"failed": 0, "deferred": 0}

    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as http:
        profile = str(output_root / ".browser_profile")
        async with CloakSession(headless=args.headless, humanize=True, profile_dir=profile) as session:
            page = await session.new_page()
            rate_limiter = DomainRateLimiter(args.delay)

            scraper = RegardScraper(
                page,
                GlobalCheckpoint(shop="regard", output_root=str(output_root)),
                http_session=http,
                rate_limiter=rate_limiter,
                download_images_flag=not args.skip_images,
                category_delay=args.category_delay,
            )

            if args.category:
                categories = [
                    RegardCategory(
                        category_id=str(args.category),
                        slug=args.category_slug or "category",
                        name=args.category_name or args.category_slug or str(args.category),
                        path=args.category_path or args.category_name or args.category_slug or "",
                        url=category_url(str(args.category), args.category_slug or "category"),
                    )
                ]
            else:
                categories = await scraper.discover_categories()
                if args.max_categories:
                    categories = categories[: args.max_categories]

            for index, category in enumerate(categories, 1):
                if category.cat_key in completed_categories:
                    logger.info("Skip completed category: %s", category.slug)
                    continue

                cat_dir = category_dir(
                    output_root,
                    category.path or category.name,
                    category.category_id,
                )
                cp_path = cat_dir / "checkpoint.json"
                checkpoint = GlobalCheckpoint.load(cp_path)
                if checkpoint is None:
                    checkpoint = GlobalCheckpoint(shop="regard", output_root=str(output_root))

                scraper.checkpoint = checkpoint
                scraper.failed = 0

                logger.info("[%s/%s] Regard %s", index, len(categories), category.url)
                cat_backoff = BlockBackoff(max_attempts=REGARD_CATEGORY_BLOCK_RETRIES)
                category_done = False
                while not category_done:
                    try:
                        await scraper.scrape_category(
                            category,
                            max_products=args.limit,
                            cat_dir=cat_dir,
                            checkpoint_path=cp_path,
                        )
                        category_done = True
                    except BlockedError as exc:
                        checkpoint.save(cp_path)
                        if checkpoint.rows:
                            export_products(checkpoint.rows, cat_dir / "products.xlsx")
                        if not await cat_backoff.wait_before_retry():
                            stats["deferred"] += 1
                            logger.error("Category deferred after blocks: %s (%s)", category.slug, exc)
                            break
                        logger.info("Retry category %s after backoff", category.slug)

                stats["failed"] += scraper.failed
                processed = len(checkpoint.completed_set)
                if category_done and args.limit and processed < args.limit and checkpoint.pending:
                    logger.warning(
                        "Category %s finished with %s/%s products — not marking complete",
                        category.slug,
                        processed,
                        args.limit,
                    )
                    category_done = False
                if category_done and processed > 0:
                    completed_categories.add(category.cat_key)
                    global_cp["completed_categories"] = sorted(completed_categories)
                    atomic_write_json(global_cp_path, global_cp)
                    logger.info("Category done: %s -> %s (%s products)", category.slug, cat_dir, processed)
                elif category_done:
                    logger.info("Category scraped: %s -> %s (%s products)", category.slug, cat_dir, processed)

                if index < len(categories):
                    await rate_limit_delay(args.category_delay)

    return stats


async def run_with_retries(args: argparse.Namespace) -> dict[str, int]:
    session_backoff = BlockBackoff(max_attempts=REGARD_SESSION_RESTARTS)
    while True:
        try:
            return await scrape_once(args)
        except BlockedError as exc:
            if not await session_backoff.wait_before_retry():
                raise
            logger.warning("Restart Regard session after block: %s", exc)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Парсер regard.ru")
    p.add_argument("-o", "--output", default=DEFAULT_OUTPUT)
    p.add_argument("--category", help="ID категории (numeric group_id)")
    p.add_argument("--category-slug", default="", help="SEO slug категории")
    p.add_argument("--category-name", default="", help="Название категории")
    p.add_argument("--category-path", default="", help="Путь категории")
    p.add_argument("--limit", type=int, help="Лимит товаров на категорию")
    p.add_argument("--max-categories", type=int)
    p.add_argument("--skip-images", action="store_true")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--delay", type=float, default=4.0)
    p.add_argument("--category-delay", type=float, default=20.0)
    p.add_argument("--workers", type=int, default=1)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    apply_regard_policy(args)
    stats = asyncio.run(run_with_retries(args))
    logger.info("Regard run finished: failed=%s deferred=%s", stats.get("failed"), stats.get("deferred"))


if __name__ == "__main__":
    main()
