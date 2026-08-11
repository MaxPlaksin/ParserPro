#!/usr/bin/env python3
"""Verify Citilink catalog manifest (C5.2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "output" / "citilink" / "catalog_manifest.json"


def main() -> None:
    if not MANIFEST.exists():
        print(f"Manifest not found: {MANIFEST}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    categories = data.get("categories") or []
    leaves = [c for c in categories if c.get("is_leaf")]

    url_dups = {}
    id_dups = {}
    for c in categories:
        url = c.get("category_url", "")
        cid = c.get("category_id", "")
        url_dups.setdefault(url, []).append(c.get("slug"))
        id_dups.setdefault(cid, []).append(c.get("slug"))

    with_count = [c for c in leaves if (c.get("product_count") or 0) > 0]
    estimated_total = sum(int(c.get("product_count") or 0) for c in with_count)

    print("=== C5.2 MANIFEST CHECK ===")
    print(f"TOTAL_CATEGORIES={len(categories)}")
    print(f"LEAF_CATEGORIES={len(leaves)}")
    print(f"CATEGORIES_WITH_PRODUCT_COUNT={len(with_count)}")
    print(f"ESTIMATED_TOTAL_LISTING_PRODUCTS={estimated_total}")
    print(f"DUPLICATE_URLS={sum(1 for v in url_dups.values() if len(v) > 1)}")
    print(f"DUPLICATE_IDS={sum(1 for v in id_dups.values() if len(v) > 1)}")

    empty = [c for c in categories if not (c.get("name") or "").strip()]
    print(f"EMPTY_NAMES={len(empty)}")

    print("\n=== SAMPLE (20 leaf categories) ===")
    sample = sorted(with_count, key=lambda x: -(x.get("product_count") or 0))[:20]
    for c in sample:
        print(
            json.dumps(
                {
                    "category_id": c.get("category_id"),
                    "slug": c.get("slug"),
                    "name": c.get("name"),
                    "path": c.get("category_path"),
                    "product_count": c.get("product_count"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
