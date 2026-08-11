#!/usr/bin/env python3
"""Build analytics summary from SQLite + checkpoints + output folders."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
REPORT_PATH = PROJECT_ROOT / "output" / "analytics_summary.json"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def local_file_exists(db_path: str) -> bool:
    if not db_path:
        return False
    return (PROJECT_ROOT / db_path.replace("\\", "/")).is_file()


def load_checkpoint(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def count_output_categories(output_root: Path) -> dict[str, int]:
    if not output_root.is_dir():
        return {"xlsx_categories": 0, "image_files": 0, "product_image_dirs": 0}
    xlsx = list(output_root.rglob("products.xlsx"))
    image_files = [p for p in output_root.rglob("images/*/*") if p.is_file()]
    product_dirs = {p.parent for p in image_files}
    return {
        "xlsx_categories": len(xlsx),
        "image_files": len(image_files),
        "product_image_dirs": len(product_dirs),
    }


def main() -> int:
    if not DB_PATH.is_file():
        print(f"Database not found: {DB_PATH}")
        return 1

    conn = connect()
    report: dict = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "database": str(DB_PATH),
    }

    tables = ["categories", "products", "product_images", "product_characteristics", "price_history"]
    report["table_counts"] = {
        t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"] for t in tables
    }

    report["by_source"] = {}
    for source in ("dns", "mvideo"):
        src_report: dict = {}
        src_report["categories_in_db"] = conn.execute(
            "SELECT COUNT(*) AS c FROM categories WHERE source = ?", (source,)
        ).fetchone()["c"]
        src_report["products"] = conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE source = ?", (source,)
        ).fetchone()["c"]
        src_report["products_with_price"] = conn.execute(
            "SELECT COUNT(*) AS c FROM products WHERE source = ? AND price IS NOT NULL",
            (source,),
        ).fetchone()["c"]
        src_report["products_with_description"] = conn.execute(
            """
            SELECT COUNT(*) AS c FROM products
            WHERE source = ? AND description IS NOT NULL AND TRIM(description) != ''
            """,
            (source,),
        ).fetchone()["c"]
        src_report["products_with_sku"] = conn.execute(
            """
            SELECT COUNT(*) AS c FROM products
            WHERE source = ? AND sku IS NOT NULL AND TRIM(sku) != ''
            """,
            (source,),
        ).fetchone()["c"]
        src_report["products_with_characteristics"] = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM products p
            JOIN product_characteristics pc ON pc.product_id = p.id
            WHERE p.source = ?
              AND pc.characteristics_json IS NOT NULL
              AND TRIM(pc.characteristics_json) NOT IN ('', '{}')
            """,
            (source,),
        ).fetchone()["c"]
        src_report["products_with_image_records"] = conn.execute(
            """
            SELECT COUNT(DISTINCT p.id) AS c
            FROM products p
            JOIN product_images pi ON pi.product_id = p.id
            WHERE p.source = ?
            """,
            (source,),
        ).fetchone()["c"]
        src_report["image_records_in_db"] = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM product_images pi
            JOIN products p ON p.id = pi.product_id
            WHERE p.source = ?
            """,
            (source,),
        ).fetchone()["c"]

        products_with_local: set[int] = set()
        local_files = 0
        rows = conn.execute(
            """
            SELECT p.id, pi.path
            FROM products p
            JOIN product_images pi ON pi.product_id = p.id
            WHERE p.source = ?
            """,
            (source,),
        ).fetchall()
        seen_paths: set[str] = set()
        for row in rows:
            path = (row["path"] or "").replace("\\", "/")
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if local_file_exists(path):
                local_files += 1
                products_with_local.add(row["id"])
        src_report["local_image_files"] = local_files
        src_report["products_with_local_images"] = len(products_with_local)

        top_cats = conn.execute(
            """
            SELECT c.path, COUNT(*) AS c
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.source = ?
            GROUP BY c.path
            ORDER BY c DESC
            LIMIT 10
            """,
            (source,),
        ).fetchall()
        src_report["top_categories_by_products"] = [
            {"path": r["path"], "products": r["c"]} for r in top_cats
        ]

        cp = load_checkpoint(PROJECT_ROOT / "output" / source / "checkpoint_global.json")
        if cp:
            blocked = cp.get("blocked_categories", {})
            src_report["checkpoint"] = {
                "completed_categories": len(cp.get("completed_categories", [])),
                "needs_recheck_categories": len(cp.get("needs_recheck_categories", [])),
                "blocked_403": sum(
                    1
                    for v in blocked.values()
                    if isinstance(v, dict) and v.get("reason") == "403"
                ),
            }
        else:
            src_report["checkpoint"] = None

        src_report["output"] = count_output_categories(PROJECT_ROOT / "output" / source)
        report["by_source"][source] = src_report

    conn.close()

    totals = {
        "products": sum(v["products"] for v in report["by_source"].values()),
        "products_with_local_images": sum(
            v["products_with_local_images"] for v in report["by_source"].values()
        ),
        "categories_in_db": sum(v["categories_in_db"] for v in report["by_source"].values()),
        "completed_categories_checkpoint": sum(
            (v.get("checkpoint") or {}).get("completed_categories", 0)
            for v in report["by_source"].values()
        ),
        "xlsx_categories_on_disk": sum(v["output"]["xlsx_categories"] for v in report["by_source"].values()),
        "local_image_files": sum(v["local_image_files"] for v in report["by_source"].values()),
    }
    report["totals"] = totals

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
