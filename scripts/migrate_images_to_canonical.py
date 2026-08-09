#!/usr/bin/env python3
"""Move legacy image files to storage/images/{source}/{id}/ and fix DB paths."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.repository import get_repository
from storage_paths import PROJECT_ROOT as ROOT, migrate_legacy_file, normalize_image_db_path

DB_PATH = PROJECT_ROOT / "data" / "products.db"


def migrate_db_paths(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT pi.id, pi.path, p.source, p.source_id
        FROM product_images pi
        JOIN products p ON p.id = pi.product_id
        """
    ).fetchall()
    updated = 0
    for row in rows:
        old_path = row["path"]
        canonical = normalize_image_db_path(
            old_path, default_source=row["source"], default_source_id=row["source_id"]
        )
        migrated = migrate_legacy_file(row["source"], row["source_id"], old_path)
        final = migrated or canonical
        if final and final != old_path and final.startswith("storage/images/"):
            conn.execute("UPDATE product_images SET path = ? WHERE id = ?", (final, row["id"]))
            updated += 1
    conn.commit()
    return updated


def scan_output_and_storage() -> int:
    copied = 0
    for pattern in (
        ROOT / "output",
        ROOT / "storage" / "dns",
        ROOT / "storage" / "mvideo",
        ROOT / "storage" / "images",
    ):
        if not pattern.exists():
            continue
        for file in pattern.rglob("*"):
            if not file.is_file() or file.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                continue
            rel = file.relative_to(ROOT).as_posix()
            if rel.startswith("storage/images/"):
                continue
            source = "mvideo" if "mvideo" in rel else "dns"
            parts = rel.split("/")
            source_id = ""
            if "images" in parts:
                source_id = parts[parts.index("images") + 1]
            elif len(parts) >= 3:
                source_id = parts[-2]
            if not source_id:
                continue
            result = migrate_legacy_file(source, source_id, rel)
            if result:
                copied += 1
    return copied


def main() -> None:
    get_repository()  # run schema migrate
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    files = scan_output_and_storage()
    paths = migrate_db_paths(conn)
    conn.close()
    print(f"Migrated files: {files}")
    print(f"Updated DB paths: {paths}")


if __name__ == "__main__":
    main()
