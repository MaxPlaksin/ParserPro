#!/usr/bin/env python3
"""Backup products.db + storage/images/ (the valuable parse result)."""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB = PROJECT_ROOT / "data" / "products.db"
IMAGES = PROJECT_ROOT / "storage" / "images"
BACKUP_ROOT = PROJECT_ROOT / "backups"


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_ROOT / stamp
    dest.mkdir(parents=True, exist_ok=True)

    if DB.exists():
        shutil.copy2(DB, dest / "products.db")
        for suffix in ("-wal", "-shm"):
            extra = Path(str(DB) + suffix)
            if extra.exists():
                shutil.copy2(extra, dest / extra.name)
        print(f"DB -> {dest / 'products.db'}")
    else:
        print("WARN: no products.db", file=sys.stderr)

    if IMAGES.exists():
        shutil.copytree(IMAGES, dest / "images", dirs_exist_ok=True)
        count = sum(1 for _ in (dest / "images").rglob("*") if _.is_file())
        print(f"Images -> {dest / 'images'} ({count} files)")
    else:
        print("WARN: no storage/images", file=sys.stderr)

    print(f"Backup ready: {dest}")


if __name__ == "__main__":
    main()
