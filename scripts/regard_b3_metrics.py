#!/usr/bin/env python3
"""B3 metrics report for Regard parser test."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
STORAGE = PROJECT_ROOT / "storage" / "images" / "regard"


def main() -> None:
    if not DB_PATH.exists():
        print("DB not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    regard_count = conn.execute("SELECT COUNT(*) c FROM products WHERE source='regard'").fetchone()["c"]

    char_rows = conn.execute(
        """
        SELECT p.id, LENGTH(pc.characteristics_json) len, pc.characteristics_json
        FROM products p
        JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source='regard'
        """
    ).fetchall()
    char_counts = []
    for row in char_rows:
        try:
            char_counts.append(len(json.loads(row["characteristics_json"])))
        except json.JSONDecodeError:
            char_counts.append(0)

    with_desc = conn.execute(
        """
        SELECT COUNT(*) c FROM products
        WHERE source='regard'
          AND (
            LENGTH(COALESCE(description,'')) > 0
            OR LENGTH(COALESCE(description_detailed,'')) > 0
            OR LENGTH(COALESCE(description_full,'')) > 0
          )
        """
    ).fetchone()["c"]

    extras = conn.execute(
        "SELECT extra_json FROM products WHERE source='regard' AND extra_json IS NOT NULL AND extra_json != '{}'"
    ).fetchall()
    with_brand = conn.execute(
        "SELECT COUNT(*) c FROM products WHERE source='regard' AND TRIM(COALESCE(brand,'')) != ''"
    ).fetchone()["c"]
    with_model = 0
    with_vendor = 0
    for row in extras:
        try:
            data = json.loads(row["extra_json"])
        except json.JSONDecodeError:
            continue
        if data.get("model"):
            with_model += 1
        if data.get("vendor_code"):
            with_vendor += 1

    image_db = conn.execute(
        """
        SELECT COUNT(*) c FROM product_images pi
        JOIN products p ON p.id = pi.product_id
        WHERE p.source='regard'
        """
    ).fetchone()["c"]

    local_files = 0
    if STORAGE.is_dir():
        for d in STORAGE.iterdir():
            if d.is_dir():
                local_files += sum(1 for f in d.iterdir() if f.is_file())

    failed = conn.execute(
        """
        SELECT COUNT(*) c FROM products
        WHERE source='regard' AND (
          TRIM(COALESCE(name,'')) = '' OR name IS NULL
        )
        """
    ).fetchone()["c"]

    print("=== B3 METRICS ===")
    print(f"REGARD_PRODUCTS={regard_count}")
    print(f"WITH_CHARACTERISTICS={len(char_rows)}")
    print(f"CHAR_FIELDS={sum(char_counts)}")
    print(f"AVG_CHAR_FIELDS={round(sum(char_counts)/len(char_counts), 2) if char_counts else 0}")
    print(f"MIN_CHAR_FIELDS={min(char_counts) if char_counts else 0}")
    print(f"MAX_CHAR_FIELDS={max(char_counts) if char_counts else 0}")
    print(f"WITH_DESCRIPTION={with_desc}")
    print(f"WITH_BRAND={with_brand}")
    print(f"WITH_MODEL={with_model}")
    print(f"WITH_VENDOR_CODE={with_vendor}")
    print(f"IMAGE_DB_ROWS={image_db}")
    print(f"LOCAL_IMAGE_FILES={local_files}")
    print(f"FAILED={failed}")
    print("DEFERRED=0")

    print("\n=== BY SOURCE ===")
    for row in conn.execute("SELECT source, COUNT(*) c FROM products GROUP BY source ORDER BY source"):
        print(f"{row['source']}: {row['c']}")

    print("\n=== DUPLICATES ===")
    dups = conn.execute(
        """
        SELECT source, source_id, COUNT(*) c
        FROM products GROUP BY source, source_id HAVING c > 1
        """
    ).fetchall()
    print(f"duplicate_groups={len(dups)}")

    print("\n=== SAMPLE CARDS (3) ===")
    samples = conn.execute(
        """
        SELECT p.*, pc.characteristics_json, c.path AS category_path_db
        FROM products p
        LEFT JOIN product_characteristics pc ON pc.product_id = p.id
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.source='regard'
        ORDER BY p.id
        LIMIT 3
        """
    ).fetchall()
    for sample in samples:
        chars = {}
        if sample["characteristics_json"]:
            try:
                chars = json.loads(sample["characteristics_json"])
            except json.JSONDecodeError:
                pass
        extra = {}
        if sample["extra_json"]:
            try:
                extra = json.loads(sample["extra_json"])
            except json.JSONDecodeError:
                pass
        img_count = conn.execute(
            "SELECT COUNT(*) c FROM product_images WHERE product_id=?",
            (sample["id"],),
        ).fetchone()["c"]
        local_count = 0
        sid = sample["source_id"]
        local_dir = STORAGE / sid
        if local_dir.is_dir():
            local_count = sum(1 for f in local_dir.iterdir() if f.is_file())
        desc_len = max(
            len(sample["description"] or ""),
            len(sample["description_detailed"] or ""),
            len(sample["description_full"] or ""),
        )
        first10 = list(chars.items())[:10]
        print(
            json.dumps(
                {
                    "source": sample["source"],
                    "source_id": sample["source_id"],
                    "url": sample["url"],
                    "name": sample["name"],
                    "sku": sample["sku"],
                    "brand": sample["brand"],
                    "model": extra.get("model"),
                    "vendor_code": extra.get("vendor_code"),
                    "category_path": sample["category_path_db"] or extra.get("category_path"),
                    "price": sample["price"],
                    "description_len": desc_len,
                    "characteristics_count": len(chars),
                    "first_10_characteristics": first10,
                    "image_db_count": img_count,
                    "local_image_files": local_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    conn.close()


if __name__ == "__main__":
    main()
