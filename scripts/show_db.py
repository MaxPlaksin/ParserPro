#!/usr/bin/env python3
"""Print SQLite storage summary for verification."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def print_counts(conn: sqlite3.Connection) -> None:
    tables = ["categories", "products", "product_characteristics", "product_images", "price_history"]
    print("=== TABLE COUNTS ===")
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        print(f"{table}: {count}")


def print_products(conn: sqlite3.Connection, limit: int = 4) -> None:
    print("\n=== PRODUCTS ===")
    rows = conn.execute(
        """
        SELECT p.id, p.source, p.source_id, p.name, p.price, p.old_price,
               p.created_at, p.updated_at, p.last_seen_at,
               c.name AS category_name, c.path AS category_path
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        ORDER BY p.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        print(dict(row))


def print_characteristics(conn: sqlite3.Connection, limit: int = 1) -> None:
    print("\n=== CHARACTERISTICS SAMPLE ===")
    row = conn.execute(
        """
        SELECT p.source, p.source_id, pc.characteristics_json
        FROM product_characteristics pc
        JOIN products p ON p.id = pc.product_id
        ORDER BY LENGTH(pc.characteristics_json) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchone()
    if row:
        payload = json.loads(row["characteristics_json"])
        sample = dict(list(payload.items())[:8])
        print({"source": row["source"], "source_id": row["source_id"], "sample": sample, "total": len(payload)})


def print_images(conn: sqlite3.Connection, limit: int = 5) -> None:
    print("\n=== IMAGES SAMPLE ===")
    rows = conn.execute(
        """
        SELECT p.source, p.source_id, pi.path, pi.kind, pi.position
        FROM product_images pi
        JOIN products p ON p.id = pi.product_id
        ORDER BY pi.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        print(dict(row))


def print_price_history(conn: sqlite3.Connection) -> None:
    print("\n=== PRICE HISTORY ===")
    rows = conn.execute(
        """
        SELECT p.source, p.source_id, ph.price, ph.old_price, ph.recorded_at
        FROM price_history ph
        JOIN products p ON p.id = ph.product_id
        ORDER BY ph.id
        """
    ).fetchall()
    for row in rows:
        print(dict(row))


def print_duplicates(conn: sqlite3.Connection) -> None:
    print("\n=== DUPLICATE CHECK ===")
    dup_products = conn.execute(
        """
        SELECT source, source_id, COUNT(*) AS c
        FROM products
        GROUP BY source, source_id
        HAVING c > 1
        """
    ).fetchall()
    dup_images = conn.execute(
        """
        SELECT product_id, path, COUNT(*) AS c
        FROM product_images
        GROUP BY product_id, path
        HAVING c > 1
        """
    ).fetchall()
    print(f"duplicate products: {len(dup_products)}")
    print(f"duplicate images: {len(dup_images)}")


def print_structured_fields(conn: sqlite3.Connection, limit: int = 2) -> None:
    print("\n=== STRUCTURED FIELDS ===")
    rows = conn.execute(
        """
        SELECT source, source_id, sku, name,
               LENGTH(description_short) AS desc_short_len,
               LENGTH(description_detailed) AS desc_detailed_len,
               LENGTH(description_full) AS desc_full_len,
               LENGTH(raw_full_text) AS raw_len,
               reliability_percent, reliability_text,
               display, dimensions, equipment, price_history_text,
               price, old_price, price_min_observed, price_max_observed, is_active,
               (SELECT LENGTH(pc.characteristics_json) FROM product_characteristics pc
                JOIN products p2 ON p2.id = pc.product_id
                WHERE p2.source = products.source AND p2.source_id = products.source_id) AS chars_json_len
        FROM products
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        print(dict(row))


def main() -> None:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = connect()
    try:
        print_counts(conn)
        print_products(conn)
        print_structured_fields(conn)
        print_characteristics(conn)
        print_images(conn)
        print_price_history(conn)
        print_duplicates(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
