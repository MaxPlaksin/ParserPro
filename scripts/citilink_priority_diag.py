#!/usr/bin/env python3
"""Diagnose Citilink priority run vs manifest and DB."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PROJECT_ROOT / "output" / "citilink" / "priority_manifest.json"
OUTPUT = PROJECT_ROOT / "output" / "citilink"
DB = PROJECT_ROOT / "data" / "products.db"
GLOBAL_CP = OUTPUT / "checkpoint_global.json"


def find_checkpoint(output: Path, *, category_id: str, slug: str) -> dict | None:
    for p in output.rglob("checkpoint.json"):
        try:
            cp = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        c = cp.get("category") or {}
        if str(c.get("category_id", "")) == str(category_id) or c.get("category_slug") == slug:
            cp["_path"] = str(p)
            return cp
    return None


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    completed_global: set[str] = set()
    if GLOBAL_CP.exists():
        completed_global = set(json.loads(GLOBAL_CP.read_text(encoding="utf-8")).get("completed_categories", []))

    conn = sqlite3.connect(DB)
    total_db = conn.execute("SELECT COUNT(*) FROM products WHERE source='citilink'").fetchone()[0]
    print(f"Citilink DB total: {total_db}")
    print(
        f"{'SLUG':<20} {'EXP':>6} {'LISTED':>7} {'PEND':>6} {'DONE':>6} "
        f"{'FAIL':>5} {'DB*':>5} {'PAGE':>5} {'L_OK':>5} {'G_OK':>5}"
    )
    print("-" * 88)

    for cat in manifest["categories"]:
        slug = cat["slug"]
        cid = cat["category_id"]
        expected = cat["product_count"]

        cp = find_checkpoint(OUTPUT, category_id=cid, slug=slug)
        pending = done_cp = failed = listed = pages = 0
        list_complete = False
        completed_ids: list[str] = []
        if cp:
            c = cp.get("category") or {}
            pending = len(cp.get("pending") or [])
            completed_ids = [str(x) for x in cp.get("completed_ids") or []]
            done_cp = len(completed_ids)
            listed = done_cp + pending
            pages = int(c.get("list_page") or 1)
            list_complete = bool(c.get("list_complete"))
            rows_path = Path(str(cp.get("_path", ""))).with_suffix(".rows.json")
            if rows_path.exists():
                rows = json.loads(rows_path.read_text(encoding="utf-8"))
                failed = sum(1 for r in rows if r.get("details_error"))

        db_count = 0
        if completed_ids:
            placeholders = ",".join("?" for _ in completed_ids)
            db_count = conn.execute(
                f"SELECT COUNT(*) FROM products WHERE source='citilink' AND source_id IN ({placeholders})",
                completed_ids,
            ).fetchone()[0]

        cat_key = f"{cid}|{slug}"
        global_ok = cat_key in completed_global
        print(
            f"{slug:<20} {expected:>6} {listed:>7} {pending:>6} {done_cp:>6} "
            f"{failed:>5} {db_count:>5} {pages:>5} "
            f"{'Y' if list_complete else 'N':>5} {'Y' if global_ok else 'N':>5}"
        )

    conn.close()
    print("\n* DB* = upserts matched by checkpoint completed_ids (not manifest category_id)")


if __name__ == "__main__":
    main()
