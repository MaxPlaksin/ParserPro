"""Lightweight SQLite migrations for products.db."""

from __future__ import annotations

import sqlite3

PRODUCT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("price_min_observed", "REAL"),
    ("price_max_observed", "REAL"),
    ("description_full", "TEXT"),
    ("raw_full_text", "TEXT"),
    ("reliability_percent", "REAL"),
    ("reliability_text", "TEXT"),
    ("display", "TEXT"),
    ("dimensions", "TEXT"),
    ("equipment", "TEXT"),
    ("price_history_text", "TEXT"),
    ("is_active", "INTEGER DEFAULT 1"),
)


def migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    for name, col_type in PRODUCT_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE products ADD COLUMN {name} {col_type}")
    if "is_active" not in existing:
        conn.execute("UPDATE products SET is_active = 1 WHERE is_active IS NULL")
