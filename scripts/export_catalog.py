#!/usr/bin/env python3
"""Выгрузка Петровича и Лемана ПРО в отдельные CSV, чтобы источники не смешивались.

Файлы:
  output/export/petrovich_groups.csv
  output/export/petrovich_products.csv
  output/export/lemana_groups.csv
  output/export/lemana_products.csv
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "products.db"
OUT = ROOT / "output" / "export"

GROUP_FIELDS = [
    "магазин",
    "тип",
    "название",
    "путь",
    "ссылка",
    "код_родителя",
    "код_раздела",
    "товаров_на_сайте",
]
PRODUCT_FIELDS = [
    "магазин",
    "номенклатура",
    "название",
    "бренд",
    "цена",
    "старая_цена",
    "валюта",
    "путь_категории",
    "категория",
    "ссылка",
    "описание",
    "сертификаты",
    "фото",
    "доп_ссылки",
]


def _links(extra: dict) -> tuple[str, str, str]:
    resources = extra.get("resource_links") or {}
    certs = extra.get("certificates") or resources.get("certificates") or []
    photos = resources.get("images") or []
    cert_urls: list[str] = []
    if isinstance(certs, list):
        for item in certs:
            if isinstance(item, str):
                cert_urls.append(item)
            elif isinstance(item, dict):
                if item.get("url"):
                    cert_urls.append(str(item["url"]))
                for file in item.get("files") or []:
                    if isinstance(file, dict) and file.get("url"):
                        cert_urls.append(str(file["url"]))
    photo_urls = [str(u) for u in photos] if isinstance(photos, list) else []
    other = []
    for key in ("fast_links", "documents"):
        value = resources.get(key) or extra.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    other.append(item)
                elif isinstance(item, dict) and item.get("url"):
                    other.append(str(item["url"]))
    page = resources.get("product_page") or extra.get("product_url")
    if page:
        other.insert(0, str(page))
    return " | ".join(cert_urls), " | ".join(photo_urls), " | ".join(other)


def export_source(conn: sqlite3.Connection, source: str) -> None:
    groups_path = OUT / f"{source}_groups.csv"
    products_path = OUT / f"{source}_products.csv"
    with groups_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GROUP_FIELDS)
        writer.writeheader()
        seen: set[str] = set()
        for row in conn.execute(
            """
            SELECT kind, name, path, url, parent_section_code, section_code, product_qty
            FROM catalog_sections
            WHERE source = ?
            ORDER BY depth, name
            """,
            (source,),
        ):
            url = (row["url"] or "").split("?")[0]
            key = f"{row['kind']}|{url}|{row['name']}"
            if key in seen:
                continue
            seen.add(key)
            writer.writerow(
                {
                    "магазин": source,
                    "тип": "группа" if row["kind"] == "group" else "подгруппа",
                    "название": row["name"],
                    "путь": row["path"],
                    "ссылка": url,
                    "код_родителя": row["parent_section_code"] or "",
                    "код_раздела": row["section_code"],
                    "товаров_на_сайте": row["product_qty"] if row["product_qty"] is not None else "",
                }
            )

    with products_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRODUCT_FIELDS)
        writer.writeheader()
        query = """
            SELECT p.source_id, p.name, p.brand, p.price, p.old_price, p.currency,
                   p.url, p.description, p.extra_json, c.name AS category_name, c.path AS category_path
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            WHERE p.source = ?
            ORDER BY c.path, p.name
        """
        for row in conn.execute(query, (source,)):
            extra = {}
            if row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"])
                except json.JSONDecodeError:
                    extra = {}
            certs, photos, links = _links(extra)
            writer.writerow(
                {
                    "магазин": source,
                    "номенклатура": row["source_id"],
                    "название": row["name"],
                    "бренд": row["brand"] or "",
                    "цена": row["price"] if row["price"] is not None else "",
                    "старая_цена": row["old_price"] if row["old_price"] is not None else "",
                    "валюта": row["currency"] or "RUB",
                    "путь_категории": row["category_path"] or "",
                    "категория": row["category_name"] or "",
                    "ссылка": row["url"] or "",
                    "описание": row["description"] or "",
                    "сертификаты": certs,
                    "фото": photos,
                    "доп_ссылки": links,
                }
            )
    print(groups_path)
    print(products_path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    for source in ("petrovich", "lemana"):
        export_source(conn, source)


if __name__ == "__main__":
    main()
