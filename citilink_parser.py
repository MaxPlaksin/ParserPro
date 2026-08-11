#!/usr/bin/env python3
"""
Парсер каталога citilink.ru.

Структура:
  output/citilink/{категория}/products.xlsx
  output/citilink/{категория}/checkpoint.json
  output/citilink/checkpoint_global.json
  storage/images/citilink/{source_id}/001.jpg

Источник данных: __NEXT_DATA__.props.pageProps.initialState (Next.js)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
)
from db.hooks import persist_citilink_row
from normalizer.content_filters import clean_html_description, clean_review_noise
from parser_policy import (
    CITILINK_CATEGORY_BLOCK_RETRIES,
    CITILINK_SESSION_RESTARTS,
    apply_citilink_policy,
)
from storage_paths import canonical_image_dir

BASE_URL = "https://www.citilink.ru"
DEFAULT_OUTPUT = "output/citilink"
SAVE_EVERY_N_ROWS = 5
BLOCKED_STATUS_CODES = {403, 429}
PAGE_SETTLE_SEC = 8
PROPERTIES_SETTLE_SEC = 4
LISTING_POLL_SEC = 2
LISTING_POLL_ATTEMPTS = 25
PRODUCT_POLL_ATTEMPTS = 25
FULL_SPEC_MIN_FIELDS = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("citilink_scraper")

EXTRACT_LISTING_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/product/"]')) {
    let href = (a.href || '').split('?')[0].split('#')[0];
    if (!href || href.includes('/otzyvy/') || href.includes('/obzory/')) continue;
    const m = href.match(/\\/product\\/(.+)-(\\d+)\\/?$/);
    if (!m || seen.has(m[2])) continue;
    seen.add(m[2]);
    const name = (a.textContent || '').replace(/\\s+/g, ' ').trim();
    out.push({ id: m[2], slug: m[1], url: href, name });
  }
  const st = window.__NEXT_DATA__?.props?.pageProps?.initialState?.subcategory || {};
  const pl = st.productList?.payload || {};
  const total = pl.total || st.filters?.filter?.foundProductsCount || null;
  const perPage = pl.limit || out.length || 36;
  return { products: out, total, perPage };
}
"""

EXTRACT_CATEGORY_META_JS = """
() => {
  const st = window.__NEXT_DATA__?.props?.pageProps?.initialState?.subcategory || {};
  const cats = st.categories?.payload?.categories || {};
  const category = cats.category || {};
  const parents = cats.parents || [];
  return {
    categoryId: category.id,
    categorySlug: category.slug,
    categoryName: category.name,
    parents,
  };
}
"""

EXTRACT_PRODUCT_JS = """
() => {
  const st = window.__NEXT_DATA__?.props?.pageProps?.initialState || {};
  const pp = st.productPage || {};
  const base = pp.productHeader?.payload?.productBase || null;
  const propsPayload = pp.properties?.payload || null;

  let productLd = null;
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(s.textContent || '');
      if (data && data['@type'] === 'Product') productLd = data;
      if (Array.isArray(data)) {
        for (const item of data) if (item && item['@type'] === 'Product') productLd = item;
      }
    } catch (e) {}
  }

  const domSpecs = [];
  const seen = new Set();
  const main = document.querySelector('main') || document.body;

  const isNoise = (name, value) => {
    if (!name || !value) return true;
    if (name.length > 120 || value.length > 600) return true;
    if (/\\(812\\)|\\+7|магазин|юридическим|конфигуратор|обратная связь/i.test(name + ' ' + value)) return true;
    if (/^(купить|добавить|отзыв|в корзину|сравн|избран)/i.test(name)) return true;
    return false;
  };

  const normalizeName = (name) => (name || '').replace(/:$/, '').replace(/\\s+/g, ' ').trim();
  const normalizeValue = (value) => (value || '').replace(/\\s+/g, ' ').trim();

  const addSpec = (group, name, value) => {
    name = normalizeName(name);
    value = normalizeValue(value);
    if (isNoise(name, value)) return;
    const g = (group || '').trim();
    const key = g + '|' + name + '|' + value;
    if (seen.has(key)) return;
    seen.add(key);
    domSpecs.push({ group: g, name, value });
  };

  const nearestGroup = (el) => {
    let node = el;
    while (node && node !== main) {
      let prev = node;
      while (prev) {
        prev = prev.previousElementSibling;
        if (!prev) break;
        if (/^H[234]$/.test(prev.tagName)) {
          const t = normalizeName(prev.textContent);
          if (t && t.length < 80) return t;
        }
      }
      node = node.parentElement;
    }
    return '';
  };

  main.querySelectorAll('[class*="ProductPropertiesItem"]').forEach(item => {
    const parts = [...item.children];
    if (parts.length >= 2) {
      addSpec(nearestGroup(item), parts[0].textContent, parts[1].textContent);
    }
  });

  main.querySelectorAll('h2, h3, h4').forEach(header => {
    const groupName = normalizeName(header.textContent);
    if (!groupName || groupName.length > 80) return;
    if (!/характерист|экран|памят|процессор|график|накопит|питани|интерфейс|корпус|комплект|общ|устройств|беспровод|вес|габарит/i.test(groupName)) return;
    let el = header.nextElementSibling;
    let steps = 0;
    while (el && steps < 40 && !/^H[234]$/.test(el.tagName)) {
      if (el.children && el.children.length === 2) {
        const cls = (el.className || '').toString();
        if (/Flex|Properties|property|spec/i.test(cls) || el.children[0].textContent.length < 80) {
          addSpec(groupName, el.children[0].textContent, el.children[1].textContent);
        }
      }
      el.querySelectorAll('[class*="Flex"]').forEach(row => {
        if (row.children.length === 2) {
          addSpec(groupName, row.children[0].textContent, row.children[1].textContent);
        }
      });
      el = el.nextElementSibling;
      steps += 1;
    }
  });

  main.querySelectorAll('[class*="Flex"]').forEach(row => {
    if (row.children.length !== 2) return;
    const cls = (row.className || '').toString();
    if (!/component|Properties|property|spec/i.test(cls)) return;
    addSpec(nearestGroup(row), row.children[0].textContent, row.children[1].textContent);
  });

  return { base, propsPayload, productLd, domSpecs, domSpecsCount: domSpecs.length };
}
"""


class BlockedError(Exception):
    """403/429 или страница блокировки."""


@dataclass
class CitilinkCategory:
    category_id: str
    slug: str
    name: str
    path: str = ""
    url: str = ""

    @property
    def cat_key(self) -> str:
        return f"{self.category_id}|{self.slug}"


def parse_product_id_from_url(url: str) -> str:
    m = re.search(r"/product/.+-(\d+)/?", url)
    return m.group(1) if m else ""


def product_url(slug: str, product_id: str) -> str:
    slug = slug.strip("/") or "product"
    return f"{BASE_URL}/product/{slug}-{product_id}/"


def product_properties_url(slug: str, product_id: str) -> str:
    return f"{product_url(slug, product_id).rstrip('/')}/properties/"


def slug_from_product_url(url: str) -> str:
    m = re.search(r"/product/(.+)-(\d+)/?", url)
    return m.group(1) if m else "product"


def category_url(slug: str, *, page: int = 1) -> str:
    slug = slug.strip("/") or "catalog"
    url = f"{BASE_URL}/catalog/{slug}/"
    if page > 1:
        return f"{url}?p={page}"
    return url


def spec_key(group: str, name: str) -> str:
    group = group.strip()
    name = name.strip()
    if group:
        return f"{group} | {name}"
    return name


def citilink_image_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "product-images/" not in raw and "/banners/" in raw:
        return ""
    upgraded = re.sub(r"width:\d+", "width:1200", raw)
    upgraded = re.sub(r"height:\d+", "height:1200", upgraded)
    return upgraded


def pick_image_url(entry: dict[str, Any]) -> str:
    sources = entry.get("sources") or []
    if not isinstance(sources, list):
        return citilink_image_url(str(entry.get("url") or ""))
    preferred = ("XL", "ML", "MD", "SM")
    by_size = {str(s.get("size") or ""): str(s.get("url") or "") for s in sources if isinstance(s, dict)}
    for size in preferred:
        url = by_size.get(size)
        if url:
            return citilink_image_url(url)
    for source in sources:
        if isinstance(source, dict) and source.get("url"):
            return citilink_image_url(str(source["url"]))
    return ""


def collect_product_images(base: dict[str, Any], ld_product: dict[str, Any] | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in base.get("images") or []:
        url = pick_image_url(item if isinstance(item, dict) else {})
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def flatten_property_groups(props_payload: dict[str, Any] | None) -> tuple[dict[str, str], list[dict[str, str]]]:
    specs: dict[str, str] = {}
    groups_out: list[dict[str, str]] = []
    if not isinstance(props_payload, dict):
        return specs, groups_out

    groups = props_payload.get("groups") or props_payload.get("propertyGroups") or []
    if isinstance(groups, dict):
        groups = list(groups.values())
    if not isinstance(groups, list):
        return specs, groups_out

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or group.get("title") or "").strip()
        rows = group.get("properties") or group.get("items") or group.get("data") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("title") or "").strip()
            value = str(row.get("value") or row.get("text") or "").strip()
            measure = str(row.get("measure") or row.get("unit") or "").strip()
            if measure and value and not value.endswith(measure):
                value = f"{value} {measure}".strip()
            if not name or not value:
                continue
            key = spec_key(group_name, name)
            specs[f"spec__{key}"] = value
            groups_out.append({"group": group_name, "name": name, "value": value})
    return specs, groups_out


def flatten_short_properties(items: Any) -> tuple[dict[str, str], list[dict[str, str]]]:
    specs: dict[str, str] = {}
    groups_out: list[dict[str, str]] = []
    if not isinstance(items, list):
        return specs, groups_out
    for row in items:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        value = str(row.get("value") or "").strip()
        measure = str(row.get("measure") or "").strip()
        if measure and value and not value.endswith(measure):
            value = f"{value} {measure}".strip()
        if not name or not value:
            continue
        specs[f"spec__{name}"] = value
        groups_out.append({"group": "", "name": name, "value": value})
    return specs, groups_out


def flatten_dom_specs(items: Any) -> tuple[dict[str, str], list[dict[str, str]]]:
    specs: dict[str, str] = {}
    groups_out: list[dict[str, str]] = []
    if not isinstance(items, list):
        return specs, groups_out
    for row in items:
        if not isinstance(row, dict):
            continue
        group = str(row.get("group") or "").strip()
        name = str(row.get("name") or "").strip()
        value = str(row.get("value") or "").strip()
        if not name or not value:
            continue
        key = spec_key(group, name)
        field = f"spec__{key}"
        if field in specs:
            continue
        specs[field] = value
        groups_out.append({"group": group, "name": name, "value": value})
    return specs, groups_out


def format_category_path(category: dict[str, Any], parents: list[dict[str, Any]], fallback: str) -> str:
    parts: list[str] = []
    for parent in parents or []:
        name = str(parent.get("name") or "").strip()
        if name:
            parts.append(name)
    cat_name = str(category.get("name") or "").strip()
    if cat_name:
        parts.append(cat_name)
    return " > ".join(parts) or fallback


def extract_model(name: str, specs: dict[str, str]) -> str:
    for key, value in specs.items():
        if key.lower().endswith("модель") or key.lower().endswith("| модель"):
            return value
    m = re.search(r"\[([^\]]+)\]\s*$", name or "")
    if m:
        return m.group(1).strip()
    return ""


def parse_availability(base: dict[str, Any], ld_product: dict[str, Any] | None) -> tuple[bool | None, str]:
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
    elif base.get("isAvailable") is True:
        in_stock = True
    elif base.get("isAvailable") is False:
        in_stock = False
    return in_stock, availability


def build_row_from_product(
    base: dict[str, Any],
    *,
    category: CategoryProgress,
    ld_product: dict[str, Any] | None = None,
    props_payload: dict[str, Any] | None = None,
    dom_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    product_id = str(base.get("id") or "")
    slug = str(base.get("slug") or "product")
    url = product_url(slug, product_id)
    name = str(base.get("name") or base.get("shortName") or "")

    spec_fields: dict[str, str] = {}
    spec_groups: list[dict[str, str]] = []

    short_specs, short_groups = flatten_short_properties(base.get("properties"))
    payload_specs, payload_groups = flatten_property_groups(props_payload)
    dom_spec_fields, dom_groups = flatten_dom_specs(dom_specs or [])

    has_full_dom = len(dom_groups) >= FULL_SPEC_MIN_FIELDS
    has_full_payload = len(payload_groups) >= FULL_SPEC_MIN_FIELDS

    if has_full_payload:
        spec_fields.update(short_specs)
        spec_fields.update(payload_specs)
        spec_groups = list(payload_groups)
        if has_full_dom:
            spec_fields.update(dom_spec_fields)
            spec_groups = list(dom_groups)
    elif has_full_dom:
        spec_fields.update(short_specs)
        spec_fields.update(dom_spec_fields)
        spec_groups = list(dom_groups)
    else:
        spec_fields.update(short_specs)
        spec_fields.update(payload_specs)
        spec_fields.update(dom_spec_fields)
        seen_group_items: set[tuple[str, str, str]] = set()
        for item in short_groups + payload_groups + dom_groups:
            key = (item.get("group", ""), item.get("name", ""), item.get("value", ""))
            if key in seen_group_items:
                continue
            seen_group_items.add(key)
            spec_groups.append(item)

    in_stock, availability_status = parse_availability(base, ld_product)

    brand_obj = base.get("brand") or {}
    brand = str(brand_obj.get("name") or brand_obj.get("id") or "").strip()
    if ld_product and not brand:
        brand_obj_ld = ld_product.get("brand")
        if isinstance(brand_obj_ld, dict):
            brand = str(brand_obj_ld.get("name") or "").strip()
        elif brand_obj_ld:
            brand = str(brand_obj_ld).strip()

    vendor_code = str(base.get("searchDescription") or "").strip()
    if ld_product and not vendor_code:
        vendor_code = str(ld_product.get("mpn") or "").strip()

    ld_sku = str(ld_product.get("sku") or "").strip() if ld_product else ""
    sku = ld_sku or product_id

    price_obj = base.get("price") or {}
    sale_price = price_obj.get("current")
    old_price = price_obj.get("old")

    description_detailed = clean_html_description(str(base.get("description") or ""))
    if ld_product and not description_detailed:
        description_detailed = clean_html_description(str(ld_product.get("description") or ""))
    description_short = clean_review_noise(description_detailed)[:5000]
    raw_full_text = clean_review_noise(description_detailed)[:50000]

    category_obj = base.get("category") or {}
    parents = category_obj.get("parents") or []
    category_path = format_category_path(category_obj, parents, category.category_path)

    photo_urls = collect_product_images(base, ld_product)
    model = extract_model(name, spec_fields)

    row: dict[str, Any] = {
        "product_id": product_id,
        "citilink_product_id": product_id,
        "name": name,
        "brand": brand,
        "sku": sku,
        "vendor_code": vendor_code,
        "model": model,
        "category_id": str(category_obj.get("id") or category.category_id),
        "category_name": str(category_obj.get("name") or category.category_name),
        "category_path": category_path or category.category_path,
        "url": url,
        "sale_price": sale_price,
        "old_price": old_price,
        "in_stock": in_stock,
        "availability_status": availability_status,
        "rating_count": (ld_product or {}).get("aggregateRating", {}).get("reviewCount")
        if ld_product
        else None,
        "rating_star": base.get("rating")
        or ((ld_product or {}).get("aggregateRating", {}).get("ratingValue") if ld_product else None),
        "description_short": description_short,
        "description_detailed": description_detailed,
        "raw_full_text": raw_full_text,
        "photo_urls": photo_urls,
        "photo_count": len(photo_urls),
        "spec_count": len(spec_groups),
        "spec_groups": spec_groups,
    }
    if ld_product and ld_product.get("mpn"):
        row["mpn"] = str(ld_product.get("mpn")).strip()
    row.update(spec_fields)
    return row


def build_row_from_listing_item(
    item: dict[str, Any],
    *,
    category: CategoryProgress,
) -> dict[str, Any]:
    product_id = str(item.get("id") or "")
    slug = str(item.get("slug") or "product")
    return {
        "product_id": product_id,
        "citilink_product_id": product_id,
        "name": str(item.get("name") or ""),
        "sku": product_id,
        "category_id": category.category_id,
        "category_name": category.category_name,
        "category_path": category.category_path,
        "url": str(item.get("url") or product_url(slug, product_id)),
    }


def is_blocked(title: str, html: str) -> bool:
    lowered = (title or "").lower()
    if any(x in lowered for x in ("403", "429", "доступ запрещ", "captcha", "robot")):
        return True
    if "cf-browser-verification" in html.lower():
        return True
    return False


class CitilinkScraper:
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
        response = await self.page.goto(url, wait_until="load", timeout=120_000)
        await asyncio.sleep(PAGE_SETTLE_SEC)
        if response and response.status in BLOCKED_STATUS_CODES:
            raise BlockedError(f"HTTP {response.status}: {url}")
        title = await self.page.title()
        html = await self.page.content()
        if is_blocked(title, html):
            raise BlockedError(f"blocked page: {url}")

    async def _wait_listing(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"products": []}
        for _ in range(LISTING_POLL_ATTEMPTS):
            payload = await self.page.evaluate(EXTRACT_LISTING_JS) or {"products": []}
            if payload.get("products"):
                return payload
            await self.page.evaluate("window.scrollBy(0, 700)")
            await asyncio.sleep(LISTING_POLL_SEC)
        return payload

    async def scrape_category_listing(
        self,
        category: CitilinkCategory,
        *,
        max_products: int | None,
        cat_state: CategoryProgress,
    ) -> None:
        collected: set[str] = set(self.checkpoint.completed_ids)
        collected.update(str(p.get("product_id", "")) for p in self.checkpoint.pending)

        page_num = max(1, int(cat_state.list_page or 1))
        list_complete = bool(cat_state.list_complete)
        per_page = 36

        while not list_complete:
            if max_products and len(collected) >= max_products:
                list_complete = True
                break

            url = category_url(category.slug, page=page_num)
            await self._goto_safe(url)
            if page_num == 1:
                meta = await self.page.evaluate(EXTRACT_CATEGORY_META_JS) or {}
                if meta.get("categoryName"):
                    cat_state.category_name = str(meta["categoryName"])
                if meta.get("categoryId"):
                    cat_state.category_id = str(meta["categoryId"])
                parents = meta.get("parents") or []
                path = format_category_path(
                    {"name": cat_state.category_name},
                    parents,
                    cat_state.category_path,
                )
                if path:
                    cat_state.category_path = path

            payload = await self._wait_listing()
            products = payload.get("products") or []
            total = payload.get("total")
            per_page = int(payload.get("perPage") or per_page or 36)

            if not products:
                list_complete = True
                break

            new_count = 0
            for item in products:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or "")
                if not pid or pid in collected:
                    continue
                if max_products and len(collected) >= max_products:
                    break
                slug = str(item.get("slug") or "product")
                self.checkpoint.pending.append(
                    {
                        "product_id": pid,
                        "slug": slug,
                        "url": str(item.get("url") or product_url(slug, pid)),
                        "name": item.get("name") or "",
                    }
                )
                collected.add(pid)
                new_count += 1

            logger.info(
                "Citilink listing %s page=%s products=%s new=%s pending=%s total=%s",
                category.slug,
                page_num,
                len(products),
                new_count,
                len(self.checkpoint.pending),
                total,
            )

            if max_products and len(collected) >= max_products:
                list_complete = True
                break

            if total is not None and page_num * per_page >= int(total):
                list_complete = True
                break
            if len(products) < max(1, per_page // 2):
                list_complete = True
                break

            page_num += 1
            cat_state.list_page = page_num

        cat_state.list_complete = list_complete

    async def _goto_properties_safe(self, url: str) -> None:
        await self.rate_limiter.wait()
        response = await self.page.goto(url, wait_until="load", timeout=120_000)
        await asyncio.sleep(PAGE_SETTLE_SEC)
        if response and response.status in BLOCKED_STATUS_CODES:
            raise BlockedError(f"HTTP {response.status}: {url}")
        title = await self.page.title()
        html = await self.page.content()
        if is_blocked(title, html):
            raise BlockedError(f"blocked page: {url}")
        for _ in range(4):
            await self.page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(1)

    async def _extract_product_payload(self) -> dict[str, Any]:
        extracted: dict[str, Any] = {}
        for _ in range(PRODUCT_POLL_ATTEMPTS):
            extracted = await self.page.evaluate(EXTRACT_PRODUCT_JS) or {}
            dom_count = int(extracted.get("domSpecsCount") or len(extracted.get("domSpecs") or []))
            props_payload = extracted.get("propsPayload")
            props_len = 0
            if isinstance(props_payload, dict):
                groups = props_payload.get("groups") or props_payload.get("propertyGroups") or []
                if isinstance(groups, list):
                    for group in groups:
                        rows = group.get("properties") or group.get("items") or group.get("data") or []
                        if isinstance(rows, list):
                            props_len += len(rows)
            if dom_count >= FULL_SPEC_MIN_FIELDS or props_len >= FULL_SPEC_MIN_FIELDS:
                return extracted
            if extracted.get("base") and dom_count > 0:
                return extracted
            await self.page.evaluate("window.scrollBy(0, 500)")
            await asyncio.sleep(2)
        return extracted

    async def scrape_product_detail(
        self,
        item: dict[str, Any],
        category: CategoryProgress,
        cat_dir: Path,
    ) -> dict[str, Any] | None:
        slug = str(item.get("slug") or slug_from_product_url(str(item.get("url") or "")))
        pid = str(item.get("product_id") or parse_product_id_from_url(str(item.get("url") or "")))
        if not pid:
            raise RuntimeError(f"missing product_id: {item}")
        properties_url = product_properties_url(slug, pid)
        product_page_url = product_url(slug, pid)

        await self._goto_properties_safe(properties_url)
        extracted = await self._extract_product_payload()

        base = extracted.get("base")
        if not base:
            await self._goto_safe(product_page_url)
            extracted = await self._extract_product_payload()
            base = extracted.get("base")
        if not base:
            raise RuntimeError(f"productPage.productHeader missing: {properties_url}")

        row = build_row_from_product(
            base,
            category=category,
            ld_product=extracted.get("productLd"),
            props_payload=extracted.get("propsPayload"),
            dom_specs=extracted.get("domSpecs"),
        )
        row["spec_source"] = (
            "payload"
            if len(flatten_property_groups(extracted.get("propsPayload"))[1]) >= FULL_SPEC_MIN_FIELDS
            else "dom"
            if len(row.get("spec_groups") or []) >= FULL_SPEC_MIN_FIELDS
            else "short"
        )
        logger.info(
            "Citilink specs %s: count=%s source=%s",
            pid,
            row.get("spec_count"),
            row.get("spec_source"),
        )

        if self.download_images_flag and row.get("photo_urls"):
            img_dir = canonical_image_dir("citilink", row["product_id"])
            prefix = f"storage/images/citilink/{row['product_id']}"
            local = await download_images(
                self.http,
                list(row["photo_urls"]),
                img_dir,
                referer=product_page_url,
                path_prefix=prefix,
            )
            row["local_images"] = " | ".join(local)
            row["local_image_count"] = len(local)

        persist_citilink_row(row, cat_dir)
        row.pop("photo_urls", None)
        return row

    async def process_pending(
        self,
        category: CitilinkCategory,
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
                logger.warning("Citilink detail failed %s: %s", pid, exc)
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
                "Citilink detail progress: %s done, %s pending",
                len(self.checkpoint.rows),
                len(self.checkpoint.pending),
            )

        if self.checkpoint.rows:
            export_products(self.checkpoint.rows, cat_dir / "products.xlsx")
        self.checkpoint.save(checkpoint_path)

    async def scrape_category(
        self,
        category: CitilinkCategory,
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
        if cat_state.list_complete and not self.checkpoint.pending and not self.checkpoint.rows:
            cat_state.list_complete = False
            cat_state.list_page = 1
        if not cat_state.list_complete:
            await self.scrape_category_listing(category, max_products=max_products, cat_state=cat_state)
            self.checkpoint.save(checkpoint_path)

        await self.process_pending(category, cat_dir, checkpoint_path, max_products=max_products)

    async def respec_products(
        self,
        items: list[dict[str, Any]],
        category: CitilinkCategory,
        cat_dir: Path,
    ) -> None:
        cat_state = CategoryProgress(
            category_id=category.category_id,
            category_name=category.name,
            category_path=category.path or category.name,
            category_slug=category.slug,
            category_url=category.url,
        )
        for index, item in enumerate(items, 1):
            pid = str(item.get("product_id") or "")
            try:
                row = await self.scrape_product_detail(item, cat_state, cat_dir)
                if row:
                    logger.info("Citilink respec %s/%s done: %s fields=%s", index, len(items), pid, row.get("spec_count"))
            except BlockedError:
                raise
            except Exception as exc:
                self.failed += 1
                logger.warning("Citilink respec failed %s: %s", pid, exc)


def load_respec_items(limit: int | None = None) -> list[dict[str, Any]]:
    import sqlite3

    from storage_paths import PROJECT_ROOT

    db_path = PROJECT_ROOT / "data" / "products.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    sql = "SELECT source_id, url FROM products WHERE source='citilink' ORDER BY id"
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    items: list[dict[str, Any]] = []
    for source_id, url in conn.execute(sql, params):
        url = str(url or "")
        items.append(
            {
                "product_id": str(source_id),
                "url": url,
                "slug": slug_from_product_url(url),
            }
        )
    conn.close()
    return items


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

            scraper = CitilinkScraper(
                page,
                GlobalCheckpoint(shop="citilink", output_root=str(output_root)),
                http_session=http,
                rate_limiter=rate_limiter,
                download_images_flag=not args.skip_images,
                category_delay=args.category_delay,
            )

            if not args.category_slug and not args.respec:
                raise SystemExit("Citilink parser requires --category-slug (e.g. noutbuki) or --respec")

            categories = [
                CitilinkCategory(
                    category_id=str(args.category or ""),
                    slug=args.category_slug,
                    name=args.category_name or args.category_slug.replace("-", " "),
                    path=args.category_path or args.category_name or args.category_slug,
                    url=category_url(args.category_slug),
                )
            ]

            if args.respec:
                items = load_respec_items(args.limit)
                if not items:
                    raise SystemExit("No citilink products in DB for --respec")
                category = categories[0]
                cat_dir = category_dir(
                    output_root,
                    category.path or category.name,
                    category.category_id or category.slug,
                )
                logger.info("Citilink respec mode: %s products", len(items))
                await scraper.respec_products(items, category, cat_dir)
                stats["failed"] += scraper.failed
                return stats

            for index, category in enumerate(categories, 1):
                if category.cat_key in completed_categories:
                    logger.info("Skip completed category: %s", category.slug)
                    continue

                cat_dir = category_dir(
                    output_root,
                    category.path or category.name,
                    category.category_id or category.slug,
                )
                cp_path = cat_dir / "checkpoint.json"
                checkpoint = GlobalCheckpoint.load(cp_path)
                if checkpoint is None:
                    checkpoint = GlobalCheckpoint(shop="citilink", output_root=str(output_root))

                scraper.checkpoint = checkpoint
                scraper.failed = 0

                logger.info("[%s/%s] Citilink %s", index, len(categories), category.url)
                cat_backoff = BlockBackoff(max_attempts=CITILINK_CATEGORY_BLOCK_RETRIES)
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
                if category_done and args.limit:
                    cat_state = checkpoint.category
                    if cat_state and not cat_state.list_complete:
                        logger.warning(
                            "Category %s partial run (%s products, limit=%s, list_complete=false) — not marking complete",
                            category.slug,
                            processed,
                            args.limit,
                        )
                        category_done = False
                if category_done and args.limit and processed < args.limit and checkpoint.pending:
                    logger.warning(
                        "Category %s finished with %s/%s products — not marking complete",
                        category.slug,
                        processed,
                        args.limit,
                    )
                    category_done = False
                if category_done and processed > 0 and (
                    not args.limit or (checkpoint.category and checkpoint.category.list_complete)
                ):
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
    session_backoff = BlockBackoff(max_attempts=CITILINK_SESSION_RESTARTS)
    while True:
        try:
            return await scrape_once(args)
        except BlockedError as exc:
            if not await session_backoff.wait_before_retry():
                raise
            logger.warning("Restart Citilink session after block: %s", exc)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Парсер citilink.ru")
    p.add_argument("-o", "--output", default=DEFAULT_OUTPUT)
    p.add_argument("--category", help="ID категории (numeric, optional)")
    p.add_argument("--category-slug", required=True, help="SEO slug категории, напр. noutbuki")
    p.add_argument("--category-name", default="", help="Название категории")
    p.add_argument("--category-path", default="", help="Путь категории")
    p.add_argument("--limit", type=int, help="Лимит товаров на категорию")
    p.add_argument("--respec", action="store_true", help="Пересканировать характеристики существующих товаров citilink из БД")
    p.add_argument("--skip-images", action="store_true")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--delay", type=float, default=4.0)
    p.add_argument("--category-delay", type=float, default=20.0)
    p.add_argument("--workers", type=int, default=1)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    apply_citilink_policy(args)
    stats = asyncio.run(run_with_retries(args))
    logger.info("Citilink run finished: failed=%s deferred=%s", stats.get("failed"), stats.get("deferred"))


if __name__ == "__main__":
    main()
