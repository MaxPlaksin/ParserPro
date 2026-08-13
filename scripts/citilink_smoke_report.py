#!/usr/bin/env python3
"""Print Citilink category smoke / priority metrics."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = PROJECT_ROOT / "output" / "citilink" / "priority_manifest.json"
OUTPUT = PROJECT_ROOT / "output" / "citilink"
DB = PROJECT_ROOT / "data" / "products.db"
GLOBAL_CP = OUTPUT / "checkpoint_global.json"


def find_checkpoint(output: Path, *, category_id: str, slug: str) -> Path | None:
    for p in output.rglob("checkpoint.json"):
        try:
            cp = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        c = cp.get("category") or {}
        if str(c.get("category_id", "")) == str(category_id) or c.get("category_slug") == slug:
            return p
    return None


def db_count_for_category(conn: sqlite3.Connection, slug: str, completed_ids: list[str]) -> int:
    if completed_ids:
        placeholders = ",".join("?" for _ in completed_ids)
        row = conn.execute(
            f"SELECT COUNT(*) FROM products WHERE source='citilink' AND source_id IN ({placeholders})",
            completed_ids,
        ).fetchone()
        return int(row[0]) if row else 0
    row = conn.execute(
        "SELECT COUNT(*) FROM products WHERE source='citilink' AND url LIKE ?",
        (f"%/catalog/{slug}/%",),
    ).fetchone()
    return int(row[0]) if row else 0


def report_category(
    *,
    slug: str,
    category_id: str,
    expected: int | None,
    output: Path,
    conn: sqlite3.Connection,
    completed_global: set[str],
) -> None:
    cp_path = find_checkpoint(output, category_id=category_id, slug=slug)
    pending = deferred = failed = listed = detail_success = pages = 0
    list_complete = category_completed = False
    completed_ids: list[str] = []

    if cp_path:
        cp = json.loads(cp_path.read_text(encoding="utf-8"))
        c = cp.get("category") or {}
        pending = len(cp.get("pending") or [])
        completed_ids = [str(x) for x in cp.get("completed_ids") or []]
        detail_success = len(completed_ids)
        listed = detail_success + pending
        pages = int(c.get("list_page") or 1)
        list_complete = bool(c.get("list_complete"))
        rows_path = cp_path.with_suffix(".rows.json")
        if rows_path.exists():
            rows = json.loads(rows_path.read_text(encoding="utf-8"))
            failed = sum(1 for r in rows if r.get("details_error"))

    cat_key = f"{category_id}|{slug}"
    if expected and list_complete and not pending and listed >= expected:
        category_completed = True
    elif cat_key in completed_global:
        category_completed = True

    db_count = db_count_for_category(conn, slug, completed_ids)

    print(f"SLUG={slug}")
    print(f"EXPECTED={expected or '?'}")
    print(f"LISTED={listed}")
    print(f"DETAIL_SUCCESS={detail_success}")
    print(f"DEFERRED={pending}")
    print(f"FAILED={failed}")
    print(f"DB_COUNT={db_count}")
    print(f"PAGES={pages}")
    print(f"CATEGORY_COMPLETED={'Y' if category_completed else 'N'}")


def main() -> None:
    p = argparse.ArgumentParser(description="Citilink category metrics")
    p.add_argument("--slug", required=True)
    p.add_argument("--category-id", default="")
    p.add_argument("--expected", type=int, default=0)
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("-o", "--output", default=str(OUTPUT))
    args = p.parse_args()

    expected = args.expected or None
    category_id = args.category_id
    manifest_path = Path(args.manifest)
    if manifest_path.exists() and not expected:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for cat in manifest.get("categories") or []:
            if cat.get("slug") == args.slug:
                expected = int(cat.get("product_count") or 0) or None
                category_id = category_id or str(cat.get("category_id") or "")
                break

    completed_global: set[str] = set()
    global_cp = Path(args.output) / "checkpoint_global.json"
    if global_cp.exists():
        completed_global = set(json.loads(global_cp.read_text(encoding="utf-8")).get("completed_categories", []))

    conn = sqlite3.connect(DB)
    report_category(
        slug=args.slug,
        category_id=category_id,
        expected=expected,
        output=Path(args.output),
        conn=conn,
        completed_global=completed_global,
    )
    conn.close()


if __name__ == "__main__":
    main()
