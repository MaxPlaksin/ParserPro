#!/usr/bin/env python3
"""C4 report: full Citilink characteristics + image audit."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
FULL_SPEC_THRESHOLD = 15
BEFORE_AVG = 7.1


def main() -> None:
    if not DB_PATH.exists():
        print("DB not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    citilink_count = conn.execute("SELECT COUNT(*) c FROM products WHERE source='citilink'").fetchone()["c"]
    char_rows = conn.execute(
        """
        SELECT p.source_id, LENGTH(pc.characteristics_json) len, pc.characteristics_json
        FROM products p
        JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source='citilink'
        """
    ).fetchall()

    char_counts: list[int] = []
    full_count = 0
    for row in char_rows:
        try:
            n = len(json.loads(row["characteristics_json"]))
        except json.JSONDecodeError:
            n = 0
        char_counts.append(n)
        if n >= FULL_SPEC_THRESHOLD:
            full_count += 1

    failed = conn.execute(
        """
        SELECT COUNT(*) c FROM products
        WHERE source='citilink' AND (TRIM(COALESCE(name,'')) = '' OR name IS NULL)
        """
    ).fetchone()["c"]

    print("=== C4 METRICS ===")
    print(f"CITILINK_PRODUCTS={citilink_count}")
    print(f"WITH_FULL_CHARACTERISTICS={full_count}")
    print(f"CHAR_FIELDS={sum(char_counts)}")
    avg = round(sum(char_counts) / len(char_counts), 2) if char_counts else 0
    print(f"AVG_CHAR_FIELDS={avg}")
    print(f"MIN_CHAR_FIELDS={min(char_counts) if char_counts else 0}")
    print(f"MAX_CHAR_FIELDS={max(char_counts) if char_counts else 0}")
    print(f"FAILED={failed}")
    print("DEFERRED=0")
    print(f"BEFORE_AVG_CHAR_FIELDS={BEFORE_AVG}")
    print(f"AFTER_AVG_CHAR_FIELDS={avg}")

    print("\n=== SAMPLE SPECS (3 products, first 20 fields) ===")
    samples = conn.execute(
        """
        SELECT p.source_id, p.name, pc.characteristics_json
        FROM products p
        JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source='citilink'
        ORDER BY p.id
        LIMIT 3
        """
    ).fetchall()
    for sample in samples:
        chars = json.loads(sample["characteristics_json"] or "{}")
        print(
            json.dumps(
                {
                    "source_id": sample["source_id"],
                    "name": (sample["name"] or "")[:100],
                    "characteristics_count": len(chars),
                    "first_20": list(chars.items())[:20],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    print("\n=== IMAGE AUDIT (3 products) ===")
    img_samples = conn.execute(
        """
        SELECT p.id, p.source_id
        FROM products p
        WHERE p.source='citilink'
        ORDER BY p.id
        LIMIT 3
        """
    ).fetchall()
    for sample in img_samples:
        imgs = conn.execute(
            """
            SELECT source_url, path, kind, position
            FROM product_images
            WHERE product_id=?
            ORDER BY position, id
            """,
            (sample["id"],),
        ).fetchall()
        urls = [r["source_url"] for r in imgs if r["source_url"]]
        print(
            json.dumps(
                {
                    "product_id": sample["source_id"],
                    "image_count": len(imgs),
                    "first_10_source_urls": urls[:10],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    conn.close()


if __name__ == "__main__":
    main()
