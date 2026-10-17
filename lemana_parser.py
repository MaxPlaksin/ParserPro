#!/usr/bin/env python3
"""
Парсер витрины Лемана ПРО (бывший Леруа Мерлен), lemanapro.ru.

Данные берутся из window.INITIAL_STATE на странице каталога,
карточка товара дочитывается со страницы номенклатуры.

Примеры:
  python lemana_parser.py --limit 3
  python lemana_parser.py --url https://spb.lemanapro.ru/catalogue/dekorativnye-paneli/
  python lemana_parser.py --skip-existing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

from common import (
    CloakSession,
    atomic_write_json,
    category_dir,
    download_images,
    export_products,
    rate_limit_delay,
    setup_logging,
)
from db.hooks import persist_lemana_row
from db.repository import get_repository

DEFAULT_URL = "https://lemanapro.ru/catalogue/dekorativnye-paneli/"
DEFAULT_OUTPUT = "output/lemana"
# Ночью при паузе около 8 секунд на карточку сайт не закрывал доступ.
# Пауза 0.2 секунды разгоняла сбор до ~30 карточек в минуту и приводила к блокировке.
REQUEST_DELAY = 7.0
SAVE_EVERY_N = 80
BLOCK_EXIT_CODE = 75
BLOCK_COOLDOWN_SEC = 10 * 60
BLOCK_MARKERS = (
    "access to resource was blocked",
    "доступ к ресурсу заблокирован",
)

setup_logging()
logger = logging.getLogger("lemana_scraper")

READ_PLP_JS = r"""
() => {
  const shell = window.INITIAL_STATE?.plp?.plp || {};
  const plp = shell.plp || shell;
  const products = plp.products || {};
  const data = products.productsData || [];
  const sections = [];
  for (const block of (plp.catalogueSections?.sections || [])) {
    if (block.kind !== 'CategoryTiles') continue;
    for (const cat of (block.categories || [])) {
      sections.push({
        name: cat.name || cat.label || '',
        path: cat.sitePath || '',
        familyId: cat.familyId || '',
      });
    }
  }
  const tree = [];
  for (const node of (plp.catalogueStructure?.catalogue || [])) {
    tree.push({
      name: node.name || node.label || '',
      path: node.sitePath || '',
      familyId: node.familyId || node.id || '',
      level: node.level || 0,
    });
  }
  const crumbs = [];
  const model = plp.pageModel?.model || {};
  const seo = model.seo || model.pageTitle || '';
  return {
    title: document.title,
    url: location.href,
    h1: (document.querySelector('h1') || {}).innerText || '',
    count: products.productsCount || data.length,
    offset: products.offset || 0,
    products: data,
    sections,
    tree,
    seo: typeof seo === 'string' ? seo : '',
    nextHref: (document.querySelector('[data-qa-pagination-item="right"]') || {}).href || '',
    breadcrumbs: (model.breadcrumbs || []).map((item) => ({
      label: item.label || '',
      link: item.link || '',
    })),
  };
}
"""

CLICK_MORE_JS = r"""
() => {
  const nodes = [...document.querySelectorAll('button, a, span')];
  const btn = nodes.find((el) => {
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    return text.length > 0 && text.length < 40 && /показать ещ[её]|загрузить ещ[её]/i.test(text);
  });
  if (!btn) return false;
  btn.click();
  return true;
}
"""

READ_PDP_JS = r"""
() => {
  const html = document.documentElement.innerHTML;
  const lds = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map((s) => s.textContent || '')
    .filter((t) => t.includes('Product') || t.includes('description'));
  const imgs = [...new Set((html.match(/https:\/\/cdn\.lemanapro\.ru\/[^"'\\\s]+/g) || []))]
    .filter((u) => /lmcode|product/i.test(u))
    .slice(0, 40);
  const pdfs = [...new Set((html.match(/https?:\/\/[^"'\\\s]+\.pdf/gi) || []))];
  const h1 = (document.querySelector('h1') || {}).innerText || '';
  let description = '';
  const descNode = document.querySelector('[data-qa="product-description"], [class*="pdp-description"], [class*="product-description"]');
  if (descNode) description = descNode.innerText || '';
  if (!description) {
    const m = html.match(/"description"\s*:\s*"((?:\\.|[^"\\]){40,4000})"/);
    if (m) {
      try { description = JSON.parse('"' + m[1] + '"'); } catch (e) { description = m[1]; }
    }
  }
  return { h1, description, lds, imgs, pdfs, url: location.href };
}
"""


SITE = "https://lemanapro.ru"
_PICTURE_FILE = re.compile(
    r"(https://cdn\.lemanapro\.ru/lmru/image/upload/)(?:(?!v\d+/)[^/]+/)*?(v\d+/.+)$",
    re.IGNORECASE,
)


def picture_link(url: str) -> str:
    """Ссылка на файл картинки, без скачивания и без превью 150×150."""
    clean = (url or "").split("?")[0].strip()
    if not clean:
        return ""
    match = _PICTURE_FILE.search(clean)
    if match:
        return match.group(1) + match.group(2)
    return clean


def picture_links(urls: list[Any]) -> list[str]:
    found: list[str] = []
    for url in urls:
        link = picture_link(str(url))
        if link and link not in found and not link.lower().endswith(".pdf"):
            found.append(link)
    return found


def abs_url(base: str, url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not (base or "").startswith("http"):
        base = SITE + "/"
    return urljoin(base, url)


def bump_page(url: str) -> str:
    match = re.search(r"([?&])page=(\d+)", url)
    if not match:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}page=2"
    page_num = int(match.group(2)) + 1
    return url[: match.start(2)] + str(page_num) + url[match.end(2) :]


def chars_from_list(items: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("description") or item.get("key") or "").strip()
        value = item.get("value")
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v not in (None, ""))
        if name and value not in (None, ""):
            out[name] = str(value)
    return out


def parse_ld(blobs: list[str]) -> dict[str, Any]:
    for blob in blobs:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") in ("Product", "ProductGroup"):
                return node
    return {}


def site_crumbs(meta: dict[str, Any]) -> list[dict[str, str]]:
    crumbs: list[dict[str, str]] = []
    for item in meta.get("breadcrumbs") or []:
        label = str(item.get("label") or "").strip()
        if not label or label in {"Главная", "Каталог"}:
            continue
        link = abs_url(SITE, str(item.get("link") or ""))
        crumbs.append({"name": label, "url": link})
    return crumbs


def leaf_index(meta: dict[str, Any]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for node in (meta.get("tree") or []) + (meta.get("sections") or []):
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        url = abs_url(SITE, str(node.get("path") or ""))
        item = {"name": name, "url": url}
        family = str(node.get("familyId") or "")
        if family:
            found[family] = item
        found[name] = item
    return found


def apply_site_category(stub: dict[str, Any], meta: dict[str, Any], *, page_url: str, page_title: str) -> None:
    crumbs = site_crumbs(meta)
    leaves = leaf_index(meta)
    family = str(stub.get("family_id") or "")
    leaf = leaves.get(family) or leaves.get(str(stub.get("subgroup_name") or "")) or {}
    leaf_name = str(stub.get("subgroup_name") or leaf.get("name") or "").strip()
    parts = [crumb["name"] for crumb in crumbs]
    if leaf_name and (not parts or parts[-1] != leaf_name):
        parts.append(leaf_name)
    if not parts:
        parts = [page_title or "Лемана ПРО"]
    stub["category_path"] = " > ".join(parts)
    stub["category_name"] = parts[-1]
    stub["group_name"] = " > ".join(parts[:-1]) if len(parts) > 1 else ""
    stub["subgroup_name"] = leaf_name or stub["category_name"]
    leaf_url = leaf.get("url") or ""
    stub["category_url"] = leaf_url or (crumbs[-1]["url"] if crumbs else page_url)
    stub["category_id"] = family or stub["category_url"].rstrip("/") or page_url.rstrip("/")


def listing_to_stub(product: dict[str, Any], *, page_url: str, page_title: str) -> dict[str, Any]:
    code = str(product.get("productId") or "")
    price = (product.get("price") or {}) if isinstance(product.get("price"), dict) else {}
    photo = ""
    media = product.get("mediaMainPhoto") or {}
    if isinstance(media, dict):
        photo = picture_link(media.get("desktop") or media.get("tablet") or media.get("mobile") or "")
    compare = product.get("compareCategory") or {}
    subgroup = compare.get("name") if isinstance(compare, dict) else ""
    family_id = compare.get("familyId") if isinstance(compare, dict) else ""
    link = abs_url(page_url, str(product.get("productLink") or f"/product/{code}/"))
    return {
        "product_id": code,
        "sku": code,
        "nomenclature_code": code,
        "name": product.get("displayedName") or "",
        "brand": product.get("brand") or "",
        "url": link,
        "sale_price": price.get("main_price"),
        "old_price": price.get("previous_price"),
        "unit": price.get("main_uom_rus") or "",
        "photo_urls": [photo] if photo else [],
        "characteristics": chars_from_list(product.get("characteristics")),
        "subgroup_name": subgroup or "",
        "family_id": family_id or "",
        "category_name": subgroup or page_title,
        "category_id": family_id or page_url.rstrip("/").split("/")[-1],
        "category_path": page_title,
        "in_stock": True,
    }


async def read_plp(page) -> dict[str, Any]:
    return await page.evaluate(READ_PLP_JS)


async def collect_listing(page, *, limit: int | None, pause: float) -> dict[str, Any]:
    seen: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {}
    stale = 0
    empty_polls = 0
    while True:
        data = await read_plp(page)
        meta = data
        if not (data.get("products") or []) and not seen:
            empty_polls += 1
            logger.info(
                "Жду каталог (%s): %s",
                empty_polls,
                data.get("title") or data.get("url"),
            )
            if empty_polls >= 12:
                break
            await page.wait_for_timeout(2000)
            continue
        before = len(seen)
        for product in data.get("products") or []:
            code = str(product.get("productId") or "")
            if code:
                seen[code] = product
        logger.info("Листинг: %s / %s", len(seen), data.get("count"))
        if limit is not None and len(seen) >= limit:
            break
        total = int(data.get("count") or 0)
        if total and len(seen) >= total:
            break
        if len(seen) == before:
            stale += 1
        else:
            stale = 0
        if stale >= 2:
            break
        clicked = await page.evaluate(CLICK_MORE_JS)
        if not clicked:
            break
        await page.wait_for_timeout(int(pause * 1000))
    return {"meta": meta, "products": list(seen.values())[:limit] if limit else list(seen.values())}


def _page_crashed(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "crashed",
            "target closed",
            "has been closed",
            "execution context was destroyed",
            "navigation",
        )
    )


def _browser_dead(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "connection closed",
            "browser has been closed",
            "browser closed",
            "target closed",
            "has been closed",
        )
    )


async def reopen_page(cloak, page, *, reason: str = "Страница браузера упала, открываю новую"):
    logger.warning(reason)
    try:
        if page and not page.is_closed():
            await page.close()
    except Exception:
        pass
    try:
        return await cloak.new_page()
    except Exception as exc:
        if _browser_dead(exc):
            logger.error("Браузер закрылся. Выхожу, сбор продолжится с той же страницы каталога.")
            raise SystemExit(1) from exc
        raise


async def page_is_blocked(page) -> bool:
    try:
        text = await page.evaluate(
            "() => ((document.body && document.body.innerText) || '').slice(0, 2000).toLowerCase()"
        )
    except Exception:
        return False
    sample = str(text or "")
    return any(marker in sample for marker in BLOCK_MARKERS)


async def wait_out_block(page, cloak, url: str):
    """Одна длинная пауза и одна повторная проверка. Пустую карточку не сохраняем."""
    logger.warning(
        "Сайт закрыл доступ (%s). Карточку не сохраняю, пауза %s минут.",
        url,
        BLOCK_COOLDOWN_SEC // 60,
    )
    left = BLOCK_COOLDOWN_SEC
    while left > 0:
        step = min(60, left)
        await asyncio.sleep(step)
        left -= step
        if left:
            logger.info("Жду, пока сайт откроет доступ, осталось %s мин", (left + 59) // 60)
    page = await reopen_page(cloak, page, reason="Проверяю, открылся ли сайт после паузы")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception as exc:
        logger.error("После паузы страница не открылась: %s. Останавливаю сбор.", exc)
        raise SystemExit(BLOCK_EXIT_CODE) from exc
    if await page_is_blocked(page):
        logger.error(
            "Доступ всё ещё закрыт. Останавливаю сбор, чтобы не запрашивать сайт снова и не писать пустые карточки."
        )
        raise SystemExit(BLOCK_EXIT_CODE)
    return page


async def read_pdp(page, cloak, url: str):
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    if await page_is_blocked(page):
        page = await wait_out_block(page, cloak, url)
    data: dict[str, Any] = {}
    for _ in range(4):
        await page.wait_for_timeout(350)
        if await page_is_blocked(page):
            page = await wait_out_block(page, cloak, url)
        data = await page.evaluate(READ_PDP_JS)
        if data.get("imgs") and str(data.get("description") or "").strip():
            break
    if await page_is_blocked(page):
        page = await wait_out_block(page, cloak, url)
        data = await page.evaluate(READ_PDP_JS)
    return page, data


async def skip_picture_files(route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


def place_known_products(stubs: list[dict[str, Any]]) -> None:
    """Ставит уже сохранённый товар в категорию сайта, карточку заново не качает."""
    if not stubs:
        return
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    repo = get_repository()
    conn = repo._connect()
    try:
        for stub in stubs:
            source_id = str(stub.get("product_id") or "")
            category_key = str(stub.get("category_id") or "")
            if not source_id or not category_key:
                continue
            conn.execute(
                """
                INSERT INTO categories (source, source_category_id, name, path, created_at, updated_at)
                VALUES ('lemana', ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_category_id) DO UPDATE SET
                    name = excluded.name,
                    path = excluded.path,
                    updated_at = excluded.updated_at
                """,
                (
                    category_key,
                    stub.get("category_name") or "",
                    stub.get("category_path") or "",
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM categories WHERE source = 'lemana' AND source_category_id = ?",
                (category_key,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE products SET category_id = ? WHERE source = 'lemana' AND source_id = ?",
                    (row["id"], source_id),
                )
        conn.commit()
    finally:
        conn.close()


def backfill_panel_paths() -> None:
    """Раскладывает уже скачанные панели по пути с сайта, без повторного открытия карточек."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    repo = get_repository()
    conn = repo._connect()
    try:
        named = conn.execute(
            """
            SELECT id, name FROM categories
            WHERE source = 'lemana' AND path = 'Декоративные панели' AND name != 'Декоративные панели'
            """
        ).fetchall()
        for row in named:
            conn.execute(
                "UPDATE categories SET path = ?, updated_at = ? WHERE id = ?",
                (f"Столярные изделия > Декоративные панели > {row['name']}", now, row["id"]),
            )
        generic_ids = [
            row["id"]
            for row in conn.execute(
                """
                SELECT id FROM categories
                WHERE source = 'lemana' AND name = 'Декоративные панели' AND instr(path, ' > ') = 0
                """
            )
        ]
        leaf_ids = {
            row["name"]: row["id"]
            for row in conn.execute(
                """
                SELECT id, name FROM categories
                WHERE source = 'lemana' AND path LIKE 'Столярные изделия > Декоративные панели > %'
                """
            )
        }
        if generic_ids:
            marks = ",".join("?" for _ in generic_ids)
            products = conn.execute(
                f"SELECT id, category_id, extra_json FROM products WHERE source = 'lemana' AND category_id IN ({marks})",
                generic_ids,
            ).fetchall()
            for product in products:
                extra = json.loads(product["extra_json"] or "{}")
                leaf = str(extra.get("subgroup_name") or "").strip()
                target = leaf_ids.get(leaf)
                if target:
                    conn.execute("UPDATE products SET category_id = ? WHERE id = ?", (target, product["id"]))
                else:
                    conn.execute(
                        "UPDATE categories SET path = ?, updated_at = ? WHERE id = ? AND instr(path, ' > ') = 0",
                        ("Столярные изделия > Декоративные панели", now, product["category_id"]),
                    )
        conn.commit()
    finally:
        conn.close()


def backfill_picture_links() -> None:
    """Оставляет ссылки на картинки и на карточку товара, сами файлы не трогает."""
    repo = get_repository()
    conn = repo._connect()
    try:
        products = conn.execute(
            "SELECT id, url, extra_json FROM products WHERE source = 'lemana'"
        ).fetchall()
        updated = 0
        for product in products:
            extra = json.loads(product["extra_json"] or "{}")
            resources = extra.get("resource_links") if isinstance(extra.get("resource_links"), dict) else {}
            images = picture_links(resources.get("images") or [])
            page = str(resources.get("product_page") or product["url"] or "")
            changed = images != (resources.get("images") or []) or resources.get("product_page") != page
            if not changed:
                continue
            resources["images"] = images
            resources["product_page"] = page
            extra["resource_links"] = resources
            conn.execute(
                "UPDATE products SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), product["id"]),
            )
            updated += 1
        seen: set[tuple[int, str]] = set()
        photos = conn.execute(
            """
            SELECT i.id, i.product_id, i.source_url
            FROM product_images i
            JOIN products p ON p.id = i.product_id
            WHERE p.source = 'lemana' AND coalesce(i.source_url, '') != ''
            """
        ).fetchall()
        for photo in photos:
            link = picture_link(photo["source_url"])
            key = (int(photo["product_id"]), link)
            if not link or key in seen:
                conn.execute("DELETE FROM product_images WHERE id = ?", (photo["id"],))
                continue
            seen.add(key)
            if link != photo["source_url"]:
                conn.execute(
                    "UPDATE product_images SET source_url = ? WHERE id = ?",
                    (link, photo["id"]),
                )
        conn.commit()
        logger.info("Ссылки на товары и картинки сохранены, файлы картинок не скачиваются. Обновлено карточек: %s", updated)
    finally:
        conn.close()


def load_known_ids() -> set[str]:
    repo = get_repository()
    conn = repo._connect()
    try:
        found = conn.execute("SELECT source_id FROM products WHERE source = 'lemana'").fetchall()
        return {str(row["source_id"]) for row in found}
    finally:
        conn.close()


def merge_pdp(row: dict[str, Any], pdp: dict[str, Any]) -> dict[str, Any]:
    ld = parse_ld(pdp.get("lds") or [])
    description = (pdp.get("description") or ld.get("description") or "").strip()
    images = picture_links([*(pdp.get("imgs") or []), *(row.get("photo_urls") or [])])
    pdfs = pdp.get("pdfs") or []
    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if row.get("sale_price") in (None, "") and isinstance(offers, dict):
        row["sale_price"] = offers.get("price")
    row["description_detailed"] = description
    row["description_short"] = description[:500]
    row["raw_full_text"] = description
    row["photo_urls"] = images
    row["photos"] = "|".join(images)
    row["certificates"] = [{"url": url} for url in pdfs]
    row["resource_links"] = {
        "product_page": row.get("url"),
        "images": images,
        "certificates": pdfs,
        "documents": pdfs,
    }
    if ld.get("sku") and not row.get("sku"):
        row["sku"] = str(ld["sku"])
    return row


DISCOVER_JS = r"""
() => [...new Set(
  [...document.querySelectorAll('a[href*="/catalogue/"]')]
    .map((a) => (a.href || '').split('?')[0].split('#')[0])
)].filter((href) => /\/catalogue\/[^/]+\/?$/.test(href));
"""


def save_sections(meta: dict[str, Any], page_url: str) -> None:
    repo = get_repository()
    crumbs = site_crumbs(meta)
    if not crumbs:
        title = (meta.get("h1") or meta.get("title") or "Каталог").strip()
        clean = page_url.split("?")[0]
        if not clean.endswith("/"):
            clean += "/"
        crumbs = [{"name": title, "url": clean}]
    parent = None
    parent_path = ""
    for depth, crumb in enumerate(crumbs):
        parent_path = f"{parent_path} > {crumb['name']}" if parent_path else crumb["name"]
        code = crumb["url"].rstrip("/") or crumb["name"]
        repo.upsert_catalog_section(
            source="lemana",
            section_code=code,
            parent_section_code=parent,
            name=crumb["name"],
            path=parent_path,
            url=crumb["url"] or page_url.split("?")[0],
            kind="group" if depth == 0 else "subgroup",
            depth=depth,
            product_qty=int(meta.get("count") or 0) if depth == len(crumbs) - 1 else None,
        )
        parent = code
    seen_codes: set[str] = set()
    for node in (meta.get("tree") or []) + (meta.get("sections") or []):
        name = str(node.get("name") or "").strip()
        if not name or name == crumbs[-1]["name"]:
            continue
        url = abs_url(SITE, str(node.get("path") or ""))
        code = (url or str(node.get("familyId") or name)).rstrip("/")
        if code in seen_codes:
            continue
        seen_codes.add(code)
        repo.upsert_catalog_section(
            source="lemana",
            section_code=code,
            parent_section_code=parent,
            name=name,
            path=f"{parent_path} > {name}",
            url=url or page_url.split("?")[0],
            kind="subgroup",
            depth=len(crumbs),
            product_qty=None,
        )


async def run(args: argparse.Namespace) -> int:
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    if args.skip_existing:
        backfill_panel_paths()
        backfill_picture_links()
    known_ids = load_known_ids() if args.skip_existing else set()
    if known_ids:
        logger.info("Уже в базе Лемана: %s, эти карточки не открываю", len(known_ids))
    resume_path = output_root / "resume_url.txt"
    paused_path = output_root / "paused_url.txt"
    urls = list(args.url or [DEFAULT_URL])

    def section_key(url: str) -> str:
        return url.split("?")[0].rstrip("/")

    saved_url = ""
    if args.skip_existing and resume_path.exists():
        saved_url = resume_path.read_text(encoding="utf-8").strip()
    saved_key = section_key(saved_url) if saved_url.startswith("http") else ""
    if saved_key and any(section_key(url) == saved_key for url in urls):
        urls = [saved_url] + [url for url in urls if section_key(url) != saved_key]
        logger.info("Продолжаю с %s", saved_url)
    if paused_path.exists():
        paused_url = paused_path.read_text(encoding="utf-8").strip()
        paused_key = section_key(paused_url) if paused_url.startswith("http") else ""
        if paused_key and all(section_key(url) != paused_key for url in urls):
            urls.append(paused_url)
            logger.info("После приоритетных разделов вернусь к %s", paused_url)

    profile_dir = output_root / "profile"
    async with CloakSession(headless=args.headless, profile_dir=str(profile_dir)) as cloak:
        await cloak.context.route("**/*", skip_picture_files)
        logger.info("Файлы картинок не загружаю, в базу пишу ссылку товара и ссылки на картинки")
        page = await cloak.new_page()
        pending = list(urls)
        logger.info("Очередь разделов: %s", " | ".join(pending))
        visited_categories: set[str] = set()
        discovered = not args.follow_catalogue
        saved = 0

        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as http:
          while pending:
            catalog_url = pending.pop(0)
            category_key = catalog_url.split("?")[0].rstrip("/")
            if category_key in visited_categories:
                continue
            visited_categories.add(category_key)
            seen_ids: set[str] = set()
            empty_tries = 0
            empty_advances = 0
            known_total = 0
            page_title = "Лемана ПРО"
            folder = output_root
            sections_saved = False
            logger.info("Раздел очереди: %s", catalog_url)
            while catalog_url:
                logger.info("Открываю %s", catalog_url)
                resume_path.write_text(catalog_url, encoding="utf-8")
                opened = False
                for attempt in range(3):
                    try:
                        await page.goto(catalog_url, wait_until="domcontentloaded", timeout=120000)
                        opened = True
                        break
                    except Exception as exc:
                        if not _page_crashed(exc) or attempt == 2:
                            raise
                        page = await reopen_page(cloak, page)
                if not opened:
                    break
                if await page_is_blocked(page):
                    page = await wait_out_block(page, cloak, catalog_url)
                data: dict[str, Any] = {}
                for _ in range(12):
                    await page.wait_for_timeout(700)
                    if await page_is_blocked(page):
                        page = await wait_out_block(page, cloak, catalog_url)
                    try:
                        data = await read_plp(page)
                    except Exception as exc:
                        logger.warning("Каталог ещё грузится: %s", exc)
                        if _page_crashed(exc) and "destroyed" not in str(exc).lower() and "navigation" not in str(exc).lower():
                            page = await reopen_page(cloak, page)
                        continue
                    if data.get("products"):
                        break
                products = data.get("products") or []
                total = int(data.get("count") or 0) or known_total
                if total:
                    known_total = total
                if not products:
                    empty_tries += 1
                    logger.warning("Пустая страница %s, попытка %s", catalog_url, empty_tries)
                    if empty_tries <= 2:
                        page = await reopen_page(cloak, page, reason="Пустой каталог, открываю вкладку заново")
                        continue
                    finished = bool(known_total) and len(seen_ids) >= known_total
                    if not finished and empty_advances < 3:
                        catalog_url = bump_page(catalog_url)
                        empty_tries = 0
                        empty_advances += 1
                        logger.warning("Перехожу на %s", catalog_url)
                        continue
                    if not seen_ids:
                        raise RuntimeError("Каталог не отдал товары. Проверьте URL и что страница открылась в браузере.")
                    break
                empty_tries = 0
                empty_advances = 0
                listing_base = page.url if str(page.url).startswith("http") else catalog_url
                fresh = [p for p in products if str(p.get("productId") or "") not in seen_ids]
                if not fresh:
                    logger.info("Страница без новых товаров, стоп")
                    break
                if not sections_saved:
                    save_sections(data, page.url)
                    page_title = (data.get("h1") or "").strip() or page_title
                    folder = category_dir(output_root, page_title, "lemana")
                    sections_saved = True
                logger.info("Страница: новых %s, всего в разделе %s", len(fresh), total)

                pending_cards = []
                skipped_stubs: list[dict[str, Any]] = []
                for product in fresh:
                    pid = str(product.get("productId") or "")
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    if pid in known_ids:
                        stub = listing_to_stub(product, page_url=listing_base, page_title=page_title)
                        apply_site_category(stub, data, page_url=listing_base, page_title=page_title)
                        skipped_stubs.append(stub)
                        continue
                    pending_cards.append(product)
                if skipped_stubs:
                    place_known_products(skipped_stubs)
                    logger.info("Уже в базе %s, категория на сайте обновлена", len(skipped_stubs))
                if skipped_stubs and not pending_cards:
                    logger.info("Страница уже в базе, дальше")
                elif pending_cards:
                    logger.info("Новых на странице %s", len(pending_cards))

                for product in pending_cards:
                    stub = listing_to_stub(product, page_url=listing_base, page_title=page_title)
                    apply_site_category(stub, data, page_url=listing_base, page_title=page_title)
                    pid = stub["product_id"]
                    if args.limit is not None and saved >= args.limit:
                        break
                    try:
                        page, pdp = await read_pdp(page, cloak, stub["url"])
                        row = merge_pdp(stub, pdp)
                    except Exception as exc:
                        if _browser_dead(exc):
                            logger.error(
                                "Браузер закрылся на карточке %s. Её не сохраняю, выхожу для перезапуска.",
                                pid,
                            )
                            raise SystemExit(1) from exc
                        logger.warning("Карточка %s без детализации: %s", pid, exc)
                        row = stub
                        if _page_crashed(exc):
                            page = await reopen_page(cloak, page)
                    if saved and saved % 40 == 0:
                        page = await reopen_page(cloak, page, reason="Обновляю вкладку браузера, чтобы она не упала")
                    if args.download_images and row.get("photo_urls"):
                        saved_files = await download_images(
                            http,
                            row["photo_urls"][:12],
                            folder / "images" / pid,
                            referer=row["url"],
                        )
                        row["local_images"] = "|".join(saved_files)
                    rows.append(row)
                    saved += 1
                    if not args.no_db:
                        persist_lemana_row(row, category_dir=folder)
                    known_ids.add(pid)
                    logger.info("[%s/%s] %s %s", len(seen_ids), total or "?", pid, row.get("name"))
                    if saved % SAVE_EVERY_N == 0:
                        export_products(rows, folder / "products.xlsx")
                    await rate_limit_delay(args.delay)

                if args.limit is not None and saved >= args.limit:
                    break
                if total and len(seen_ids) >= total:
                    break
                next_href = str(data.get("nextHref") or "")
                if next_href:
                    catalog_url = abs_url(listing_base, next_href)
                elif total and len(seen_ids) < total:
                    catalog_url = bump_page(catalog_url)
                else:
                    break
            if not discovered:
                discovered = True
                try:
                    await page.goto("https://lemanapro.ru/catalogue/", wait_until="domcontentloaded", timeout=120000)
                    await page.wait_for_timeout(4000)
                    links = await page.evaluate(DISCOVER_JS)
                except Exception as exc:
                    logger.warning("Не удалось прочитать меню каталога: %s", exc)
                    links = []
                added = 0
                for link in links or []:
                    key = str(link).split("?")[0].rstrip("/")
                    if key and key not in visited_categories and link not in pending:
                        pending.append(link)
                        added += 1
                logger.info("Дальше по каталогу Лемана: %s разделов", added)

    if rows:
        export_products(rows, output_root / "products.xlsx")
    summary = {"url": args.url, "products": len(rows), "db": str(get_repository().db_path)}
    atomic_write_json(output_root / "summary.json", summary)
    logger.info("Готово: %s", summary)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсер Лемана ПРО (Леруа Мерлен)")
    parser.add_argument(
        "--url",
        action="append",
        default=None,
        help="URL раздела. Можно указать несколько: сначала сантехника, потом электрика",
    )
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=None, help="Сколько товаров снять")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY)
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument(
        "--follow-catalogue",
        action="store_true",
        help="После текущего раздела обойти остальные разделы каталога",
    )
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
