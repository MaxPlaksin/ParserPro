#!/usr/bin/env python3
"""Import existing parser checkpoint rows into SQLite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.hooks import persist_dns_row, persist_mvideo_row


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def seed_dns() -> int:
    rows_path = PROJECT_ROOT / "output/dns/smartfony/checkpoint.rows.json"
    category_dir = PROJECT_ROOT / "output/dns/smartfony"
    count = 0
    for row in load_rows(rows_path):
        persist_dns_row(row, category_dir)
        count += 1
    return count


def seed_mvideo() -> int:
    rows_path = PROJECT_ROOT / "output/mvideo/category-118/checkpoint.rows.json"
    category_dir = PROJECT_ROOT / "output/mvideo/category-118"
    count = 0
    for row in load_rows(rows_path):
        persist_mvideo_row(row, category_dir)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed data/products.db from checkpoint rows")
    parser.add_argument("--dns-only", action="store_true")
    parser.add_argument("--mvideo-only", action="store_true")
    args = parser.parse_args()

    total = 0
    if not args.mvideo_only:
        total += seed_dns()
    if not args.dns_only:
        total += seed_mvideo()
    print(f"Seeded {total} products into data/products.db")


if __name__ == "__main__":
    main()
