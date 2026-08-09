#!/usr/bin/env python3
"""
Парсер каталога dns-shop.ru.

Структура выгрузки:
  output/dns/{категория}/products.xlsx
  output/dns/{категория}/images/{product_id}/001.jpg
  output/dns/checkpoint_global.json

Правила запуска (не менять): parser_policy.py
  - всегда видимый браузер (headless запрещён)
  - прогрев через главную перед категорией
  - _goto_safe: при 403 прогрев + один повтор
  - atomic checkpoint (.tmp + os.replace)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

from common import (
    BlockBackoff,
    CategoryProgress,
    GlobalCheckpoint,
    CloakSession,
    DomainRateLimiter,
    atomic_write_json,
    category_dir,
    download_images,
    export_products,
    rate_limit_delay,
    safe_name,
)
from normalizer.content_filters import clean_review_noise, is_skipped_product_path
from normalizer.structured import build_description_full, pick_spec_value
from parser_policy import (
    DNS_CATEGORY_BLOCK_RETRIES,
    DNS_PAGE_SETTLE_SEC,
    DNS_SESSION_RESTARTS,
    DNS_WARMUP_DELAY_SEC,
    apply_dns_policy,
)

BASE_URL = "https://www.dns-shop.ru"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
REQUEST_DELAY = 5.0
CATEGORY_DELAY = 30.0
PAGE_SETTLE_DELAY = DNS_PAGE_SETTLE_SEC
INITIAL_WARMUP_DELAY = DNS_WARMUP_DELAY_SEC
INITIAL_RETRY_DELAY = 30
MAX_RETRY_DELAY = 300
DEFAULT_OUTPUT = "output/dns"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("dns_scraper")

PRODUCT_LINK_RE = re.compile(r'href="(/product/[a-f0-9]{16}/[^"?#]+)"')
LD_JSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
BREADCRUMB_RE = re.compile(r'itemprop="name"[^>]*>([^<]+)<')
IMAGE_RE = re.compile(r'https://c\.dns-shop\.ru/[^"\']+\.(?:webp|jpg|jpeg|png|gif)', re.I)
GIF_RE = re.compile(r'https://c\.dns-shop\.ru/[^"\']+\.gif(?:\?[^"\']*)?', re.I)
SPEC_ROW_RE = re.compile(
    r'<div class="product-characteristics__spec-title[^"]*"[^>]*>\s*([^<]+?)\s*</div>\s*'
    r'<div class="product-characteristics__spec-value[^"]*"[^>]*>\s*([^<]+?)\s*</div>',
    re.S,
)
SHORT_SPEC_RE = re.compile(
    r'product-card-top__specs-item-title">\s*([^:<]+?):?\s*</div>\s*'
    r'<div class="product-card-top__specs-item-content"[^>]*(?:title="([^"]*)")?[^>]*>\s*([^<]+?)\s*</div>',
    re.S,
)
IMAGE_HASH_RE = re.compile(r"/([a-f0-9]{64})\.(webp|jpg|jpeg|png|gif)", re.I)
OLD_PRICE_RE = re.compile(
    r'product-buy__price-old[^>]*>\s*([\d\s]+)\s*(?:₽|&#8381;|руб)',
    re.I,
)

FETCH_MICRODATA_JS = """
async () => {
    const url = window.cardMicrodataUrl;
    if (!url) return null;
    const response = await fetch(url, { credentials: 'include' });
    if (!response.ok) return null;
    const payload = await response.json();
    return payload.data || null;
}
"""

SCRAPE_RELIABILITY_JS = """
() => {
    const textOrEmpty = (el) => (el && el.innerText ? el.innerText.trim() : '');
    const selectors = [
        '.product-card-description-service-rating',
        '.product-card-top-service-rating',
        '.product-buy__service-rating',
        '[class*="service-rating"]',
    ];
    let reliabilityBlock = '';
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el) {
            reliabilityBlock = textOrEmpty(el);
            if (reliabilityBlock) break;
        }
    }
    let reliabilityPercent = null;
    const percentMatch = reliabilityBlock.match(/(\\d+[.,]?\\d*)\\s*%/);
    if (percentMatch) {
        reliabilityPercent = parseFloat(percentMatch[1].replace(',', '.'));
    }
    const reliabilityText = reliabilityBlock
        .replace(/Отличная надежность/gi, '')
        .replace(/\\d+[.,]?\\d*\\s*%/g, '')
        .replace(/\\s+/g, ' ')
        .trim();
    return { reliabilityPercent, reliabilityText };
}
"""

SCRAPE_CHARACTERISTICS_PAGE_JS = """
async () => {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const textOrEmpty = (el) => (el ? el.innerText.trim() : '');

    const descriptionRoot = document.querySelector('.product-card-description');
    if (descriptionRoot) {
        for (const btn of [...descriptionRoot.querySelectorAll('a, button, span, div')].filter(
            (el) => /развер/i.test(el.textContent) && el.textContent.trim().length < 40
        )) {
            try {
                btn.click();
                await sleep(700);
            } catch (e) {}
        }
    }
    await sleep(500);

    const marketingText = textOrEmpty(
        document.querySelector(
            '.product-card-description__text-description, .product-card-description__text, .product-card-description-text'
        )
    );

    let rawFullText = '';
    const mainEl = document.querySelector('.product-card-description__main');
    if (mainEl) {
        const clone = mainEl.cloneNode(true);
        clone.querySelectorAll(
            '[class*="review"], [class*="opinion"], [href*="opinion"], [href*="review"]'
        ).forEach((el) => el.remove());
        rawFullText = clone.innerText.trim();
    } else {
        rawFullText = marketingText;
    }

    const reliabilityBlock = textOrEmpty(document.querySelector('.product-card-description-service-rating'));
    let reliabilityPercent = null;
    const percentMatch = reliabilityBlock.match(/(\\d+[.,]?\\d*)\\s*%/);
    if (percentMatch) {
        reliabilityPercent = parseFloat(percentMatch[1].replace(',', '.'));
    }
    const reliabilityText = reliabilityBlock
        .replace(/Отличная надежность/gi, '')
        .replace(/\\d+[.,]?\\d*\\s*%/g, '')
        .replace(/\\s+/g, ' ')
        .trim();

    const priceHistoryText = textOrEmpty(document.querySelector('.product-card-description-price-history'));
    let priceHistoryMin = null;
    let priceHistoryMax = null;
    const prices = [...priceHistoryText.matchAll(/(\\d[\\d\\s]{2,})/g)]
        .map((m) => parseInt(m[1].replace(/\\s+/g, ''), 10))
        .filter((n) => !Number.isNaN(n));
    if (prices.length) {
        priceHistoryMin = Math.min(...prices);
        priceHistoryMax = Math.max(...prices);
    }

    const equipmentText = textOrEmpty(document.querySelector('.product-card-description-equipment'));
    const dimensionsText = textOrEmpty(document.querySelector('.product-card-description-dimensions'));

    let displayText = '';
    document.querySelectorAll('.product-card-description-specs-item, .product-card-top__specs-item').forEach((item) => {
        const title = textOrEmpty(item.querySelector('.product-card-description-specs-item-title, .product-card-top__specs-item-title'));
        const value = textOrEmpty(item.querySelector('.product-card-description-specs-item-content, .product-card-top__specs-item-content'));
        if (/дисплей/i.test(title)) {
            displayText = value || title.replace(/:/g, '').trim();
        }
    });

    const characteristicsRoot = document.querySelector('.product-characteristics');
    if (characteristicsRoot) {
        for (const btn of [...characteristicsRoot.querySelectorAll('a, button, span, div')].filter(
            (el) => /развер/i.test(el.textContent) && el.textContent.trim().length < 40
        )) {
            try {
                btn.click();
                await sleep(700);
            } catch (e) {}
        }
    }

    const grouped = [];
    document.querySelectorAll('.product-characteristics__group').forEach((group) => {
        const groupEl = group.querySelector('.product-characteristics__group-title');
        const groupName = groupEl ? groupEl.textContent.trim() : '';
        group.querySelectorAll('.product-characteristics__spec').forEach((row) => {
            const titleEl = row.querySelector('.product-characteristics__spec-title');
            const valueEl = row.querySelector('.product-characteristics__spec-value');
            if (titleEl && valueEl) {
                grouped.push([groupName, titleEl.textContent.trim(), valueEl.textContent.trim()]);
            }
        });
    });
    if (!grouped.length) {
        document.querySelectorAll('.product-characteristics__spec').forEach((row) => {
            const titleEl = row.querySelector('.product-characteristics__spec-title');
            const valueEl = row.querySelector('.product-characteristics__spec-value');
            if (titleEl && valueEl) {
                grouped.push(['', titleEl.textContent.trim(), valueEl.textContent.trim()]);
            }
        });
    }

    return {
        grouped,
        marketingText,
        rawFullText,
        reliabilityPercent,
        reliabilityText,
        equipmentText,
        dimensionsText,
        displayText,
        priceHistoryText,
        priceHistoryMin,
        priceHistoryMax,
    };
}
"""


class BlockedError(Exception):
    pass


@dataclass
class DnsCategory:
    category_id: str
    slug: str
    url: str
    name: str = ""
    path: str = ""


@dataclass
class DnsProductRef:
    product_id: str
    url: str
    slug: str = ""


from parser_policy import (
    DNS_CATEGORY_BLOCK_RETRIES,
    DNS_PAGE_SETTLE_SEC,
    DNS_SESSION_RESTARTS,
    DNS_WARMUP_DELAY_SEC,
    apply_dns_policy,
)


async def warmup_dns_session(page, *, label: str = "") -> None:
    """Прогрев cookies/сессии через главную — без этого DNS часто отдаёт 403."""
    if label:
        logger.info("DNS прогрев сессии (%s)", label)
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
    await rate_limit_delay(INITIAL_WARMUP_DELAY)
    try:
        await page.evaluate(
            """() => {
                window.scrollBy(0, Math.min(500, document.body?.scrollHeight || 500));
            }"""
        )
    except Exception:
        pass
    await rate_limit_delay(2)


def is_blocked(title: str, html: str) -> bool:
    t = (title or "").lower().strip()
    if "http 403" in t or "http 401" in t or t in {"403", "401", "ошибка"}:
        return True
    if any(x in t for x in ("captcha", "challenge", "robot", "blocked", "access denied")):
        return True
    if len(html) < 8000 and ("captcha" in html.lower() or "challenge" in html.lower()):
        return True
    return False


def normalize_image(url: str) -> str:
    return url


def image_dedupe_key(url: str) -> str:
    match = IMAGE_HASH_RE.search(url)
    return match.group(1) if match else url


def extract_gif_urls(html: str) -> list[str]:
    return list(dict.fromkeys(GIF_RE.findall(html)))


def extract_all_images(html: str, ld_images: list[str] | None = None) -> list[str]:
    found = list(ld_images or [])
    found.extend(IMAGE_RE.findall(html))
    ordered: list[str] = []
    seen: set[str] = set()
    for url in found:
        if url.lower().endswith(".gif"):
            continue
        normalized = normalize_image(url)
        key = image_dedupe_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    return ordered


def parse_short_specs(html: str) -> dict[str, str]:
    specs: dict[str, str] = {}
    for title, title_attr, content in SHORT_SPEC_RE.findall(html):
        key = safe_name(re.sub(r"\s+", " ", title).strip(" :"), max_len=80)
        value = re.sub(r"\s+", " ", (title_attr or content)).strip()
        if key and value:
            specs[key] = value
    return specs


def characteristics_url(product_url: str) -> str:
    path = characteristics_path(product_url)
    return path if path.startswith("http") else urljoin(BASE_URL, path)


def clean_description_text(text: str) -> str:
    return clean_review_noise(text)


def characteristics_path(product_url: str) -> str:
    return product_url.replace("/product/", "/product/characteristics/", 1)


def spec_key(group: str, name: str, *, prefix: str = "") -> str:
    parts = [p for p in (prefix, group, name) if p]
    return safe_name(" | ".join(parts), max_len=100)


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
        resp.raise_for_status()
        return await resp.text()


async def load_category_sitemap(session: aiohttp.ClientSession) -> list[DnsCategory]:
    index_xml = await fetch_text(session, f"{BASE_URL}/sitemap.xml")
    root = ET.fromstring(index_xml)
    sitemap_locs = [
        loc.text.strip()
        for loc in root.findall(".//sm:loc", SITEMAP_NS)
        if loc.text and "sitemap-category" in loc.text
    ]
    categories: list[DnsCategory] = []
    seen: set[str] = set()
    for sm_url in sitemap_locs:
        xml = await fetch_text(session, sm_url)
        sm_root = ET.fromstring(xml)
        for loc in sm_root.findall(".//sm:loc", SITEMAP_NS):
            if not loc.text:
                continue
            url = loc.text.strip()
            m = re.search(r"/catalog/([a-f0-9]{16})/([^/]+)/?", url)
            if not m:
                continue
            cid, slug = m.group(1), m.group(2)
            if cid in seen:
                continue
            seen.add(cid)
            categories.append(DnsCategory(category_id=cid, slug=slug, url=url))
    return categories


def parse_category_meta(html: str, fallback: DnsCategory) -> tuple[str, str]:
    title_m = re.search(r"<title>([^<]+)</title>", html, re.I)
    title = title_m.group(1).split(" - ")[0].strip() if title_m else fallback.slug.replace("-", " ").title()
    crumbs = BREADCRUMB_RE.findall(html)
    if crumbs:
        path = " > ".join(c.strip() for c in crumbs)
        name = crumbs[-1].strip()
        return name, path
    name = title.split(" купить")[0].strip() or fallback.slug
    return name, name


def extract_product_refs(html: str) -> list[DnsProductRef]:
    refs: list[DnsProductRef] = []
    seen: set[str] = set()
    for path in PRODUCT_LINK_RE.findall(html):
        if is_skipped_product_path(path):
            continue
        m = re.search(r"/product/([a-f0-9]{16})/", path)
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        refs.append(DnsProductRef(product_id=pid, url=urljoin(BASE_URL, path), slug=path))
    return refs


def parse_product_page(
    html: str,
    ref: DnsProductRef,
    category: CategoryProgress,
    *,
    microdata: dict[str, Any] | None = None,
    page_data: dict[str, Any] | None = None,
    media_3d: list[str] | None = None,
) -> dict[str, Any]:
    page_data = page_data or {}
    grouped_specs: list[list[str]] = page_data.get("grouped") or []
    ld_blocks = LD_JSON_RE.findall(html)
    product_ld: dict[str, Any] = {}
    for block in ld_blocks:
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            product_ld = data
            break

    product_data = microdata or product_ld
    offers = product_data.get("offers") or product_ld.get("offers") or {}
    rating = product_data.get("aggregateRating") or product_ld.get("aggregateRating") or {}
    brand = product_data.get("brand") or product_ld.get("brand") or {}
    ld_images = product_data.get("image") or product_ld.get("image") or []
    if isinstance(ld_images, str):
        ld_images = [ld_images]
    images = extract_all_images(html, ld_images)
    gif_urls = extract_gif_urls(html)

    specs: dict[str, str] = {}
    for key, val in SPEC_ROW_RE.findall(html):
        specs[spec_key("", key)] = re.sub(r"\s+", " ", val).strip()
    specs.update({spec_key("Кратко", k, prefix=""): v for k, v in parse_short_specs(html).items()})
    for group, name, value in grouped_specs or []:
        clean_value = re.sub(r"\s+", " ", value).strip()
        specs[spec_key(group, name)] = clean_value

    availability = offers.get("availability", "")
    in_stock = "InStock" in availability or "instock" in availability.lower()
    description_short = clean_description_text(
        product_data.get("description") or product_ld.get("description") or ""
    )[:5000]
    description_detailed = clean_description_text(page_data.get("marketingText") or "")[:20000]
    raw_full_text = clean_description_text(page_data.get("rawFullText") or "")[:50000]
    reliability_percent = page_data.get("reliabilityPercent")
    reliability_text = clean_description_text(page_data.get("reliabilityText") or "")
    equipment = clean_description_text(page_data.get("equipmentText") or "")
    dimensions = clean_description_text(page_data.get("dimensionsText") or "")
    display = clean_description_text(page_data.get("displayText") or "") or pick_spec_value(specs, "Дисплей", "display")
    price_history_text = clean_description_text(page_data.get("priceHistoryText") or "")
    price_history_min = page_data.get("priceHistoryMin")
    price_history_max = page_data.get("priceHistoryMax")

    row_for_full = {
        "description_detailed": description_detailed,
        "display": display,
        "equipment": equipment,
        "dimensions": dimensions,
        "reliability_text": reliability_text,
        "reliability_percent": reliability_percent,
        "price_history_text": price_history_text,
        "_grouped_specs": grouped_specs,
        "characteristics": specs,
    }
    description_full = build_description_full(row_for_full)[:50000]
    description = description_full or description_detailed or description_short
    old_price_match = OLD_PRICE_RE.search(html)
    old_price = None
    if old_price_match:
        old_price = int(re.sub(r"\s+", "", old_price_match.group(1)))

    product_uuid = ""
    micro_url_match = re.search(r"/product/microdata/([a-f0-9-]{36})/", html)
    if micro_url_match:
        product_uuid = micro_url_match.group(1)

    row = {
        "product_id": ref.product_id,
        "product_uuid": product_uuid,
        "name": product_data.get("name") or product_ld.get("name", ""),
        "brand": brand.get("name", "") if isinstance(brand, dict) else str(brand),
        "sku": product_data.get("sku") or product_ld.get("sku", ""),
        "category_id": category.category_id,
        "category_name": category.category_name,
        "category_path": category.category_path,
        "url": ref.url,
        "sale_price": offers.get("price"),
        "old_price": old_price,
        "price_currency": offers.get("priceCurrency", "RUB"),
        "in_stock": in_stock,
        "rating_count": rating.get("reviewCount"),
        "rating_star": rating.get("ratingValue"),
        "description": description,
        "description_short": description_short,
        "description_detailed": description_detailed,
        "description_full": description_full,
        "raw_full_text": raw_full_text,
        "reliability_percent": reliability_percent,
        "reliability_text": reliability_text,
        "display": display,
        "dimensions": dimensions,
        "equipment": equipment,
        "price_history_text": price_history_text,
        "price_min_observed": price_history_min,
        "price_max_observed": price_history_max,
        "price_history_min": price_history_min,
        "price_history_max": price_history_max,
        "description_len": len(description_full or description),
        "photos": " | ".join(images),
        "photo_count": len(images),
        "photo_urls": images,
        "gif_urls": gif_urls,
        "gif_count": len(gif_urls),
        "characteristics": "; ".join(f"{k}: {v}" for k, v in specs.items()),
        "spec_count": len(specs),
    }
    if media_3d:
        row["media_3d"] = " | ".join(media_3d)
        row["media_3d_count"] = len(media_3d)
    row.update({f"spec__{k}": v for k, v in specs.items()})
    return row


class DnsScraper:
    def __init__(
        self,
        page,
        checkpoint: GlobalCheckpoint,
        *,
        http_session: aiohttp.ClientSession,
        rate_limiter: DomainRateLimiter,
        category_delay: float = CATEGORY_DELAY,
        download_images_flag: bool = True,
    ):
        self.page = page
        self.checkpoint = checkpoint
        self.http = http_session
        self.rate_limiter = rate_limiter
        self.category_delay = category_delay
        self.download_images_flag = download_images_flag

    async def _goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 90000):
        await self.rate_limiter.wait()
        return await self.page.goto(url, wait_until=wait_until, timeout=timeout_ms)

    async def _goto_safe(self, url: str) -> Any:
        """Navigate with one recovery attempt via homepage warmup."""
        for attempt in range(2):
            try:
                await self._goto(url)
                await rate_limit_delay(PAGE_SETTLE_DELAY)
                title = await self.page.title()
                html = await self.page.content()
                if not is_blocked(title, html):
                    return
                logger.warning("DNS block на %s — прогрев главной и повтор", url)
            except Exception as exc:
                logger.warning("DNS navigation issue on %s: %s", url, exc)
            if attempt == 0:
                await warmup_dns_session(self.page, label="recovery")
        raise BlockedError(f"Блокировка/таймаут: {url}")

    async def _fetch_microdata(self) -> dict[str, Any] | None:
        try:
            data = await self.page.evaluate(FETCH_MICRODATA_JS)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("DNS microdata недоступен: %s", exc)
            return None

    async def _fetch_reliability(self) -> dict[str, Any]:
        try:
            payload = await self.page.evaluate(SCRAPE_RELIABILITY_JS)
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning("DNS reliability недоступен: %s", exc)
            return {}

    @staticmethod
    def _merge_page_data(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key in ("reliabilityPercent", "reliabilityText", "displayText", "equipmentText", "dimensionsText"):
            if not merged.get(key) and extra.get(key):
                merged[key] = extra[key]
        return merged

    async def _scrape_characteristics_page(self, product_url: str) -> dict[str, Any]:
        char_url = characteristics_url(product_url)
        empty: dict[str, Any] = {"grouped": []}
        try:
            await self._goto_safe(char_url)
            title = await self.page.title()
            html = await self.page.content()
            if is_blocked(title, html):
                logger.warning("DNS block на странице описания: %s", char_url)
                return empty
            payload = await self.page.evaluate(SCRAPE_CHARACTERISTICS_PAGE_JS)
            if not isinstance(payload, dict):
                return empty
            grouped_raw = payload.get("grouped") or []
            payload["grouped"] = [
                item for item in grouped_raw if isinstance(item, list) and len(item) >= 2
            ]
            return payload
        except Exception as exc:
            logger.warning("DNS подробное описание недоступно для %s: %s", product_url, exc)
            return empty

    async def _fetch_media_3d(self, product_uuid: str) -> list[str]:
        if not product_uuid:
            return []
        api_url = f"{BASE_URL}/catalog/product/get-three-dimensional-image/?id={product_uuid}"
        try:
            await self.rate_limiter.wait()
            payload = await self.page.evaluate(
                """async (url) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 30000);
                    try {
                        const response = await fetch(url, { credentials: 'include', signal: controller.signal });
                        if (!response.ok) return null;
                        return await response.json();
                    } finally {
                        clearTimeout(timer);
                    }
                }""",
                api_url,
            )
        except Exception as exc:
            logger.warning("DNS 3D media недоступен: %s", exc)
            return []
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") or {}
        urls: list[str] = []
        tabs = data.get("tabs") or []
        if isinstance(tabs, list):
            for tab in tabs:
                if not isinstance(tab, dict):
                    continue
                for key in ("images", "items", "frames"):
                    items = tab.get(key) or []
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, str):
                                urls.append(item)
                            elif isinstance(item, dict):
                                for img_key in ("url", "src", "image", "preview"):
                                    if item.get(img_key):
                                        urls.append(str(item[img_key]))
        return list(dict.fromkeys(normalize_image(u) for u in urls if u))

    async def scrape_category(
        self,
        category: DnsCategory,
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
            )
            self.checkpoint.pending = []
            self.checkpoint.completed_ids = []
            self.checkpoint.rows = []

        cat_state = self.checkpoint.category
        page_num = cat_state.list_page
        collected_ids: set[str] = set(self.checkpoint.completed_set)

        while not cat_state.list_complete:
            page_url = category.url if page_num <= 1 else f"{category.url.rstrip('/')}/?p={page_num}"
            logger.info("Категория %s — страница %s", cat_state.category_name or category.slug, page_num)
            response = await self._goto_safe(page_url)
            html = await self.page.content()
            title = await self.page.title()
            if is_blocked(title, html):
                logger.warning("DNS block? title=%r html_len=%s", title[:80], len(html))
                raise BlockedError(f"Блокировка: {page_url}")

            if page_num == 1 or not cat_state.category_name:
                name, path = parse_category_meta(html, category)
                cat_state.category_name = name
                cat_state.category_path = path
                category.name, category.path = name, path

            refs = extract_product_refs(html)
            if not refs:
                cat_state.list_complete = True
                break

            new_refs = [r for r in refs if r.product_id not in collected_ids]
            for ref in new_refs:
                if max_products and len(collected_ids) >= max_products:
                    cat_state.list_complete = True
                    break
                if ref.product_id in self.checkpoint.completed_set:
                    continue
                if any(p.get("product_id") == ref.product_id for p in self.checkpoint.pending):
                    continue
                self.checkpoint.pending.append({"product_id": ref.product_id, "url": ref.url, "slug": ref.slug})
                collected_ids.add(ref.product_id)

            if cat_state.list_complete:
                break

            total_pages_m = re.search(r"страница\s+\d+\s+из\s+(\d+)", title, re.I)
            total_pages = int(total_pages_m.group(1)) if total_pages_m else page_num
            if page_num >= total_pages:
                cat_state.list_complete = True
            else:
                page_num += 1
                cat_state.list_page = page_num
            self.checkpoint.save(checkpoint_path)

        await self._process_pending(cat_dir, checkpoint_path, max_products)

    async def _process_pending(
        self,
        cat_dir: Path,
        checkpoint_path: Path,
        max_products: int | None,
    ) -> None:
        cat_state = self.checkpoint.category
        if not cat_state:
            return

        while self.checkpoint.pending:
            if max_products and len(self.checkpoint.rows) >= max_products:
                break
            item = self.checkpoint.pending.pop(0)
            ref = DnsProductRef(product_id=item["product_id"], url=item["url"], slug=item.get("slug", ""))
            try:
                await self._goto_safe(ref.url)
                html = await self.page.content()
                title = await self.page.title()
                if is_blocked(title, html):
                    self.checkpoint.pending.insert(0, item)
                    raise BlockedError(f"Карточка: {ref.product_id}")
                microdata = await self._fetch_microdata()
                reliability = await self._fetch_reliability()
                page_data = await self._scrape_characteristics_page(ref.url)
                page_data = self._merge_page_data(page_data, reliability)
                product_uuid = ""
                if microdata and microdata.get("@id"):
                    product_uuid = str(microdata["@id"]).split("/")[-1]
                if not product_uuid:
                    uuid_match = re.search(r"/product/microdata/([a-f0-9-]{36})/", html)
                    product_uuid = uuid_match.group(1) if uuid_match else ""
                media_3d = await self._fetch_media_3d(product_uuid)
                row = parse_product_page(
                    html,
                    ref,
                    cat_state,
                    microdata=microdata,
                    page_data=page_data,
                    media_3d=media_3d,
                )
                if self.download_images_flag and (row.get("photo_urls") or row.get("gif_urls")):
                    from storage_paths import canonical_image_dir, image_storage_relpath

                    img_dir = canonical_image_dir("dns", ref.product_id)
                    prefix = f"storage/images/dns/{ref.product_id}"
                    all_urls = list(row.get("photo_urls") or []) + list(row.get("gif_urls") or [])
                    local = await download_images(
                        self.http,
                        all_urls,
                        img_dir,
                        referer=ref.url,
                        path_prefix=prefix,
                    )
                    row["local_images"] = " | ".join(local)
                    row["local_image_count"] = len(local)
                from db.hooks import persist_dns_row

                persist_dns_row(row, cat_dir)
                row.pop("photo_urls", None)
                row.pop("gif_urls", None)
                self.checkpoint.add_row(row)
            except BlockedError:
                raise
            except Exception as exc:
                row = {
                    "product_id": ref.product_id,
                    "url": ref.url,
                    "category_path": cat_state.category_path,
                    "details_error": str(exc),
                }
                self.checkpoint.add_row(row)

            if len(self.checkpoint.rows) % 5 == 0:
                export_products(self.checkpoint.rows, cat_dir / "products.xlsx")
            self.checkpoint.save(checkpoint_path)
            logger.info("DNS детали: %s/%s", len(self.checkpoint.rows), len(self.checkpoint.pending))

        if self.checkpoint.rows:
            export_products(self.checkpoint.rows, cat_dir / "products.xlsx")
        self.checkpoint.save(checkpoint_path)


async def scrape_once(args: argparse.Namespace) -> None:
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    global_cp_path = output_root / "checkpoint_global.json"

    global_cp: dict[str, Any] = {}
    if global_cp_path.exists():
        global_cp = json.loads(global_cp_path.read_text(encoding="utf-8"))

    completed_categories: set[str] = set(global_cp.get("completed_categories", []))

    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as http:
        categories = await load_category_sitemap(http)
        logger.info("Категорий DNS в sitemap: %s", len(categories))

        if args.category:
            categories = [c for c in categories if c.category_id == args.category]
        if args.max_categories:
            categories = categories[: args.max_categories]

        profile = str(output_root / ".browser_profile")
        async with CloakSession(
            headless=args.headless,
            humanize=True,
            profile_dir=profile,
        ) as session:
            page = await session.new_page()
            await warmup_dns_session(page, label="start")

            rate_limiter = DomainRateLimiter(args.delay)

            for index, category in enumerate(categories, 1):
                if category.category_id in completed_categories:
                    logger.info("Пропуск (готово): %s", category.slug)
                    continue

                cat_dir = category_dir(output_root, category.slug.replace("-", " "), category.category_id)
                if category.path:
                    cat_dir = category_dir(output_root, category.path, category.category_id)

                cp_path = cat_dir / "checkpoint.json"
                checkpoint = GlobalCheckpoint.load(cp_path)
                if checkpoint is None:
                    checkpoint = GlobalCheckpoint(shop="dns", output_root=str(output_root))

                scraper = DnsScraper(
                    page,
                    checkpoint,
                    http_session=http,
                    rate_limiter=rate_limiter,
                    category_delay=args.category_delay,
                    download_images_flag=not args.skip_images,
                )

                logger.info("[%s/%s] DNS %s", index, len(categories), category.url)
                cat_backoff = BlockBackoff(max_attempts=DNS_CATEGORY_BLOCK_RETRIES)
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
                            logger.error(
                                "Категория DNS %s не загружена после блокировок: %s",
                                category.slug,
                                exc,
                            )
                            raise BlockedError(
                                f"Категория {category.slug} не загружена — нужен перезапуск браузера"
                            ) from exc
                        logger.info("Повтор категории DNS %s после backoff", category.slug)
                        await warmup_dns_session(page, label=f"retry-{category.slug}")

                completed_categories.add(category.category_id)
                global_cp["completed_categories"] = sorted(completed_categories)
                atomic_write_json(global_cp_path, global_cp)
                logger.info("Категория готова: %s -> %s", category.slug, cat_dir)
                if index < len(categories):
                    await rate_limit_delay(args.category_delay)


async def run_with_retries(args: argparse.Namespace) -> None:
    session_backoff = BlockBackoff(max_attempts=DNS_SESSION_RESTARTS)
    while True:
        try:
            await scrape_once(args)
            return
        except BlockedError as exc:
            if not await session_backoff.wait_before_retry():
                raise
            logger.warning("Перезапуск DNS-сессии после блокировки: %s", exc)


def ensure_dns_run_policy(args: argparse.Namespace) -> None:
    """См. parser_policy.py — единый источник правил DNS."""
    apply_dns_policy(args)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Парсер dns-shop.ru")
    p.add_argument("-o", "--output", default=DEFAULT_OUTPUT)
    p.add_argument("--category", help="ID категории (16 hex)")
    p.add_argument("--limit", type=int, help="Лимит товаров на категорию")
    p.add_argument("--max-categories", type=int)
    p.add_argument("--skip-images", action="store_true")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--delay", type=float, default=REQUEST_DELAY,
                   help="Мин. интервал между page-запросами, сек (~4–6 при 5)")
    p.add_argument("--category-delay", type=float, default=CATEGORY_DELAY,
                   help="Пауза между категориями, sec (~20–35 при 30)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dns_run_policy(args)
    asyncio.run(run_with_retries(args))


if __name__ == "__main__":
    main()
