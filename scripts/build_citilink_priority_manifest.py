#!/usr/bin/env python3
"""Build Citilink priority manifest from catalog_manifest.json."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from common import atomic_write_json

DEFAULT_CATALOG = PROJECT_ROOT / "output" / "citilink" / "catalog_manifest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "citilink" / "priority_manifest.json"

# priority_key -> exact leaf category name(s) in manifest (first exact match wins)
PRIORITY_RULES: list[tuple[str, str, list[str]]] = [
    ("1", "Ноутбуки", ["Ноутбуки"]),
    ("2", "Смартфоны", ["Смартфоны"]),
    ("3", "Мониторы", ["Мониторы"]),
    ("4", "Материнские платы", ["Материнские платы"]),
    ("5", "Видеокарты", ["Видеокарты"]),
    ("6", "Процессоры", ["Процессоры"]),
    ("7", "Блоки питания", ["Блоки питания"]),
    ("8", "SSD / твердотельные накопители", ["SSD накопители"]),
    ("9", "Оперативная память", ["Оперативная память"]),
    ("10", "Роутеры / Wi-Fi оборудование", ["Роутеры", "Wi-Fi оборудование", "Беспроводное оборудование"]),
    ("11", "ИБП", ["ИБП", "Источники бесперебойного питания"]),
    ("12", "Картриджи / расходные материалы", ["Картриджи"]),
    ("13", "Проекторы", ["Проекторы"]),
    ("14", "USB-флешки", ["USB-флешки", "USB флешки", "USB Flash"]),
    ("15", "HDD / накопители", ["Жесткие диски"]),
]


def load_leaves(catalog_path: Path) -> list[dict]:
    manifest = json.loads(catalog_path.read_text(encoding="utf-8"))
    return [c for c in manifest.get("categories", []) if c.get("is_leaf")]


def find_priority_matches(leaves: list[dict]) -> tuple[list[dict], list[dict]]:
    by_name = {str(c.get("name", "")).strip(): c for c in leaves}
    matched: list[dict] = []
    missing: list[dict] = []

    for priority, label, names in PRIORITY_RULES:
        cat = None
        matched_name = ""
        for name in names:
            if name in by_name:
                cat = by_name[name]
                matched_name = name
                break
        if cat:
            matched.append(
                {
                    "priority": priority,
                    "priority_label": label,
                    "matched_name": matched_name,
                    "category_id": cat.get("category_id", ""),
                    "slug": cat.get("slug", ""),
                    "name": cat.get("name", ""),
                    "category_path": cat.get("category_path", ""),
                    "category_url": cat.get("category_url", ""),
                    "product_count": cat.get("product_count"),
                }
            )
        else:
            missing.append({"priority": priority, "priority_label": label, "candidate_names": names})
    return matched, missing


def build_manifest(catalog_path: Path, output_path: Path) -> dict:
    leaves = load_leaves(catalog_path)
    matched, missing = find_priority_matches(leaves)
    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_catalog": str(catalog_path),
        "description": "Citilink priority leaf categories for sequential full scrape",
        "total_priorities": len(PRIORITY_RULES),
        "matched_count": len(matched),
        "missing_priorities": missing,
        "categories": [
            {
                "priority": int(item["priority"]),
                "category_id": item["category_id"],
                "slug": item["slug"],
                "name": item["name"],
                "category_path": item["category_path"],
                "category_url": item["category_url"],
                "product_count": item["product_count"],
            }
            for item in matched
        ],
    }
    atomic_write_json(output_path, manifest)

    csv_path = output_path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "priority",
                "category_id",
                "slug",
                "name",
                "category_path",
                "category_url",
                "product_count",
            ],
        )
        writer.writeheader()
        for row in manifest["categories"]:
            writer.writerow(row)
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description="Build Citilink priority manifest")
    p.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    p.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--print-only", action="store_true", help="Print matches without writing files")
    args = p.parse_args()

    catalog_path = Path(args.catalog)
    output_path = Path(args.output)
    leaves = load_leaves(catalog_path)
    matched, missing = find_priority_matches(leaves)

    print("=== PRIORITY MATCHES ===")
    for item in matched:
        print(
            f"{item['priority']:>2} | {item['category_id']:<8} | {item['name']:<30} | "
            f"{item['slug']:<22} | count={item.get('product_count')}"
        )
        print(f"    path: {item['category_path']}")

    if missing:
        print("\n=== NOT FOUND IN MANIFEST ===")
        for item in missing:
            print(f"{item['priority']:>2} | {item['priority_label']} | tried: {', '.join(item['candidate_names'])}")

    if not args.print_only:
        manifest = build_manifest(catalog_path, output_path)
        print(f"\nWrote {output_path} ({manifest['matched_count']}/{manifest['total_priorities']} categories)")


if __name__ == "__main__":
    main()
