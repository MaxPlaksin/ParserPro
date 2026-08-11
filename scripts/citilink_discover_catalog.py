#!/usr/bin/env python3
"""Discover Citilink catalog categories (read-only, no DB writes)."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from common import CloakSession, DomainRateLimiter, atomic_write_json
from parser_policy import apply_citilink_policy

BASE_URL = "https://www.citilink.ru"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "citilink"
SKIP_SLUGS = {
    "podarochnye-sertifikaty",
    "catalog",
    "search",
    "compare",
    "wishlist",
}

EXTRACT_CATEGORY_PAGE_JS = """
() => {
  const st = window.__NEXT_DATA__?.props?.pageProps?.initialState?.subcategory || {};
  const cats = st.categories?.payload?.categories || {};
  const cat = cats.category || {};
  const parents = cats.parents || [];
  const found = st.filters?.filter?.foundProductsCount ?? null;
  const childSlugs = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/catalog/"]')) {
    const href = (a.href || '').split('?')[0].split('#')[0];
    const m = href.match(/\\/catalog\\/([^/?#]+)\\/?$/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    childSlugs.push(m[1]);
  }
  const pathParts = [];
  for (const p of parents) if (p && p.name) pathParts.push(String(p.name).trim());
  if (cat.name) pathParts.push(String(cat.name).trim());
  return {
    category_id: cat.id != null ? String(cat.id) : '',
    slug: String(cat.slug || ''),
    name: String(cat.name || ''),
    isListing: !!cat.isListing,
    parents: parents,
    category_path: pathParts.join(' > '),
    product_count: found != null ? Number(found) : null,
    child_slugs: childSlugs,
  };
}
"""

EXTRACT_CATALOG_LINKS_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/catalog/"]')) {
    const href = (a.href || '').split('?')[0].split('#')[0];
    const m = href.match(/\\/catalog\\/([^/?#]+)\\/?$/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    out.push(m[1]);
  }
  return out;
}
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("citilink_discover")


def category_url(slug: str) -> str:
    return f"{BASE_URL}/catalog/{slug.strip('/')}/"


def should_enqueue(slug: str) -> bool:
    if not slug or slug in SKIP_SLUGS:
        return False
    if "--" in slug:
        return False
    return True


def merge_category(existing: dict[str, Any] | None, data: dict[str, Any], visited_slug: str) -> dict[str, Any]:
    parents = data.get("parents") or []
    parent_ids = [str(p.get("id", "")) for p in parents if isinstance(p, dict)]
    parent_names = [str(p.get("name", "")) for p in parents if isinstance(p, dict)]
    entry = {
        "category_id": str(data.get("category_id") or ""),
        "slug": str(data.get("slug") or visited_slug),
        "name": str(data.get("name") or ""),
        "category_path": str(data.get("category_path") or ""),
        "category_url": category_url(str(data.get("slug") or visited_slug)),
        "is_listing": bool(data.get("isListing")),
        "is_leaf": bool(data.get("isListing")),
        "parent_ids": parent_ids,
        "parent_names": parent_names,
        "parents": parents,
        "product_count": data.get("product_count"),
        "visited_slugs": [visited_slug],
    }
    if not existing:
        return entry
    merged = {**existing}
    if not merged.get("name") and entry["name"]:
        merged["name"] = entry["name"]
    if not merged.get("category_path") and entry["category_path"]:
        merged["category_path"] = entry["category_path"]
    if entry["product_count"] is not None:
        merged["product_count"] = entry["product_count"]
    merged["is_listing"] = merged.get("is_listing") or entry["is_listing"]
    merged["is_leaf"] = merged.get("is_leaf") or entry["is_leaf"]
    slugs = set(merged.get("visited_slugs") or [])
    slugs.add(visited_slug)
    merged["visited_slugs"] = sorted(slugs)
    return merged


def write_manifest(output_root: Path, by_id: dict[str, dict[str, Any]], visited_count: int) -> dict[str, Any]:
    categories = sorted(by_id.values(), key=lambda x: (x.get("category_path") or "", x.get("slug") or ""))
    leaf_categories = [c for c in categories if c.get("is_leaf")]
    manifest = {
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "source": "citilink.ru catalog BFS via __NEXT_DATA__",
        "total_slugs_visited": visited_count,
        "total_categories": len(categories),
        "leaf_categories_count": len(leaf_categories),
        "categories": categories,
    }
    json_path = output_root / "catalog_manifest.json"
    csv_path = output_root / "catalog_manifest.csv"
    atomic_write_json(json_path, manifest)
    fieldnames = [
        "category_id",
        "slug",
        "name",
        "category_path",
        "category_url",
        "is_leaf",
        "is_listing",
        "product_count",
        "parent_ids",
        "parent_names",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in categories:
            writer.writerow(
                {
                    "category_id": row.get("category_id", ""),
                    "slug": row.get("slug", ""),
                    "name": row.get("name", ""),
                    "category_path": row.get("category_path", ""),
                    "category_url": row.get("category_url", ""),
                    "is_leaf": row.get("is_leaf", False),
                    "is_listing": row.get("is_listing", False),
                    "product_count": row.get("product_count", ""),
                    "parent_ids": "|".join(row.get("parent_ids") or []),
                    "parent_names": " > ".join(row.get("parent_names") or []),
                }
            )
    logger.info("Manifest saved: %s (%s categories, %s leaf)", json_path, len(categories), len(leaf_categories))
    return manifest


async def discover(args: argparse.Namespace) -> dict[str, Any]:
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    progress_path = output_root / "catalog_discovery_progress.json"

    if args.finalize_only:
        if not progress_path.exists():
            raise SystemExit(f"No progress file: {progress_path}")
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        by_id = progress.get("by_id", {})
        return write_manifest(output_root, by_id, len(progress.get("visited_slugs", [])))

    visited_slugs: set[str] = set()
    queue: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}

    if progress_path.exists() and args.resume:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        visited_slugs = set(progress.get("visited_slugs", []))
        by_id = progress.get("by_id", {})
        if args.expand_children:
            queue = list(progress.get("queue", []))

    rate_limiter = DomainRateLimiter(args.delay)
    profile = str(output_root / f".browser_profile{args.profile_suffix}")

    async with CloakSession(headless=args.headless, humanize=True, profile_dir=profile) as session:
        page = await session.new_page()

        await rate_limiter.wait()
        await page.goto(f"{BASE_URL}/catalog/", wait_until="load", timeout=120_000)
        await asyncio.sleep(args.settle)
        seeds = await page.evaluate(EXTRACT_CATALOG_LINKS_JS) or []
        seed_slugs = [s for s in seeds if should_enqueue(s)]

        if not queue:
            queue = [s for s in seed_slugs if s not in visited_slugs]
            logger.info("Seed slugs from /catalog/: %s (queue=%s)", len(seed_slugs), len(queue))
        elif not args.expand_children:
            queue = [s for s in seed_slugs if s not in visited_slugs]
            logger.info("Rebuild seed queue: %s remaining", len(queue))

        while queue:
            if args.max_visits and len(visited_slugs) >= args.max_visits:
                logger.info("Reached max visits: %s", args.max_visits)
                break

            slug = queue.pop(0)
            if slug in visited_slugs or not should_enqueue(slug):
                continue

            url = category_url(slug)
            await rate_limiter.wait()
            try:
                response = await page.goto(url, wait_until="load", timeout=120_000)
                await asyncio.sleep(args.settle)
                if response and response.status in {403, 429}:
                    logger.warning("Blocked %s: HTTP %s", slug, response.status)
                    await asyncio.sleep(30)
                    queue.insert(0, slug)
                    continue
                data = await page.evaluate(EXTRACT_CATEGORY_PAGE_JS) or {}
            except Exception as exc:
                logger.warning("Failed %s: %s", slug, exc)
                visited_slugs.add(slug)
                continue

            visited_slugs.add(slug)
            cat_id = str(data.get("category_id") or "")
            if cat_id:
                by_id[cat_id] = merge_category(by_id.get(cat_id), data, slug)
            else:
                logger.warning("No category_id for slug %s", slug)

            if args.expand_children:
                for child in data.get("child_slugs") or []:
                    if should_enqueue(child) and child not in visited_slugs and child not in queue:
                        queue.append(child)

            if len(visited_slugs) % 10 == 0:
                atomic_write_json(
                    progress_path,
                    {"visited_slugs": sorted(visited_slugs), "queue": queue, "by_id": by_id},
                )
                logger.info("Progress: visited=%s queue=%s categories=%s", len(visited_slugs), len(queue), len(by_id))

    categories = sorted(by_id.values(), key=lambda x: (x.get("category_path") or "", x.get("slug") or ""))
    manifest = write_manifest(output_root, by_id, len(visited_slugs))
    if progress_path.exists():
        progress_path.unlink(missing_ok=True)
    return manifest


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Discover Citilink catalog categories")
    p.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--delay", type=float, default=4.0)
    p.add_argument("--category-delay", type=float, default=20.0)
    p.add_argument("--settle", type=float, default=5.0)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--max-visits", type=int, help="Limit slug visits (debug)")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--profile-suffix", default="", help="Browser profile suffix")
    p.add_argument(
        "--finalize-only",
        action="store_true",
        help="Build manifest from catalog_discovery_progress.json without browser",
    )
    p.add_argument(
        "--expand-children",
        action="store_true",
        help="Enqueue child slugs from category pages (default: seed-only from /catalog/)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    apply_citilink_policy(args)
    manifest = asyncio.run(discover(args))
    print(json.dumps({
        "total_categories": manifest["total_categories"],
        "leaf_categories": manifest["leaf_categories_count"],
        "total_slugs_visited": manifest["total_slugs_visited"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
