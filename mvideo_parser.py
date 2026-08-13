#!/usr/bin/env python3
"""
Парсер каталога mvideo.ru с CloakBrowser.

Структура выгрузки:
  output/mvideo/{категория}/products.xlsx
  output/mvideo/{категория}/images/{product_id}/001.jpg

Примеры:
  python mvideo_parser.py --category 1263 --category-slug monitory --limit 5
  python mvideo_parser.py --max-categories 2 --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import aiohttp
from playwright.async_api import BrowserContext, Page, Route, TimeoutError as PlaywrightTimeoutError

from common import (
    BlockBackoff,
    CloakSession,
    DomainRateLimiter,
    atomic_write_json,
    category_dir,
    download_images,
    export_products,
    rate_limit_delay,
    setup_logging,
)
from normalizer.content_filters import clean_html_description, is_review_media_url

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

BASE_URL = "https://www.mvideo.ru"
IMAGE_BASE_URL = "https://img.mvideo.ru/"
PRODUCTS_PAGE_SIZE = 60
REQUEST_DELAY_SECONDS = 4.0
DEFAULT_WORKERS = 2
CATEGORY_DELAY_SECONDS = 20.0
BLOCKED_STATUS_CODES = {403, 429}
INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

DEFAULT_OUTPUT = "output/mvideo"
SAVE_EVERY_N_ROWS = 5
INITIAL_RETRY_DELAY_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 300
CATEGORY_CACHE_TTL_HOURS = 24

setup_logging()
logger = logging.getLogger("mvideo_scraper")

_stealth_helper = None


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


class BlockedError(Exception):
    """Сайт вернул 403 или страницу блокировки."""

    def __init__(self, message: str = "Доступ заблокирован (403)"):
        super().__init__(message)


class RecoverableCategoryError(Exception):
    """Ошибки категории, после которых можно продолжить full-run."""


# ---------------------------------------------------------------------------
# Fast defer (403)
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class MvideoFastDeferConfig:
    fast_defer: bool = True
    defer_after_retry: int = 1
    recheck_delay_minutes: int = 120
    consecutive_403_cooldown_after: int = 3
    session_cooldown_seconds: int = 300
    short_retry_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> MvideoFastDeferConfig:
        return cls(
            fast_defer=_env_bool("MVIDEO_FAST_DEFER", True),
            defer_after_retry=max(0, _env_int("MVIDEO_DEFER_AFTER_RETRY", 1)),
            recheck_delay_minutes=max(1, _env_int("MVIDEO_403_RECHECK_DELAY_MINUTES", 120)),
            consecutive_403_cooldown_after=max(1, _env_int("MVIDEO_CONSECUTIVE_403_COOLDOWN_AFTER", 3)),
            session_cooldown_seconds=max(1, _env_int("MVIDEO_SESSION_COOLDOWN_SECONDS", 300)),
            short_retry_seconds=30.0,
        )


CategoryBlockAction = Literal["retry", "defer"]


class CategoryBlockHandler:
    """403 на категории: один короткий retry, затем defer (без 60/120/240)."""

    def __init__(self, config: MvideoFastDeferConfig):
        self.config = config
        self.blocked_retries = 0

    def reset(self) -> None:
        self.blocked_retries = 0

    async def on_blocked(self) -> CategoryBlockAction:
        if self.blocked_retries < self.config.defer_after_retry:
            delay = self.config.short_retry_seconds * random.uniform(0.85, 1.15)
            logger.warning("MVideo 403: короткий retry через %.0f сек", delay)
            await asyncio.sleep(delay)
            self.blocked_retries += 1
            return "retry"
        return "defer"


class Session403Tracker:
    def __init__(self, config: MvideoFastDeferConfig):
        self.config = config
        self.consecutive_403_deferrals = 0

    def on_success(self) -> None:
        self.consecutive_403_deferrals = 0

    def on_defer(self) -> bool:
        self.consecutive_403_deferrals += 1
        return self.consecutive_403_deferrals >= self.config.consecutive_403_cooldown_after

    def reset_after_cooldown(self) -> None:
        self.consecutive_403_deferrals = 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def defer_category_403(
    global_cp: dict[str, Any],
    cat_key: str,
    *,
    retry_count: int,
    pending_count: int,
    recheck_delay_minutes: int,
    now: datetime | None = None,
) -> str:
    now = now or utc_now()
    next_retry_at = now + timedelta(minutes=recheck_delay_minutes)
    blocked_meta = global_cp.setdefault("blocked_categories", {})
    blocked_meta[cat_key] = {
        "reason": "403",
        "last_403": now.isoformat(),
        "retry_count": retry_count,
        "next_retry_at": next_retry_at.isoformat(),
        "pending_count": pending_count,
    }
    needs_recheck = set(global_cp.get("needs_recheck_categories", []))
    needs_recheck.add(cat_key)
    global_cp["needs_recheck_categories"] = sorted(needs_recheck)
    return next_retry_at.isoformat()


def clear_category_403_defer(global_cp: dict[str, Any], cat_key: str) -> None:
    blocked_meta = global_cp.get("blocked_categories", {})
    if isinstance(blocked_meta, dict):
        blocked_meta.pop(cat_key, None)
        global_cp["blocked_categories"] = blocked_meta
    needs_recheck = set(global_cp.get("needs_recheck_categories", []))
    needs_recheck.discard(cat_key)
    global_cp["needs_recheck_categories"] = sorted(needs_recheck)


def is_403_recheck_ready(
    global_cp: dict[str, Any],
    cat_key: str,
    now: datetime | None = None,
) -> bool:
    meta = global_cp.get("blocked_categories", {}).get(cat_key)
    if not isinstance(meta, dict) or meta.get("reason") != "403":
        return True
    next_retry_at = meta.get("next_retry_at")
    if not next_retry_at:
        return True
    now = now or utc_now()
    return now >= datetime.fromisoformat(next_retry_at)


def category_403_next_retry_at(global_cp: dict[str, Any], cat_key: str) -> str | None:
    meta = global_cp.get("blocked_categories", {}).get(cat_key)
    if not isinstance(meta, dict) or meta.get("reason") != "403":
        return None
    value = meta.get("next_retry_at")
    return str(value) if value else None


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


@dataclass
class CategoryState:
    category_id: str
    category_name: str
    category_path: str
    category_slug: str
    list_cursor: str = ""
    list_complete: bool = False


@dataclass
class CheckpointState:
    version: int = 2
    output: str = ""
    category: CategoryState | None = None
    completed_ids: list[str] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed_set(self) -> set[str]:
        return set(self.completed_ids)

    def add_row(self, row: dict[str, Any]) -> None:
        self.upsert_row(row)

    def upsert_row(self, row: dict[str, Any]) -> None:
        product_id = str(row["product_id"])
        row.pop("details_error", None)
        for index, existing in enumerate(self.rows):
            if str(existing.get("product_id")) == product_id:
                self.rows[index] = row
                return
        self.completed_ids.append(product_id)
        self.rows.append(row)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "output": self.output,
            "category": asdict(self.category) if self.category else None,
            "completed_ids": self.completed_ids,
            "pending": self.pending,
            "rows_count": len(self.rows),
        }
        atomic_write_json(path, payload)
        atomic_write_json(path.with_suffix(".rows.json"), self.rows, indent=None)

    @classmethod
    def load(cls, path: Path) -> CheckpointState | None:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows_path = path.with_suffix(".rows.json")
        rows: list[dict[str, Any]] = []
        if rows_path.exists():
            rows = json.loads(rows_path.read_text(encoding="utf-8"))
        category_payload = payload.get("category")
        category = CategoryState(**category_payload) if category_payload else None
        return cls(
            version=payload.get("version", 2),
            output=payload.get("output", ""),
            category=category,
            completed_ids=payload.get("completed_ids", []),
            pending=payload.get("pending", []),
            rows=rows,
        )


def row_needs_detail_retry(row: dict[str, Any]) -> bool:
    if row.get("details_error"):
        return True
    if str(row.get("description") or "").strip():
        return False
    spec_count = row.get("spec_count")
    if spec_count is not None and int(spec_count) > 0:
        return False
    if str(row.get("characteristics") or "").strip():
        return False
    return True


def slug_from_product_row(row: dict[str, Any]) -> str:
    url = str(row.get("url") or "")
    if "mvideo.ru" in url:
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path.startswith("/"):
            return path
    product_id = str(row.get("product_id") or "").strip()
    return f"/products/{product_id}" if product_id else ""


def images_from_product_row(row: dict[str, Any]) -> list[str]:
    photos = row.get("photos") or row.get("photo_urls") or ""
    if isinstance(photos, list):
        return [str(url).strip() for url in photos if str(url).strip()]
    return [part.strip() for part in str(photos).split("|") if part.strip()]


def row_to_list_item(row: dict[str, Any]) -> ProductListItem:
    return ProductListItem(
        product_id=str(row["product_id"]),
        name=str(row.get("name") or ""),
        slug=slug_from_product_row(row),
        sale_price=row.get("sale_price"),
        base_price=row.get("base_price"),
        discount=row.get("discount_percent") if "discount_percent" in row else row.get("discount"),
        brand=row.get("brand"),
        rating_count=row.get("rating_count"),
        rating_star=row.get("rating_star"),
        status=row.get("status"),
        images=images_from_product_row(row),
        category_id=str(row.get("category_id") or ""),
        category_name=str(row.get("category_name") or ""),
        category_path=str(row.get("category_path") or ""),
    )


def requeue_failed_details_in_checkpoint(checkpoint: CheckpointState) -> int:
    pending_ids = {str(item.get("product_id")) for item in checkpoint.pending}
    requeued = 0
    kept_rows: list[dict[str, Any]] = []
    for row in checkpoint.rows:
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            kept_rows.append(row)
            continue
        if not row_needs_detail_retry(row):
            kept_rows.append(row)
            continue
        if product_id in pending_ids:
            continue
        checkpoint.pending.append(row_to_list_item(row).to_dict())
        pending_ids.add(product_id)
        if product_id in checkpoint.completed_ids:
            checkpoint.completed_ids.remove(product_id)
        requeued += 1
    checkpoint.rows = kept_rows
    checkpoint.completed_ids = [
        str(row["product_id"]) for row in kept_rows if str(row.get("product_id") or "").strip()
    ]
    return requeued


# ---------------------------------------------------------------------------
# Модели и API-клиент
# ---------------------------------------------------------------------------


@dataclass
class Category:
    seo_id: int
    name: str
    slug: str
    path: str


def categories_from_urls(urls: list[str], *, log_label: str) -> list[Category]:
    categories: list[Category] = []
    seen: set[str] = set()
    for url in urls or []:
        if not isinstance(url, str):
            continue
        path_match = re.search(r"https://www\.mvideo\.ru/([^?#]+)", url)
        if not path_match:
            continue
        raw_path = path_match.group(1).strip("/")
        tail_match = re.search(r"^(.*)-(\d+)(?:[/?#]|$)", raw_path)
        if not tail_match:
            continue
        slug = tail_match.group(1).strip("/")
        seo_id = int(tail_match.group(2))
        if not slug:
            continue
        key = f"{slug}|{seo_id}"
        if key in seen:
            continue
        seen.add(key)
        name = slug.rsplit("/", 1)[-1].replace("-", " ").strip()
        categories.append(
            Category(
                seo_id=seo_id,
                name=name or slug,
                slug=slug,
                path=slug.replace("/", " > "),
            )
        )
    logger.info("Категории из %s: %s", log_label, len(categories))
    return categories


@dataclass
class ProductListItem:
    product_id: str
    name: str
    slug: str
    sale_price: float | None
    base_price: float | None
    discount: float | None
    brand: str | None
    rating_count: int | None
    rating_star: float | None
    status: str | None
    images: list[str] = field(default_factory=list)
    category_id: str = ""
    category_name: str = ""
    category_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProductListItem:
        return cls(**data)


def is_valid_product_id(product_id: Any) -> bool:
    return bool(str(product_id or "").strip())


def is_blocked_status(status: int, body_preview: str = "") -> bool:
    if status in BLOCKED_STATUS_CODES:
        return True
    lowered = body_preview.lower()
    return "<title>ошибка</title>" in lowered or "support/css/style.min.css" in lowered


async def apply_stealth_to_page(page: Page) -> None:
    global _stealth_helper
    if _stealth_helper is None:
        from playwright_stealth import Stealth

        _stealth_helper = Stealth()
    await _stealth_helper.apply_stealth_async(page)


async def configure_context(context: BrowserContext) -> None:
    async def block_heavy(route: Route) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", block_heavy)


async def prepare_page(page: Page, *, use_stealth: bool = False) -> None:
    if use_stealth:
        await apply_stealth_to_page(page)
    else:
        await page.add_init_script(INIT_SCRIPT)


class MvideoApiClient:
    def __init__(self, page: Page, rate_limiter: DomainRateLimiter | None = None):
        self._page = page
        self._rate_limiter = rate_limiter

    async def _throttle(self) -> None:
        if self._rate_limiter:
            await self._rate_limiter.wait()

    async def _fetch_json(self, method: str, path: str, body: dict | None = None) -> dict:
        await self._throttle()
        payload = json.dumps(body, ensure_ascii=False) if body is not None else None
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        result = await self._page.evaluate(
            """async ({ method, url, payload }) => {
                const options = {
                    method,
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                };
                if (payload) options.body = payload;
                const response = await fetch(url, options);
                const text = await response.text();
                return { status: response.status, text };
            }""",
            {"method": method, "url": url, "payload": payload},
        )
        status, text = result["status"], result["text"]
        if is_blocked_status(status, text):
            raise BlockedError(f"{method} {path} -> HTTP {status}")
        if status != 200:
            raise RuntimeError(f"{method} {path} -> HTTP {status}: {text[:300]}")
        data = json.loads(text)
        if not data.get("success", True):
            raise RuntimeError(f"{method} {path} -> API error: {data.get('messages')}")
        return data.get("body", data)

    async def _fetch_products_api(self, request_body: dict) -> dict:
        """POST /bff/products with graceful 204 handling."""
        await self._throttle()
        payload = json.dumps(request_body, ensure_ascii=False)
        result = await self._page.evaluate(
            """async ({ payload }) => {
                const response = await fetch('/bff/products', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: payload,
                });
                const text = await response.text();
                return { status: response.status, text };
            }""",
            {"payload": payload},
        )
        status, text = result["status"], result["text"]
        if is_blocked_status(status, text):
            raise BlockedError(f"POST /bff/products -> HTTP {status}")
        if status == 204:
            logger.warning("POST /bff/products -> HTTP 204 (empty payload)")
            return {
                "items": [],
                "total": 0,
                "cursorId": None,
                "_meta": {"http_status": 204, "empty": True, "source": "api"},
            }
        if status != 200:
            raise RuntimeError(f"POST /bff/products -> HTTP {status}: {text[:300]}")
        data = json.loads(text) if text else {}
        if isinstance(data, dict) and not data.get("success", True):
            raise RuntimeError(f"POST /bff/products -> API error: {data.get('messages')}")
        body = data.get("body", data) if isinstance(data, dict) else {}
        if isinstance(body, dict):
            body.setdefault("_meta", {"http_status": 200, "source": "api"})
        return body

    @staticmethod
    def _products_meta(body: dict[str, Any]) -> dict[str, Any]:
        meta = body.get("_meta")
        return meta if isinstance(meta, dict) else {}

    async def get_catalog_from_catalog_api(self, cached_body: dict | None = None) -> list[Category]:
        categories: list[Category] = []
        if cached_body is None:
            cached_body = await self._fetch_json("GET", "/bff/settings/v3/catalog")

        if cached_body:
            def walk(nodes: list[dict], parents: list[str]) -> None:
                for node in nodes:
                    name = node.get("name", "")
                    path_parts = parents + [name]
                    children = node.get("children")
                    if not children:
                        categories.append(
                            Category(
                                seo_id=int(node["seoId"]),
                                name=name,
                                slug=node.get("slug", ""),
                                path=" > ".join(path_parts),
                            )
                        )
                    else:
                        walk(children, path_parts)

            walk(cached_body.get("categories", []), [])

        return categories

    async def get_catalog_from_sitemap(self) -> list[Category]:
        await self._throttle()
        result = await self._page.evaluate(
            """async () => {
                const fetchText = async (url) => {
                    const r = await fetch(url, { credentials: 'include' });
                    if (!r.ok) throw new Error(`${url} -> ${r.status}`);
                    return await r.text();
                };
                const indexXml = await fetchText('/sitemap.xml');
                const locs = [...indexXml.matchAll(/<loc>([^<]+)<\\/loc>/g)].map((m) => m[1]);
                const catMaps = locs.filter((u) => u.includes('sitemap-categories-'));
                if (!catMaps.length) {
                    catMaps.push('https://www.mvideo.ru/sitemaps/sitemap-categories-www.mvideo.ru-1.xml');
                }
                const urls = [];
                for (const sm of catMaps) {
                    const xml = await fetchText(sm);
                    for (const m of xml.matchAll(/<loc>([^<]+)<\\/loc>/g)) {
                        urls.push(m[1]);
                    }
                }
                return urls;
            }""",
        )
        return categories_from_urls(result or [], log_label="sitemap")

    async def get_catalog_from_browser_links(self) -> list[Category]:
        """Last-resort discovery from links visible in browser pages."""
        await self._throttle()
        home_links = await self._page.evaluate(
            """() => {
                const urls = [];
                const add = (u) => {
                    if (!u || typeof u !== 'string') return;
                    if (!u.startsWith('https://www.mvideo.ru/')) return;
                    urls.push(u);
                };
                for (const a of document.querySelectorAll('a[href]')) {
                    add(a.href);
                }
                return Array.from(new Set(urls));
            }""",
        )
        await self._throttle()
        settings_links = await self._page.evaluate(
            """async () => {
                const req = '/bff/settings?types=general&types=common&types=main&types=login&types=promo20&types=pdp&types=plp&types=srp&types=ssrv&types=customerServices&types=typ&types=mcombo&types=promo';
                const r = await fetch(req, { credentials: 'include' });
                if (!r.ok) return [];
                const text = await r.text();
                const matches = text.match(/https:\\/\\/www\\.mvideo\\.ru\\/[^"\\s\\\\]+-\\d+/g) || [];
                return Array.from(new Set(matches));
            }""",
        )
        seeded = list(dict.fromkeys([*(home_links or []), *(settings_links or [])]))
        discovered: list[str] = list(seeded)
        # Best effort: open a small set of discovered category-like links and collect nested links.
        for url in seeded[:12]:
            if not re.search(r"-\d+(?:[/?#]|$)", url):
                continue
            try:
                await self._throttle()
                await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await self._page.wait_for_timeout(600)
                links = await self._page.evaluate(
                    """() => Array.from(
                        new Set(
                            [...document.querySelectorAll('a[href]')]
                                .map((a) => a.href)
                                .filter((u) => typeof u === 'string' && u.startsWith('https://www.mvideo.ru/'))
                        )
                    )""",
                )
                discovered.extend(links or [])
            except Exception as exc:
                logger.debug("browser-link discovery skip %s: %s", url, exc)
                continue
        return categories_from_urls(list(dict.fromkeys(discovered)), log_label="browser links")

    async def list_products(
        self,
        category_id: str,
        cursor_id: str = "",
        limit: int = PRODUCTS_PAGE_SIZE,
        category_slug: str = "",
    ) -> tuple[list[ProductListItem], str | None, int, bool, str | None]:
        request_body = {
            "limit": limit,
            "cursorId": cursor_id,
            "enrich": True,
            "filters": [{"id": "category", "valuesId": [str(category_id)]}],
            "sortBy": "popularity",
            "sortDirection": "desc",
            "isGettingBonusRoubles": True,
        }
        if cursor_id == "" and category_slug:
            category_url = f"{BASE_URL}/{category_slug}-{category_id}"
            try:
                body = await self._load_products_via_page(category_url, request_body)
            except BlockedError:
                logger.warning("Страница категории недоступна, пробуем API: %s", category_url)
                body = await self._fetch_products_api(request_body)
        else:
            body = await self._fetch_products_api(request_body)

        meta = self._products_meta(body)
        deferred_empty = bool(meta.get("deferred_empty"))
        deferred_reason = str(meta.get("deferred_reason") or "") or None
        items = []
        for raw in body.get("items", []):
            if not isinstance(raw, dict):
                continue
            item = parse_list_product_item(raw, category_id)
            if item is not None:
                items.append(item)
        next_cursor = body.get("cursorId")
        total = int(body.get("total") or 0)
        if not items or (next_cursor == cursor_id and cursor_id):
            return items, None, total, deferred_empty, deferred_reason
        return items, str(next_cursor) if next_cursor else None, total, deferred_empty, deferred_reason

    async def _load_products_via_page(self, category_url: str, request_body: dict) -> dict:
        await self._throttle()
        try:
            async with self._page.expect_response(
                lambda r: "/bff/products" in r.url and r.request.method == "POST",
                timeout=60000,
            ) as response_info:
                response = await self._page.goto(category_url, wait_until="load", timeout=90000)
                if response and is_blocked_status(response.status):
                    raise BlockedError(f"Категория: {category_url}")
            api_response = await response_info.value
            if is_blocked_status(api_response.status):
                raise BlockedError(f"POST /bff/products -> HTTP {api_response.status}")
            if api_response.status == 204:
                logger.warning(
                    "browser products list empty: %s -> HTTP 204",
                    category_url,
                )
                return {
                    "items": [],
                    "total": 0,
                    "cursorId": None,
                    "_meta": {"http_status": 204, "empty": True, "source": "browser"},
                }
            if api_response.status != 200:
                raise RuntimeError(
                    f"POST /bff/products(browser) -> HTTP {api_response.status}"
                )
            payload = await api_response.json()
            if not payload.get("success"):
                raise RuntimeError(str(payload.get("messages")))
            return payload.get("body", payload)
        except BlockedError:
            raise
        except PlaywrightTimeoutError as exc:
            logger.warning("Timeout list via browser for %s, fallback API", category_url)
            try:
                fallback = await self._fetch_products_api(request_body)
                fallback_meta = self._products_meta(fallback)
                if fallback_meta.get("http_status") == 204:
                    logger.warning(
                        "API fallback after timeout returned 204 for %s, retry once",
                        category_url,
                    )
                    await rate_limit_delay(6)
                    second_try = await self._fetch_products_api(request_body)
                    second_meta = self._products_meta(second_try)
                    if second_meta.get("http_status") == 204:
                        second_meta["deferred_empty"] = True
                        second_meta["deferred_reason"] = "timeout_then_double_204"
                        second_try["_meta"] = second_meta
                    return second_try
                return fallback
            except Exception as inner_exc:
                raise RecoverableCategoryError(
                    f"list timeout + API fallback failed for {category_url}: {inner_exc}"
                ) from exc
        except Exception:
            return await self._fetch_products_api(request_body)

    @staticmethod
    def image_url(relative_path: str) -> str:
        if not relative_path:
            return ""
        if relative_path.startswith("http"):
            return relative_path
        return IMAGE_BASE_URL + relative_path.lstrip("/")

    @staticmethod
    def collect_image_urls(details: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for key in ("imagesBig", "images", "image"):
            value = details.get(key)
            if isinstance(value, list):
                candidates.extend(MvideoApiClient.image_url(img) for img in value if img)
            elif isinstance(value, str) and value:
                candidates.append(MvideoApiClient.image_url(value))
        images_3d = details.get("images3d") or []
        if isinstance(images_3d, list):
            candidates.extend(MvideoApiClient.image_url(img) for img in images_3d if img)
        return list(dict.fromkeys(url for url in candidates if url))

    @staticmethod
    def collect_file_urls(details: dict[str, Any], key: str) -> list[str]:
        value = details.get(key) or []
        if not isinstance(value, list):
            return []
        urls: list[str] = []
        for item in value:
            if isinstance(item, str):
                urls.append(MvideoApiClient.image_url(item))
            elif isinstance(item, dict):
                for field in ("url", "link", "src", "file"):
                    if item.get(field):
                        urls.append(MvideoApiClient.image_url(str(item[field])))
        return list(dict.fromkeys(url for url in urls if url))

    @staticmethod
    def flatten_properties(properties_body: dict[str, Any]) -> dict[str, str]:
        flat: dict[str, str] = {}
        for group in properties_body.get("all", []):
            group_name = group.get("name", "")
            for prop in group.get("properties", []):
                key = f"{group_name} | {prop.get('name', '')}".strip(" |")
                flat[key] = str(prop.get("value", ""))
        return flat


def parse_list_product_item(raw: dict[str, Any], category_id: str) -> ProductListItem | None:
    product_id = str(raw.get("productId") or "").strip()
    if not is_valid_product_id(product_id):
        logger.warning(
            "MVideo list item skipped: category_id=%s reason=empty productId",
            category_id,
        )
        return None
    price = raw.get("price") or {}
    rating = raw.get("rating") or {}
    return ProductListItem(
        product_id=product_id,
        name=raw.get("name", ""),
        slug=raw.get("slug", ""),
        sale_price=price.get("salePrice"),
        base_price=price.get("basePrice"),
        discount=price.get("discount"),
        brand=raw.get("brandName"),
        rating_count=rating.get("count"),
        rating_star=rating.get("star"),
        status=raw.get("status"),
        images=[MvideoApiClient.image_url(img) for img in raw.get("images", [])],
        category_id=category_id,
    )


async def fetch_product_details(
    page: Page,
    product_id: str,
    slug: str = "",
    *,
    rate_limiter: DomainRateLimiter | None = None,
) -> dict[str, Any]:
    if rate_limiter:
        await rate_limiter.wait()
    product_url = f"{BASE_URL}{slug}" if slug.startswith("/") else f"{BASE_URL}/products/{product_id}"
    try:
        async with page.expect_response(
            lambda r: "/bff/product-details" in r.url and f"productId={product_id}" in r.url,
            timeout=45000,
        ) as response_info:
            response = await page.goto(product_url, wait_until="domcontentloaded", timeout=90000)
            if response and is_blocked_status(response.status):
                raise BlockedError(f"Карточка заблокирована: {product_id}")
        api_response = await response_info.value
        if is_blocked_status(api_response.status):
            raise BlockedError(f"product-details 403: {product_id}")
        payload = await api_response.json()
        if not payload.get("success"):
            raise RuntimeError(str(payload.get("messages")))
        return payload.get("body", payload)
    except BlockedError:
        raise
    except Exception as exc:
        client = MvideoApiClient(page, rate_limiter)
        try:
            return await client._fetch_json("GET", f"/bff/product-details?productId={product_id}")
        except BlockedError:
            raise
        except Exception as inner_exc:
            raise RuntimeError(f"product-details failed for {product_id}: {exc}; {inner_exc}") from exc


class MvideoWorkerPool:
    def __init__(
        self,
        context: BrowserContext,
        workers: int = DEFAULT_WORKERS,
        *,
        use_stealth: bool = False,
        rate_limiter: DomainRateLimiter | None = None,
    ):
        self._context = context
        self._workers = max(1, workers)
        self._use_stealth = use_stealth
        self._rate_limiter = rate_limiter
        self._pages: asyncio.Queue[Page] = asyncio.Queue()

    async def start(self) -> None:
        for _ in range(self._workers):
            page = await self._context.new_page()
            await prepare_page(page, use_stealth=self._use_stealth)
            await self._pages.put(page)

    async def close(self) -> None:
        while not self._pages.empty():
            await (await self._pages.get()).close()

    async def get_product_details(self, product_id: str, slug: str = "") -> dict[str, Any]:
        page = await self._pages.get()
        try:
            return await fetch_product_details(
                page, product_id, slug, rate_limiter=self._rate_limiter
            )
        finally:
            await self._pages.put(page)


async def warm_up_session(
    context: BrowserContext,
    *,
    category_slug: str = "",
    category_id: str = "",
    retry_attempt: int = 0,
    use_stealth: bool = False,
) -> tuple[Page, dict | None]:
    page = await context.new_page()
    await prepare_page(page, use_stealth=use_stealth)
    await page.goto(BASE_URL, wait_until="load", timeout=90000)
    await rate_limit_delay(4 + retry_attempt)

    if retry_attempt >= 3:
        logger.info("Облегчённый прогрев после %s блокировок", retry_attempt)
        await rate_limit_delay(8 + retry_attempt * 2)
        return page, None

    warmup_path = (
        f"{BASE_URL}/{category_slug}-{category_id}"
        if category_slug and category_id
        else f"{BASE_URL}/noutbuki-118"
    )
    catalog_body: dict | None = None
    try:
        async with page.expect_response(
            lambda r: "/bff/settings/v3/catalog" in r.url and r.status == 200,
            timeout=45000,
        ) as response_info:
            response = await page.goto(warmup_path, wait_until="load", timeout=90000)
            if response and is_blocked_status(response.status):
                raise BlockedError(f"Прогрев: {warmup_path}")
        payload = await (await response_info.value).json()
        if payload.get("success"):
            catalog_body = payload.get("body")
    except BlockedError:
        logger.warning("Категория для прогрева недоступна: %s", warmup_path)
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
        await rate_limit_delay(5 + retry_attempt)
    except Exception:
        response = await page.goto(warmup_path, wait_until="domcontentloaded", timeout=90000)
        if response and is_blocked_status(response.status):
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)
        await rate_limit_delay(5)
    return page, catalog_body


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _category_to_dict(cat: Category) -> dict[str, Any]:
    return {
        "seo_id": int(cat.seo_id),
        "name": cat.name,
        "slug": cat.slug,
        "path": cat.path,
    }


def _category_from_dict(payload: dict[str, Any]) -> Category | None:
    try:
        return Category(
            seo_id=int(payload["seo_id"]),
            name=str(payload.get("name", "")),
            slug=str(payload.get("slug", "")),
            path=str(payload.get("path", "")),
        )
    except Exception:
        return None


def load_categories_cache(cache_path: Path) -> tuple[list[Category], dict[str, Any] | None]:
    if not cache_path.exists():
        return [], None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Категории cache unreadable: %s", exc)
        return [], None
    raw_items = payload.get("categories") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return [], payload if isinstance(payload, dict) else None
    categories: list[Category] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        cat = _category_from_dict(item)
        if cat is not None:
            categories.append(cat)
    return categories, payload if isinstance(payload, dict) else None


def save_categories_cache(
    cache_path: Path,
    categories: list[Category],
    *,
    source: str,
) -> None:
    if not categories:
        logger.warning("Пустой categories cache не сохраняем (source=%s)", source)
        return
    payload = {
        "updated_at": _now_iso(),
        "source": source,
        "count": len(categories),
        "categories": [_category_to_dict(c) for c in categories],
    }
    atomic_write_json(cache_path, payload)
    logger.info("categories_cache updated: %s (%s)", len(categories), source)


def is_cache_stale(meta: dict[str, Any] | None, *, ttl_hours: int = CATEGORY_CACHE_TTL_HOURS) -> bool:
    if not meta:
        return True
    updated_at = str(meta.get("updated_at") or "")
    if not updated_at:
        return True
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except Exception:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc) - timedelta(hours=ttl_hours)


def is_recoverable_category_error(exc: Exception) -> bool:
    if isinstance(exc, (RecoverableCategoryError, PlaywrightTimeoutError, asyncio.TimeoutError, aiohttp.ClientError)):
        return True
    text = str(exc)
    recoverable_markers = (
        "/bff/products",
        "list timeout + API fallback failed",
        "Timeout",
        "timed out",
    )
    return any(marker in text for marker in recoverable_markers)


def build_base_row(item: ProductListItem) -> dict[str, Any]:
    url = f"{BASE_URL}{item.slug}" if item.slug.startswith("/") else item.slug
    return {
        "product_id": item.product_id,
        "name": item.name,
        "brand": item.brand or "",
        "category_id": item.category_id,
        "category_name": item.category_name,
        "category_path": item.category_path,
        "url": url,
        "sale_price": item.sale_price,
        "base_price": item.base_price,
        "discount_percent": item.discount,
        "rating_count": item.rating_count,
        "rating_star": item.rating_star,
        "status": item.status,
        "photos": " | ".join(item.images),
        "photo_count": len(item.images),
    }


def enrich_row_with_details(row: dict[str, Any], details: dict[str, Any], list_images: list[str]) -> dict[str, Any]:
    detail_images = MvideoApiClient.collect_image_urls(details)
    all_images = list(dict.fromkeys(list_images + detail_images))
    flat_props = MvideoApiClient.flatten_properties(details.get("properties", {}))
    videos = [
        url
        for url in MvideoApiClient.collect_file_urls(details, "videos")
        if not is_review_media_url(url)
    ]
    instructions = MvideoApiClient.collect_file_urls(details, "instructions")
    certificates = MvideoApiClient.collect_file_urls(details, "certificates")
    groups = details.get("groups") or []
    group_path = " > ".join(
        str(item.get("name", "")).strip()
        for item in groups
        if isinstance(item, dict) and item.get("name")
    )
    supplier = details.get("supplier") or {}
    status = details.get("status") or {}
    rating = details.get("rating") or {}
    variants = details.get("variants") or []
    variant_names: list[str] = []
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict):
                label = variant.get("name") or variant.get("title") or variant.get("color")
                if label:
                    variant_names.append(str(label))

    row.update(
        {
            "brand": details.get("brandName") or row["brand"],
            "model_name": details.get("modelName", ""),
            "description": clean_html_description(details.get("description") or ""),
            "weight_kg": details.get("weight"),
            "material_number": details.get("materialCisNumber", ""),
            "detail_category_id": (details.get("category") or {}).get("id", ""),
            "detail_category_name": (details.get("category") or {}).get("name", ""),
            "category_groups": group_path,
            "photos": " | ".join(all_images),
            "photo_count": len(all_images),
            "photo_urls": all_images,
            "videos": " | ".join(videos),
            "video_count": len(videos),
            "instructions": " | ".join(instructions),
            "instruction_count": len(instructions),
            "certificates": " | ".join(certificates),
            "certificate_count": len(certificates),
            "supplier_id": supplier.get("id", ""),
            "supplier_name": supplier.get("name", ""),
            "supplier_inn": supplier.get("inn", ""),
            "supplier_ogrn": supplier.get("ogrn", ""),
            "supplier_phone": supplier.get("phone", ""),
            "supplier_address": supplier.get("address", ""),
            "status_showcase": status.get("showCase"),
            "status_sold_out": status.get("soldOut"),
            "status_retail_only": status.get("availableOnlyInRetailStore"),
            "status_no_exchange": status.get("isCannotBeExchanged"),
            "rating_percent": rating.get("percent"),
            "sale_legal": details.get("saleLegal"),
            "is_preorder": details.get("isPreorder"),
            "variant_count": len(variant_names),
            "variants": " | ".join(variant_names),
            "characteristics": "; ".join(f"{k}: {v}" for k, v in flat_props.items()),
            "spec_count": len(flat_props),
        }
    )
    for key, value in flat_props.items():
        row[f"spec__{key}"] = value
    return row


class MvideoScraper:
    def __init__(
        self,
        list_client: MvideoApiClient,
        worker_pool: MvideoWorkerPool | None,
        checkpoint: CheckpointState,
        *,
        fetch_details: bool = True,
        save_every: int = SAVE_EVERY_N_ROWS,
        category_delay: float = CATEGORY_DELAY_SECONDS,
        http_session: aiohttp.ClientSession | None = None,
        cat_dir: Path | None = None,
        download_images_flag: bool = True,
        rate_limiter: DomainRateLimiter | None = None,
        details_only: bool = False,
    ):
        self.list_client = list_client
        self.worker_pool = worker_pool
        self.checkpoint = checkpoint
        self.fetch_details = fetch_details
        self.details_only = details_only
        self.save_every = save_every
        self.http = http_session
        self.cat_dir = cat_dir
        self.download_images_flag = download_images_flag
        self.category_delay = category_delay
        self.rate_limiter = rate_limiter
        self.discovered_urls = 0
        self.processed = 0
        self.skipped = 0
        self.failed = 0
        self.deferred_empty = False
        self.deferred_reason = ""

    async def scrape_category(
        self,
        category_id: str,
        category_name: str,
        category_path: str,
        *,
        category_slug: str = "",
        max_products: int | None = None,
        checkpoint_path: Path | None = None,
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        self.deferred_empty = False
        self.deferred_reason = ""
        if (
            self.checkpoint.category is None
            or self.checkpoint.category.category_id != category_id
        ):
            self.checkpoint.category = CategoryState(
                category_id=category_id,
                category_name=category_name,
                category_path=category_path,
                category_slug=category_slug,
            )
            self.checkpoint.pending = []
            self.checkpoint.completed_ids = []
            self.checkpoint.rows = []
        if self.details_only and self.checkpoint.category:
            self.checkpoint.category.list_complete = True
            logger.info(
                "Details-only: пропуск listing, очередь %s",
                len(self.checkpoint.pending),
            )
        elif not self.details_only:
            await self._collect_product_list(self.checkpoint.category, max_products, checkpoint_path, output_path)
        elif self.checkpoint.category and self.checkpoint.category.list_complete:
            logger.info("Details-only: listing уже собран, очередь %s", len(self.checkpoint.pending))
        await self._process_pending_details(max_products, checkpoint_path, output_path)
        summary = {
            "discovered_urls": self.discovered_urls,
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "deferred_empty": self.deferred_empty,
            "deferred_reason": self.deferred_reason,
        }
        logger.info(
            "SUMMARY category=%s discovered=%s processed=%s skipped=%s failed=%s deferred_empty=%s",
            self.checkpoint.category.category_path if self.checkpoint.category else category_id,
            summary["discovered_urls"],
            summary["processed"],
            summary["skipped"],
            summary["failed"],
            summary["deferred_empty"],
        )
        return summary

    async def _collect_product_list(
        self,
        category_state: CategoryState,
        max_products: int | None,
        checkpoint_path: Path | None,
        output_path: Path | None,
    ) -> None:
        if category_state.list_complete:
            logger.info("Список уже собран, в очереди: %s", len(self.checkpoint.pending))
            return
        cursor = category_state.list_cursor
        while True:
            items, next_cursor, total, deferred_empty, deferred_reason = await self.list_client.list_products(
                category_state.category_id, cursor, category_slug=category_state.category_slug
            )
            if deferred_empty:
                self.deferred_empty = True
                self.deferred_reason = deferred_reason or "timeout_then_double_204"
                logger.warning(
                    "Категория %s помечена deferred_empty (%s), перенесена на recheck",
                    category_state.category_path,
                    self.deferred_reason,
                )
                self._persist(checkpoint_path, output_path)
                return
            for item in items:
                if not is_valid_product_id(item.product_id):
                    logger.warning(
                        "MVideo list item skipped: category_id=%s reason=empty productId",
                        category_state.category_id,
                    )
                    continue
                if item.product_id in self.checkpoint.completed_set:
                    self.skipped += 1
                    continue
                if any(p["product_id"] == item.product_id for p in self.checkpoint.pending):
                    self.skipped += 1
                    continue
                item.category_name = category_state.category_name
                item.category_path = category_state.category_path
                self.checkpoint.pending.append(item.to_dict())
                self.discovered_urls += 1
                if max_products and len(self.checkpoint.completed_ids) + len(self.checkpoint.pending) >= max_products:
                    category_state.list_complete = True
                    self._persist(checkpoint_path, output_path)
                    return
            logger.info(
                "Список '%s': %s/%s, очередь %s",
                category_state.category_name,
                len(self.checkpoint.completed_ids) + len(self.checkpoint.pending),
                total,
                len(self.checkpoint.pending),
            )
            if not next_cursor:
                category_state.list_complete = True
                category_state.list_cursor = ""
                self._persist(checkpoint_path, output_path)
                return
            cursor = next_cursor
            category_state.list_cursor = cursor
            self._persist(checkpoint_path, output_path)

    def _take_next_valid_pending_item(self) -> ProductListItem | None:
        while self.checkpoint.pending:
            item = ProductListItem.from_dict(self.checkpoint.pending.pop(0))
            if is_valid_product_id(item.product_id):
                return item
            logger.warning(
                "MVideo invalid pending item skipped: category_id=%s product_id=%r name=%r slug=%r",
                item.category_id,
                item.product_id,
                item.name,
                item.slug,
            )
            self.skipped += 1
        return None

    async def _process_pending_details(
        self,
        max_products: int | None,
        checkpoint_path: Path | None,
        output_path: Path | None,
    ) -> None:
        if not self.fetch_details:
            while self.checkpoint.pending:
                item = self._take_next_valid_pending_item()
                if item is None:
                    break
                if item.product_id not in self.checkpoint.completed_set:
                    row = build_base_row(item)
                    if item.images:
                        row["photo_urls"] = item.images
                    await self._finalize_row(row, item)
                    self.checkpoint.upsert_row(row)
                    self.processed += 1
                else:
                    self.skipped += 1
                if max_products and len(self.checkpoint.rows) >= max_products:
                    break
            self._persist(checkpoint_path, output_path)
            return
        if self.worker_pool is None:
            raise RuntimeError("Worker pool required")
        workers = self.worker_pool._workers
        while self.checkpoint.pending:
            if max_products and len(self.checkpoint.rows) >= max_products:
                break
            batch: list[ProductListItem] = []
            while len(batch) < workers and self.checkpoint.pending:
                if max_products and len(self.checkpoint.rows) + len(batch) >= max_products:
                    break
                item = self._take_next_valid_pending_item()
                if item is None:
                    break
                if item.product_id in self.checkpoint.completed_set:
                    self.skipped += 1
                    continue
                batch.append(item)
            if not batch:
                continue
            results = await asyncio.gather(
                *[self._fetch_and_build_row(item, index) for index, item in enumerate(batch)],
                return_exceptions=True,
            )
            blocked: list[ProductListItem] = []
            for item, result in zip(batch, results):
                if isinstance(result, BlockedError):
                    logger.warning(
                        "MVideo BLOCK SOURCE: product_id=%s slug=%s error=%s",
                        item.product_id,
                        item.slug,
                        result,
                    )
                    blocked.append(item)
                elif isinstance(result, Exception):
                    logger.warning(
                        "MVideo detail failed product_id=%s slug=%s: %s",
                        item.product_id,
                        item.slug,
                        result,
                    )
                    row = build_base_row(item)
                    row["details_error"] = str(result)
                    await self._finalize_row(row, item)
                    self.checkpoint.upsert_row(row)
                    self.failed += 1
                else:
                    await self._finalize_row(result, item)
                    self.checkpoint.upsert_row(result)
                    self.processed += 1
            self._persist(checkpoint_path, output_path)
            if blocked:
                for item in reversed(blocked):
                    self.checkpoint.pending.insert(0, item.to_dict())
                self._persist(checkpoint_path, output_path)
                raise BlockedError(f"403 при загрузке {len(blocked)} карточек")
            logger.info("Детали: готово %s, осталось %s", len(self.checkpoint.rows), len(self.checkpoint.pending))

    async def _fetch_and_build_row(self, item: ProductListItem, stagger_index: int = 0) -> dict[str, Any]:
        row = build_base_row(item)
        details = await self.worker_pool.get_product_details(item.product_id, item.slug)
        return enrich_row_with_details(row, details, item.images)

    async def _finalize_row(self, row: dict[str, Any], item: ProductListItem) -> None:
        photo_urls = row.pop("photo_urls", None) or item.images
        gif_urls = row.pop("gif_urls", None) or []
        if not self.download_images_flag or not self.http:
            row["photo_urls"] = photo_urls
            row["gif_urls"] = gif_urls
            from db.hooks import persist_mvideo_row

            persist_mvideo_row(row, self.cat_dir)
            row.pop("photo_urls", None)
            row.pop("gif_urls", None)
            return
        urls = list(photo_urls or [])
        urls.extend(gif_urls or [])
        if urls:
            from storage_paths import canonical_image_dir

            img_dir = canonical_image_dir("mvideo", item.product_id)
            prefix = f"storage/images/mvideo/{item.product_id}"
            url = row.get("url") or BASE_URL
            local = await download_images(
                self.http, urls, img_dir, referer=url, path_prefix=prefix
            )
            row["local_images"] = " | ".join(local)
            row["local_image_count"] = len(local)
        row["photo_urls"] = photo_urls
        row["gif_urls"] = gif_urls
        from db.hooks import persist_mvideo_row

        persist_mvideo_row(row, self.cat_dir)
        row.pop("photo_urls", None)
        row.pop("gif_urls", None)

    def _persist(self, checkpoint_path: Path | None, output_path: Path | None) -> None:
        if checkpoint_path:
            self.checkpoint.save(checkpoint_path)
        if output_path and self.checkpoint.rows and len(self.checkpoint.rows) % self.save_every == 0:
            export_products(self.checkpoint.rows, output_path)
            logger.info("Промежуточное сохранение: %s -> %s", len(self.checkpoint.rows), output_path)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

# Приоритет обхода: названия с сайта -> фактические root из discovery (path.split(' > ')[0]).
# catalog_api даёт плоский верхний уровень, не совпадающий с меню сайта один-в-один.
MVIDEO_PRIORITY_ROOT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Смартфоны и гаджеты",
        (
            "Смартфоны и связь",
            "Гаджеты",
            "Аксессуары для телефонов",
            "Портативное аудио",
            "Наушники",
        ),
    ),
    (
        "Ноутбуки, планшеты, компьютеры",
        (
            "Ноутбуки, планшеты, компьютеры",
            "Компьютерная техника",
            "Компьютерные аксессуары",
            "Компьютерные комплектующие",
            "Периферийные устройства",
            "Планшеты и электронные книги",
        ),
    ),
    (
        "Телевизоры и цифровое ТВ",
        (
            "Телевизоры и цифровое ТВ",
            "Спутниковое/цифровое ТВ",
            "Аксессуары для телевизоров",
            "Видеотехника",
        ),
    ),
    (
        "Техника для кухни",
        (
            "Крупная кухонная техника",
            "Мелкая кухонная техника",
            "Встраиваемая техника",
            "Приготовление кофе",
        ),
    ),
    (
        "Техника для дома",
        (
            "Климатическая техника",
            "Стиральные и сушильные машины",
            "Холодильники и морозильники",
            "Пылесосы и аксессуары",
        ),
    ),
)

MVIDEO_PRIORITY_DISCOVERY_ROOTS: frozenset[str] = frozenset(
    root for _, roots in MVIDEO_PRIORITY_ROOT_GROUPS for root in roots
)


def category_discovery_root(category: Category) -> str:
    if not category.path:
        return ""
    return category.path.split(" > ", 1)[0].strip()


def sort_mvideo_categories_by_priority(categories: list[Category]) -> list[Category]:
    root_group_order = {
        root: (group_idx, root_idx)
        for group_idx, (_, roots) in enumerate(MVIDEO_PRIORITY_ROOT_GROUPS)
        for root_idx, root in enumerate(roots)
    }
    other_group_idx = len(MVIDEO_PRIORITY_ROOT_GROUPS)

    def sort_key(item: tuple[int, Category]) -> tuple[int, int, int]:
        index, category = item
        group_idx, root_idx = root_group_order.get(
            category_discovery_root(category),
            (other_group_idx, 0),
        )
        return group_idx, root_idx, index

    return [category for _, category in sorted(enumerate(categories), key=sort_key)]


def log_mvideo_priority_categories(categories: list[Category]) -> None:
    priority_count = 0
    per_discovery_root: dict[str, int] = {root: 0 for root in MVIDEO_PRIORITY_DISCOVERY_ROOTS}
    per_display_group: dict[str, int] = {display: 0 for display, _ in MVIDEO_PRIORITY_ROOT_GROUPS}

    for category in categories:
        root = category_discovery_root(category)
        if root not in MVIDEO_PRIORITY_DISCOVERY_ROOTS:
            continue
        priority_count += 1
        per_discovery_root[root] += 1
        for display, roots in MVIDEO_PRIORITY_ROOT_GROUPS:
            if root in roots:
                per_display_group[display] += 1
                break

    other_count = len(categories) - priority_count
    logger.info(
        "MVideo priority categories: priority=%s other=%s total=%s",
        priority_count,
        other_count,
        len(categories),
    )
    for display, roots in MVIDEO_PRIORITY_ROOT_GROUPS:
        parts = ", ".join(f"{root}={per_discovery_root[root]}" for root in roots if per_discovery_root[root])
        logger.info(
            "MVideo priority root %s: %s (total=%s)",
            display,
            parts or "none",
            per_display_group[display],
        )


async def discover_categories_for_run(
    list_client: MvideoApiClient,
    *,
    catalog_body: dict | None,
    cache_path: Path,
) -> tuple[list[Category], str]:
    # 1) catalog API (optional)
    try:
        categories = await list_client.get_catalog_from_catalog_api(catalog_body)
        if categories:
            save_categories_cache(cache_path, categories, source="catalog_api")
            return categories, "catalog_api"
    except BlockedError as exc:
        logger.warning("catalog discovery blocked: %s", exc)
    except Exception as exc:
        logger.warning("catalog discovery failed: %s", exc)

    # 2) sitemap (optional)
    try:
        categories = await list_client.get_catalog_from_sitemap()
        if categories:
            save_categories_cache(cache_path, categories, source="sitemap")
            return categories, "sitemap"
    except BlockedError as exc:
        logger.warning("sitemap discovery blocked: %s", exc)
    except Exception as exc:
        logger.warning("sitemap discovery failed: %s", exc)

    # 3) cache
    cached_categories, meta = load_categories_cache(cache_path)
    stale = is_cache_stale(meta)
    if cached_categories and not stale:
        logger.info(
            "categories cache used: count=%s updated_at=%s source=%s",
            len(cached_categories),
            (meta or {}).get("updated_at"),
            (meta or {}).get("source"),
        )
        return cached_categories, "cache_fresh"

    # 4) browser-link discovery (if no cache or stale cache)
    try:
        categories = await list_client.get_catalog_from_browser_links()
        if categories:
            save_categories_cache(cache_path, categories, source="browser_links")
            return categories, "browser_links"
    except Exception as exc:
        logger.warning("browser-link discovery failed: %s", exc)

    # fallback to stale cache if any
    if cached_categories:
        logger.warning(
            "using stale categories cache: count=%s updated_at=%s source=%s",
            len(cached_categories),
            (meta or {}).get("updated_at"),
            (meta or {}).get("source"),
        )
        return cached_categories, "cache_stale"

    return [], "none"


async def scrape_once(args: argparse.Namespace, *, retry_attempt: int = 1) -> Path:
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    global_cp_path = output_root / "checkpoint_global.json"
    global_cp: dict = {}
    if global_cp_path.exists():
        global_cp = json.loads(global_cp_path.read_text(encoding="utf-8"))
    completed_categories: set[str] = set(global_cp.get("completed_categories", []))
    needs_recheck_categories: set[str] = set(global_cp.get("needs_recheck_categories", []))

    last_output = output_root / "products.xlsx"
    profile_dir = str(output_root / ".browser_profile")

    logger.info("Режим: CloakBrowser (headless=%s)", args.headless)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as http:
            async with CloakSession(
                headless=args.headless,
                humanize=True,
                profile_dir=profile_dir,
            ) as cloak:
                context = cloak.context
                page, catalog_body = await warm_up_session(
                    context,
                    category_slug=args.category_slug or "",
                    category_id=args.category or "",
                    retry_attempt=retry_attempt - 1,
                    use_stealth=False,
                )
                rate_limiter = DomainRateLimiter(args.delay)
                list_client = MvideoApiClient(page, rate_limiter=rate_limiter)
                # Respect policy/user setting (already clamped in apply_mvideo_policy).
                effective_workers = max(1, args.workers)
                worker_pool = None
                if not args.skip_details:
                    worker_pool = MvideoWorkerPool(
                        context,
                        workers=effective_workers,
                        use_stealth=False,
                        rate_limiter=rate_limiter,
                    )
                    await worker_pool.start()
                    logger.info("Воркеров: %s", effective_workers)

                cache_path = output_root / "categories_cache.json"
                if args.category:
                    categories = [
                        Category(
                            int(args.category),
                            f"category-{args.category}",
                            args.category_slug or "",
                            f"category-{args.category}",
                        )
                    ]
                    logger.info(
                        "Категория задана вручную: id=%s slug=%s",
                        args.category,
                        args.category_slug or "",
                    )
                else:
                    categories, discovery_source = await discover_categories_for_run(
                        list_client,
                        catalog_body=catalog_body,
                        cache_path=cache_path,
                    )
                    logger.info("Категорий: %s (source=%s)", len(categories), discovery_source)
                    categories = sort_mvideo_categories_by_priority(categories)
                    log_mvideo_priority_categories(categories)
                if args.max_categories:
                    categories = categories[: args.max_categories]
                categories_by_id: dict[str, Category] = {str(c.seo_id): c for c in categories}
                deferred_recheck_order: list[str] = []
                fast_defer_config = MvideoFastDeferConfig.from_env()
                session_403 = Session403Tracker(fast_defer_config)
                if fast_defer_config.fast_defer:
                    logger.info(
                        "MVideo fast defer: ON (retries=%s, recheck_delay=%s min)",
                        fast_defer_config.defer_after_retry,
                        fast_defer_config.recheck_delay_minutes,
                    )

                total_discovered = 0
                total_processed = 0
                total_skipped = 0
                total_failed = 0
                for index, category in enumerate(categories, 1):
                    cat_key = str(category.seo_id)
                    if (
                        cat_key in completed_categories
                        and not args.details_only
                        and not args.retry_details_errors
                    ):
                        logger.info("Пропуск (готово): %s", category.path)
                        continue
                    if cat_key in needs_recheck_categories and not is_403_recheck_ready(global_cp, cat_key):
                        logger.info(
                            "MVideo deferred пока не готов к recheck: category=%s next_retry_at=%s",
                            category.path,
                            category_403_next_retry_at(global_cp, cat_key) or "?",
                        )
                        continue

                    cat_dir = category_dir(
                        output_root,
                        category.path if not category.path.startswith("category-") else (
                            (args.category_slug or category.slug or category.path).replace("-", " ").title()
                        ),
                        cat_key,
                    )
                    cp_path = cat_dir / "checkpoint.json"
                    out_path = cat_dir / "products.xlsx"
                    checkpoint = CheckpointState.load(cp_path)
                    if checkpoint is None:
                        checkpoint = CheckpointState(output=str(out_path))
                    else:
                        logger.info(
                            "Возобновление %s: готово %s, очередь %s",
                            category.path,
                            len(checkpoint.completed_ids),
                            len(checkpoint.pending),
                        )

                    if args.retry_details_errors or args.details_only:
                        requeued = requeue_failed_details_in_checkpoint(checkpoint)
                        if requeued:
                            logger.info(
                                "Requeued %s products for detail retry: %s",
                                requeued,
                                category.path,
                            )
                        if cat_key in completed_categories and checkpoint.pending:
                            completed_categories.discard(cat_key)
                            logger.info(
                                "Re-open completed category for pending details: %s",
                                category.path,
                            )

                    if args.details_only and not checkpoint.pending:
                        logger.info("Details-only skip (nothing pending): %s", category.path)
                        continue

                    scraper = MvideoScraper(
                        list_client,
                        worker_pool,
                        checkpoint,
                        fetch_details=not args.skip_details,
                        http_session=http,
                        cat_dir=cat_dir,
                        download_images_flag=not args.skip_images,
                        category_delay=args.category_delay,
                        rate_limiter=rate_limiter,
                        details_only=args.details_only,
                    )
                    logger.info("[%s/%s] %s (id=%s)", index, len(categories), category.path, category.seo_id)

                    cat_backoff = BlockBackoff()
                    cat_block_handler = CategoryBlockHandler(fast_defer_config)
                    category_done = False
                    while not category_done:
                        try:
                            summary = await scraper.scrape_category(
                                cat_key,
                                category.name,
                                category.path,
                                category_slug=category.slug,
                                max_products=args.limit,
                                checkpoint_path=cp_path,
                                output_path=out_path,
                            )
                            total_discovered += summary["discovered_urls"]
                            total_processed += summary["processed"]
                            total_skipped += summary["skipped"]
                            total_failed += summary["failed"]
                            category_done = True
                            session_403.on_success()
                        except BlockedError as exc:
                            checkpoint.save(cp_path)
                            if checkpoint.rows:
                                export_products(checkpoint.rows, out_path)
                            if fast_defer_config.fast_defer:
                                action = await cat_block_handler.on_blocked()
                                if action == "retry":
                                    logger.info(
                                        "Повтор категории %s после короткого backoff",
                                        category.path,
                                    )
                                    continue
                                next_retry_at = defer_category_403(
                                    global_cp,
                                    cat_key,
                                    retry_count=cat_block_handler.blocked_retries,
                                    pending_count=len(checkpoint.pending),
                                    recheck_delay_minutes=fast_defer_config.recheck_delay_minutes,
                                )
                                needs_recheck_categories.add(cat_key)
                                if cat_key not in deferred_recheck_order:
                                    deferred_recheck_order.append(cat_key)
                                global_cp["completed_categories"] = sorted(completed_categories)
                                atomic_write_json(global_cp_path, global_cp)
                                logger.warning(
                                    "MVideo FAST DEFER: category=%s pending=%s next_retry_at=%s",
                                    category.path,
                                    len(checkpoint.pending),
                                    next_retry_at,
                                )
                                total_failed += 1
                                if session_403.on_defer():
                                    logger.warning(
                                        "MVideo consecutive 403 deferrals=%s; session cooldown %s sec",
                                        fast_defer_config.consecutive_403_cooldown_after,
                                        fast_defer_config.session_cooldown_seconds,
                                    )
                                    await asyncio.sleep(fast_defer_config.session_cooldown_seconds)
                                    session_403.reset_after_cooldown()
                                break
                            if not await cat_backoff.wait_before_retry():
                                logger.error(
                                    "Категория %s пропущена после блокировок: %s",
                                    category.path,
                                    exc,
                                )
                                total_failed += 1
                                break
                            logger.info("Повтор категории %s после backoff", category.path)
                        except Exception as exc:
                            checkpoint.save(cp_path)
                            if checkpoint.rows:
                                export_products(checkpoint.rows, out_path)
                            if is_recoverable_category_error(exc):
                                logger.error(
                                    "Категория %s изолирована (recoverable): %s",
                                    category.path,
                                    exc,
                                )
                                total_failed += 1
                                break
                            raise

                    if not category_done:
                        continue

                    if checkpoint.rows:
                        last_output = export_products(checkpoint.rows, out_path)
                    checkpoint.save(cp_path)
                    if summary.get("deferred_empty"):
                        needs_recheck_categories.add(cat_key)
                        if cat_key not in deferred_recheck_order:
                            deferred_recheck_order.append(cat_key)
                        global_cp["needs_recheck_categories"] = sorted(needs_recheck_categories)
                        atomic_write_json(global_cp_path, global_cp)
                        logger.warning(
                            "Категория отложена на recheck: %s (reason=%s)",
                            category.path,
                            summary.get("deferred_reason") or "unknown",
                        )
                        total_failed += 1
                        if index < len(categories):
                            await rate_limit_delay(args.category_delay)
                        continue
                    completed_categories.add(cat_key)
                    clear_category_403_defer(global_cp, cat_key)
                    needs_recheck_categories.discard(cat_key)
                    global_cp["completed_categories"] = sorted(completed_categories)
                    global_cp["needs_recheck_categories"] = sorted(needs_recheck_categories)
                    atomic_write_json(global_cp_path, global_cp)
                    logger.info("Категория готова: %s -> %s", category.path, cat_dir)
                    if index < len(categories):
                        await rate_limit_delay(args.category_delay)

                if deferred_recheck_order:
                    logger.info(
                        "Запуск recheck для deferred категорий: %s",
                        len(deferred_recheck_order),
                    )
                for cat_key in deferred_recheck_order:
                    if cat_key in completed_categories:
                        continue
                    category = categories_by_id.get(cat_key)
                    if category is None:
                        continue
                    if not is_403_recheck_ready(global_cp, cat_key):
                        logger.info(
                            "MVideo deferred пока не готов к recheck: category=%s next_retry_at=%s",
                            category.path,
                            category_403_next_retry_at(global_cp, cat_key) or "?",
                        )
                        global_cp["needs_recheck_categories"] = sorted(needs_recheck_categories)
                        atomic_write_json(global_cp_path, global_cp)
                        continue
                    cat_dir = category_dir(
                        output_root,
                        category.path if not category.path.startswith("category-") else (
                            (args.category_slug or category.slug or category.path).replace("-", " ").title()
                        ),
                        cat_key,
                    )
                    cp_path = cat_dir / "checkpoint.json"
                    out_path = cat_dir / "products.xlsx"
                    checkpoint = CheckpointState.load(cp_path) or CheckpointState(output=str(out_path))
                    if args.retry_details_errors or args.details_only:
                        requeued = requeue_failed_details_in_checkpoint(checkpoint)
                        if requeued:
                            logger.info(
                                "Requeued %s products for detail retry (recheck): %s",
                                requeued,
                                category.path,
                            )
                    scraper = MvideoScraper(
                        list_client,
                        worker_pool,
                        checkpoint,
                        fetch_details=not args.skip_details,
                        http_session=http,
                        cat_dir=cat_dir,
                        download_images_flag=not args.skip_images,
                        category_delay=args.category_delay,
                        rate_limiter=rate_limiter,
                        details_only=args.details_only,
                    )
                    logger.info("MVideo deferred recheck: category=%s", category.path)
                    recheck_block_handler = CategoryBlockHandler(fast_defer_config)
                    recheck_done = False
                    while not recheck_done:
                        try:
                            summary = await scraper.scrape_category(
                                cat_key,
                                category.name,
                                category.path,
                                category_slug=category.slug,
                                max_products=args.limit,
                                checkpoint_path=cp_path,
                                output_path=out_path,
                            )
                            total_discovered += summary["discovered_urls"]
                            total_processed += summary["processed"]
                            total_skipped += summary["skipped"]
                            total_failed += summary["failed"]
                            checkpoint.save(cp_path)
                            if summary.get("deferred_empty"):
                                needs_recheck_categories.add(cat_key)
                                logger.warning(
                                    "Категория остаётся deferred после recheck: %s (reason=%s)",
                                    category.path,
                                    summary.get("deferred_reason") or "unknown",
                                )
                                total_failed += 1
                            else:
                                if checkpoint.rows:
                                    last_output = export_products(checkpoint.rows, out_path)
                                completed_categories.add(cat_key)
                                clear_category_403_defer(global_cp, cat_key)
                                needs_recheck_categories.discard(cat_key)
                                logger.info("Категория подтверждена после recheck: %s", category.path)
                            recheck_done = True
                        except BlockedError as exc:
                            checkpoint.save(cp_path)
                            if checkpoint.rows:
                                export_products(checkpoint.rows, out_path)
                            if fast_defer_config.fast_defer:
                                action = await recheck_block_handler.on_blocked()
                                if action == "retry":
                                    continue
                                next_retry_at = defer_category_403(
                                    global_cp,
                                    cat_key,
                                    retry_count=recheck_block_handler.blocked_retries,
                                    pending_count=len(checkpoint.pending),
                                    recheck_delay_minutes=fast_defer_config.recheck_delay_minutes,
                                )
                                needs_recheck_categories.add(cat_key)
                                total_failed += 1
                                logger.warning(
                                    "MVideo FAST DEFER: category=%s pending=%s next_retry_at=%s",
                                    category.path,
                                    len(checkpoint.pending),
                                    next_retry_at,
                                )
                            else:
                                needs_recheck_categories.add(cat_key)
                                total_failed += 1
                                logger.error("RECHECK failed for %s: %s", category.path, exc)
                            break
                        except Exception as exc:
                            checkpoint.save(cp_path)
                            needs_recheck_categories.add(cat_key)
                            total_failed += 1
                            logger.error("RECHECK failed for %s: %s", category.path, exc)
                            break
                    global_cp["completed_categories"] = sorted(completed_categories)
                    global_cp["needs_recheck_categories"] = sorted(needs_recheck_categories)
                    atomic_write_json(global_cp_path, global_cp)
                    await rate_limit_delay(4)

                if worker_pool:
                    await worker_pool.close()
                logger.info(
                    "SUMMARY run discovered=%s processed=%s skipped=%s failed=%s",
                    total_discovered,
                    total_processed,
                    total_skipped,
                    total_failed,
                )
    except BlockedError:
        raise

    logger.info("M.Video завершён -> %s", last_output)
    return last_output


async def run_with_retries(args: argparse.Namespace) -> Path:
    # M.Video API can temporarily hard-block catalog endpoint (403).
    # Allow longer recovery window for full runs.
    session_backoff = BlockBackoff(max_attempts=4)
    cooldown_round = 0
    while True:
        try:
            return await scrape_once(args)
        except BlockedError as exc:
            if await session_backoff.wait_before_retry():
                logger.warning("Перезапуск сессии после блокировки: %s", exc)
                continue
            cooldown_round += 1
            cooldown_sec = min(900, 300 * cooldown_round)
            logger.error(
                "Исчерпан retry budget (%s). Длинный cooldown %s сек и повтор.",
                session_backoff.max_attempts,
                cooldown_sec,
            )
            await asyncio.sleep(cooldown_sec)
            session_backoff.reset()
            logger.info("Возобновление M.Video после cooldown")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Парсер mvideo.ru — один файл",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python mvideo_parser.py --category 1263 --category-slug monitory -o monitory.xlsx
  python mvideo_parser.py --category 118 --category-slug noutbuki --limit 10
  python mvideo_parser.py --skip-details --category 1263 --category-slug monitory
  python mvideo_parser.py --details-only --retry-details-errors -o output/mvideo
        """,
    )
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--category", help="ID категории (seoId), напр. 1263 = мониторы")
    parser.add_argument("--category-slug", help="Slug для URL, напр. monitory")
    parser.add_argument("--limit", type=int, help="Лимит товаров на категорию")
    parser.add_argument("--max-categories", type=int)
    parser.add_argument("--skip-details", action="store_true", help="Без характеристик (быстрее)")
    parser.add_argument(
        "--details-only",
        action="store_true",
        help="Только detail-этап: без listing, обработка pending и retry",
    )
    parser.add_argument(
        "--retry-details-errors",
        action="store_true",
        help="Перепоставить в очередь товары без описания/details_error",
    )
    parser.add_argument("--skip-images", action="store_true", help="Не скачивать картинки")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS,
                        help="Мин. интервал между page/API запросами, сек (~3–5 при 4)")
    parser.add_argument("--category-delay", type=float, default=CATEGORY_DELAY_SECONDS,
                        help="Пауза между категориями, сек (~15–25 при 20)")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.skip_details and args.details_only:
        raise SystemExit("Нельзя совмещать --skip-details и --details-only")
    from parser_policy import apply_mvideo_policy

    apply_mvideo_policy(args)
    asyncio.run(run_with_retries(args))


if __name__ == "__main__":
    main()
