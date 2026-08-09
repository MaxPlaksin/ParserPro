#!/usr/bin/env python3
"""Pre-full-run safety checks (idempotency, paths, checkpoint)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"


def check_duplicates(conn: sqlite3.Connection) -> list[str]:
    issues = []
    dup = conn.execute(
        """
        SELECT source, source_id, COUNT(*) c FROM products
        GROUP BY source, source_id HAVING c > 1
        """
    ).fetchall()
    if dup:
        issues.append(f"duplicate products: {len(dup)}")
    dup_img = conn.execute(
        """
        SELECT product_id, path, COUNT(*) c FROM product_images
        GROUP BY product_id, path HAVING c > 1
        """
    ).fetchall()
    if dup_img:
        issues.append(f"duplicate images: {len(dup_img)}")
    bad_names = conn.execute(
        """
        SELECT path FROM product_images
        WHERE path LIKE '%\\_%\\_%' OR path GLOB '*_[0-9].jpg'
        """
    ).fetchall()
    if bad_names:
        issues.append(f"suspicious image suffix duplicates: {len(bad_names)}")
    return issues


def check_paths(conn: sqlite3.Connection) -> list[str]:
    issues = []
    rows = conn.execute("SELECT path FROM product_images").fetchall()
    for row in rows:
        p = row["path"]
        if p.startswith("/"):
            issues.append(f"absolute path in DB: {p}")
        elif not p.startswith("storage/images/"):
            issues.append(f"non-canonical path: {p}")
    return issues[:10]


def check_checkpoints() -> list[str]:
    issues = []
    for cp in PROJECT_ROOT.rglob("checkpoint.rows.json"):
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            meta = cp.parent / "checkpoint.json"
            if meta.exists():
                payload = json.loads(meta.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("rows_count", 0) > 0 and not data:
                    issues.append(f"checkpoint rows empty but rows_count>0: {cp}")
        except json.JSONDecodeError as exc:
            issues.append(f"invalid checkpoint JSON {cp}: {exc}")
    return issues


def main() -> None:
    if not DB_PATH.exists():
        print("FAIL: no products.db")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    issues: list[str] = []
    issues.extend(check_duplicates(conn))
    issues.extend(check_paths(conn))
    conn.close()
    issues.extend(check_checkpoints())

    print("=== SAFETY CHECK ===")
    if issues:
        print("FAIL:")
        for item in issues:
            print(f"  - {item}")
        sys.exit(1)
    print("PASS: idempotency keys OK, image paths canonical, checkpoints readable")


if __name__ == "__main__":
    main()
