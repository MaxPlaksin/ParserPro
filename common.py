"""Shared utilities for DNS and M.Video parsers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

# Rate limiting (сек) — центр диапазона; jitter ±25% даёт нужный коридор
DEFAULT_REQUEST_DELAY = 4.0       # М.Видео ~3–5 сек между page/API запросами
DEFAULT_CATEGORY_DELAY = 20.0     # М.Видео ~15–25 сек между категориями
DEFAULT_IMAGE_DELAY = 1.0         # картинки ~0.8–1.5 сек
IMAGE_MAX_CONCURRENT = 2
BACKOFF_STEPS_SEC = (30, 60, 120, 240)
MAX_BLOCK_RETRIES = 4
RATE_JITTER = 0.25

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write file atomically via temp + os.replace (safe on crash/403)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Any, *, indent: int | None = 2) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    atomic_write_text(path, text)


def safe_name(text: str, *, max_len: int = 120) -> str:
    cleaned = INVALID_CHARS.sub("_", text.strip())
    cleaned = WHITESPACE.sub(" ", cleaned).strip(" .")
    return (cleaned or "unknown")[:max_len]


def category_dir(base: Path, category_path: str, category_id: str = "") -> Path:
    parts = [safe_name(p) for p in category_path.split(" > ") if p.strip()]
    if not parts:
        parts = [safe_name(category_id or "category")]
    return base.joinpath(*parts)


def product_images_dir(category_folder: Path, product_id: str) -> Path:
    return category_folder / "images" / safe_name(product_id)


def image_extension(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".gifv"):
        if path.endswith(ext):
            return ".gif" if ext == ".gifv" else ext
    return ".jpg"


async def rate_limit_delay(base: float, *, spread: float = RATE_JITTER) -> None:
    """Пауза rate limit с jitter (не «имитация человека», а ограничение частоты)."""
    if base <= 0:
        return
    low = max(0.2, base * (1 - spread))
    high = base * (1 + spread)
    await asyncio.sleep(random.uniform(low, high))


# alias для совместимости
human_delay = rate_limit_delay


class DomainRateLimiter:
    """Общий rate limiter на домен: не чаще одного запроса за min_interval (±jitter)."""

    def __init__(self, min_interval: float, *, spread: float = RATE_JITTER):
        self.min_interval = min_interval
        self.spread = spread
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if now < self._next_at:
                await asyncio.sleep(self._next_at - now)
            pause = self.min_interval * random.uniform(1 - self.spread, 1 + self.spread)
            self._next_at = loop.time() + pause


class BlockBackoff:
    """Backoff при 403/429/captcha: 30 → 60 → 120 → 240 сек (+ jitter)."""

    def __init__(
        self,
        steps: tuple[float, ...] = BACKOFF_STEPS_SEC,
        max_attempts: int = MAX_BLOCK_RETRIES,
    ):
        self.steps = steps
        self.max_attempts = max_attempts
        self.attempt = 0

    def reset(self) -> None:
        self.attempt = 0

    async def wait_before_retry(self) -> bool:
        """True — можно повторить; False — лимит исчерпан, пропустить категорию."""
        if self.attempt >= self.max_attempts:
            return False
        base = self.steps[min(self.attempt, len(self.steps) - 1)]
        delay = base * random.uniform(0.85, 1.15)
        logger.warning(
            "Backoff %s/%s: пауза %.0f сек перед повтором",
            self.attempt + 1,
            self.max_attempts,
            delay,
        )
        await asyncio.sleep(delay)
        self.attempt += 1
        return True


async def download_images(
    session: aiohttp.ClientSession,
    urls: list[str],
    dest_dir: Path,
    *,
    referer: str = "",
    image_delay: float = DEFAULT_IMAGE_DELAY,
    max_concurrent: int = IMAGE_MAX_CONCURRENT,
    path_prefix: str = "",
) -> list[str]:
    """Download images; return paths relative to project root (storage/images/...)."""
    from storage_paths import PROJECT_ROOT, image_storage_relpath

    dest_dir.mkdir(parents=True, exist_ok=True)
    if not path_prefix:
        try:
            rel = dest_dir.relative_to(PROJECT_ROOT).as_posix()
            if rel.startswith("storage/images/"):
                path_prefix = rel
        except ValueError:
            path_prefix = ""

    saved: list[str] = []
    headers = {"User-Agent": "Mozilla/5.0", "Referer": referer} if referer else {"User-Agent": "Mozilla/5.0"}
    sem = asyncio.Semaphore(max(1, max_concurrent))
    img_limiter = DomainRateLimiter(image_delay)

    async def fetch_one(index: int, url: str) -> str | None:
        if not url:
            return None
        ext = image_extension(url)
        filename = f"{index:03d}{ext}"
        target = dest_dir / filename
        if target.exists() and target.stat().st_size > 0:
            if path_prefix:
                return f"{path_prefix}/{filename}"
            return str(target.relative_to(PROJECT_ROOT).as_posix())
        async with sem:
            await img_limiter.wait()
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        logger.warning("Image HTTP %s: %s", resp.status, url[:120])
                        return None
                    data = await resp.read()
                    if len(data) < 100:
                        return None
                    target.write_bytes(data)
                    if path_prefix:
                        return f"{path_prefix}/{filename}"
                    return str(target.relative_to(PROJECT_ROOT).as_posix())
            except Exception as exc:
                logger.warning("Image download failed %s: %s", url[:80], exc)
                return None

    results = await asyncio.gather(*(fetch_one(i, u) for i, u in enumerate(urls, 1)))
    saved = [r for r in results if r]
    return saved


def export_products(rows: list[dict], output_path: Path) -> Path:
    if not rows:
        return output_path
    df = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == ".csv":
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        return output_path
    if output_path.suffix.lower() not in {".xlsx", ".xls"}:
        output_path = output_path.with_suffix(".xlsx")
    try:
        df.to_excel(output_path, index=False, engine="openpyxl")
    except PermissionError:
        output_path = output_path.with_name(f"{output_path.stem}_new{output_path.suffix}")
        df.to_excel(output_path, index=False, engine="openpyxl")
    return output_path


@dataclass
class CategoryProgress:
    category_id: str
    category_name: str
    category_path: str
    category_slug: str = ""
    category_url: str = ""
    list_cursor: str = ""
    list_page: int = 1
    list_complete: bool = False


@dataclass
class GlobalCheckpoint:
    version: int = 3
    shop: str = ""
    output_root: str = ""
    current_category_index: int = 0
    categories_total: int = 0
    categories: list[dict[str, Any]] = field(default_factory=list)
    category: CategoryProgress | None = None
    completed_ids: list[str] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed_set(self) -> set[str]:
        return set(self.completed_ids)

    def add_row(self, row: dict[str, Any]) -> None:
        product_id = str(row.get("product_id", ""))
        if not product_id or product_id in self.completed_set:
            return
        self.completed_ids.append(product_id)
        self.rows.append(row)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "shop": self.shop,
            "output_root": self.output_root,
            "current_category_index": self.current_category_index,
            "categories_total": self.categories_total,
            "categories": self.categories,
            "category": asdict(self.category) if self.category else None,
            "completed_ids": self.completed_ids,
            "pending": self.pending,
            "rows_count": len(self.rows),
        }
        atomic_write_json(path, payload)
        atomic_write_json(path.with_suffix(".rows.json"), self.rows, indent=None)

    @classmethod
    def load(cls, path: Path) -> GlobalCheckpoint | None:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows_path = path.with_suffix(".rows.json")
        rows: list[dict[str, Any]] = []
        if rows_path.exists():
            rows = json.loads(rows_path.read_text(encoding="utf-8"))
        cat = payload.get("category")
        return cls(
            version=payload.get("version", 3),
            shop=payload.get("shop", ""),
            output_root=payload.get("output_root", ""),
            current_category_index=payload.get("current_category_index", 0),
            categories_total=payload.get("categories_total", 0),
            categories=payload.get("categories", []),
            category=CategoryProgress(**cat) if cat else None,
            completed_ids=payload.get("completed_ids", []),
            pending=payload.get("pending", []),
            rows=rows,
        )


class CloakSession:
    """CloakBrowser session (Playwright API)."""

    def __init__(
        self,
        *,
        headless: bool = False,
        humanize: bool = True,
        profile_dir: str = "",
    ):
        self.headless = headless
        self.humanize = humanize
        self.profile_dir = profile_dir
        self._browser = None
        self._context = None

    async def __aenter__(self):
        from cloakbrowser import launch_async, launch_persistent_context_async

        if self.profile_dir:
            Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
            self._context = await launch_persistent_context_async(
                self.profile_dir,
                headless=self.headless,
                humanize=self.humanize,
                locale="ru-RU",
                viewport=None if not self.headless else {"width": 1920, "height": 1080},
            )
        else:
            self._browser = await launch_async(
                headless=self.headless,
                humanize=self.humanize,
            )
            self._context = await self._browser.new_context(locale="ru-RU")

        return self

    async def __aexit__(self, *_args):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()

    @property
    def context(self):
        if self._context is None:
            raise RuntimeError("CloakSession not started")
        return self._context

    async def new_page(self):
        return await self.context.new_page()
