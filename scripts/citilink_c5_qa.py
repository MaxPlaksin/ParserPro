#!/usr/bin/env python3
"""C5.3 multi-category QA metrics for Citilink."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
PARSER = PROJECT_ROOT / "citilink_parser.py"
FULL_SPEC_THRESHOLD = 15

# slug, category_id, name, path
QA_CATEGORIES = [
    ("noutbuki", "3", "Ноутбуки", "Ноутбуки, ПК и мониторы > Ноутбуки"),
    ("smartfony", "214", "Смартфоны", "Смартфоны, планшеты и наушники > Смартфоны"),
    ("processory", "26", "Процессоры", "Комплектующие для ПК > Процессоры"),
    ("holodilniki", "323", "Холодильники", "Техника для кухни > Крупная бытовая техника > Холодильники"),
    ("videokarty", "29", "Видеокарты", "Комплектующие для ПК > Видеокарты"),
]


def category_metrics(conn: sqlite3.Connection, slug: str) -> dict:
    rows = conn.execute(
        """
        SELECT p.source_id, p.name, p.brand, p.description, p.description_detailed,
               p.description_full, pc.characteristics_json, p.extra_json,
               (SELECT COUNT(*) FROM product_images pi WHERE pi.product_id = p.id) AS image_count
        FROM products p
        LEFT JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = 'citilink' AND p.url LIKE ?
        ORDER BY p.id DESC
        LIMIT 10
        """,
        (f"%/catalog/{slug}%", f"%/product/%"),
    ).fetchall()
    # URLs don't contain catalog slug - match by recent products isn't ideal
    # Use extra category_path or scrape batch - for QA we'll count last N added per run
    return {}


def metrics_for_ids(conn: sqlite3.Connection, source_ids: list[str]) -> dict:
    if not source_ids:
        return {"PRODUCTS": 0}
    placeholders = ",".join("?" * len(source_ids))
    rows = conn.execute(
        f"""
        SELECT p.source_id, p.name, p.brand, p.description, p.description_detailed,
               p.description_full, pc.characteristics_json, p.extra_json,
               (SELECT COUNT(*) FROM product_images pi WHERE pi.product_id = p.id) AS image_count
        FROM products p
        LEFT JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = 'citilink' AND p.source_id IN ({placeholders})
        """,
        source_ids,
    ).fetchall()

    char_counts = []
    with_full = 0
    with_desc = 0
    with_brand = 0
    with_model = 0
    images = 0
    failed = 0
    for row in rows:
        chars = {}
        if row["characteristics_json"]:
            try:
                chars = json.loads(row["characteristics_json"])
            except json.JSONDecodeError:
                pass
        n = len(chars)
        char_counts.append(n)
        if n >= FULL_SPEC_THRESHOLD:
            with_full += 1
        desc_len = max(len(row["description"] or ""), len(row["description_detailed"] or ""), len(row["description_full"] or ""))
        if desc_len > 0:
            with_desc += 1
        if (row["brand"] or "").strip():
            with_brand += 1
        extra = {}
        if row["extra_json"]:
            try:
                extra = json.loads(row["extra_json"])
            except json.JSONDecodeError:
                pass
        if extra.get("model"):
            with_model += 1
        if not (row["name"] or "").strip():
            failed += 1
        images += int(row["image_count"] or 0)

    return {
        "PRODUCTS": len(rows),
        "WITH_FULL_CHARACTERISTICS": with_full,
        "AVG_CHAR_FIELDS": round(sum(char_counts) / len(char_counts), 2) if char_counts else 0,
        "MIN_CHAR_FIELDS": min(char_counts) if char_counts else 0,
        "MAX_CHAR_FIELDS": max(char_counts) if char_counts else 0,
        "WITH_DESCRIPTION": with_desc,
        "WITH_BRAND": with_brand,
        "WITH_MODEL": with_model,
        "IMAGE_COUNT": images,
        "FAILED": failed,
        "DEFERRED": 0,
        "source_ids": [r["source_id"] for r in rows],
    }


def run_category(slug: str, cat_id: str, name: str, path: str, limit: int = 5) -> list[str]:
    before = set()
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        before = {r[0] for r in conn.execute("SELECT source_id FROM products WHERE source='citilink'")}
        conn.close()

    cmd = [
        str(PYTHON),
        str(PARSER),
        "-o",
        str(PROJECT_ROOT / "output" / "citilink"),
        "--category-slug",
        slug,
        "--category",
        cat_id,
        "--category-name",
        name,
        "--category-path",
        path,
        "--limit",
        str(limit),
        "--skip-images",
        "--delay",
        "6",
    ]
    print(f"\n>>> Running QA: {slug} limit={limit}")
    subprocess.run(cmd, check=False, cwd=str(PROJECT_ROOT))

    after_ids: list[str] = []
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        after = {r[0] for r in conn.execute("SELECT source_id FROM products WHERE source='citilink'")}
        new_ids = sorted(after - before, key=int)
        # if no new, take last `limit` from all citilink (upsert case)
        if new_ids:
            after_ids = new_ids[-limit:]
        else:
            rows = conn.execute(
                "SELECT source_id FROM products WHERE source='citilink' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            after_ids = [r[0] for r in rows]
        conn.close()
    return after_ids


def main() -> None:
    if not PYTHON.exists():
        print("venv python not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH) if DB_PATH.exists() else None
    print("=== C5.3 MULTI-CATEGORY QA ===")
    for slug, cat_id, name, path in QA_CATEGORIES:
        ids = run_category(slug, cat_id, name, path, limit=5)
        if conn:
            m = metrics_for_ids(conn, ids)
        else:
            m = {"PRODUCTS": 0}
        print(f"\n--- {slug} ({name}) ---")
        for key in (
            "PRODUCTS",
            "WITH_FULL_CHARACTERISTICS",
            "AVG_CHAR_FIELDS",
            "MIN_CHAR_FIELDS",
            "MAX_CHAR_FIELDS",
            "WITH_DESCRIPTION",
            "WITH_BRAND",
            "WITH_MODEL",
            "IMAGE_COUNT",
            "FAILED",
            "DEFERRED",
        ):
            print(f"{key}={m.get(key, 0)}")
        if m.get("source_ids"):
            print(f"source_ids={m['source_ids']}")
    if conn:
        conn.close()


if __name__ == "__main__":
    main()
