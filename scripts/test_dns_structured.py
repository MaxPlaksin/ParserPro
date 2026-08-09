#!/usr/bin/env python3
"""Verify structured DNS parsing on 2 products (mock or live)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common import CategoryProgress
from db.repository import get_repository
from dns_parser import DnsProductRef, parse_product_page
from normalizer.dns import normalize_dns_row

DB_PATH = PROJECT_ROOT / "data" / "products.db"


def mock_page_data() -> dict:
    return {
        "grouped": [
            ["Общие параметры", "Модель", "Xiaomi Redmi 15"],
            ["Экран", "Диагональ", '6.9"'],
            ["Память", "ОЗУ", "8 ГБ"],
        ],
        "marketingText": (
            "Погрузитесь в мир ярких и насыщенных цветов с потрясающим дисплеем смартфона Xiaomi REDMI 15."
        ),
        "rawFullText": (
            "Погрузитесь в мир ярких и насыщенных цветов...\n"
            "История цены\nот 12 259 ₽ до 16 999 ₽\n"
            "Отличная надежность\n99.82%\nПродаж без обращений в сервис\n"
            "Характеристики\nДисплей\n6.9\", 2340x1080\nКомплектация\nкабель, зарядка"
        ),
        "reliabilityPercent": 99.82,
        "reliabilityText": "Продаж без обращений в сервис",
        "equipmentText": "кабель USB Type-C, зарядное устройство",
        "dimensionsText": "Ширина 80.5 мм, Высота 169.5 мм, Вес 214 г",
        "displayText": '6.9", 2340x1080, IPS, 144 Гц',
        "priceHistoryText": "от 12 259 ₽ до 16 999 ₽",
        "priceHistoryMin": 12259,
        "priceHistoryMax": 16999,
    }


def build_mock_rows() -> list[dict]:
    cat = CategoryProgress(
        "17a8a01d16404e77",
        "Смартфоны и гаджеты",
        "Каталог > Смартфоны",
        "smartfony",
    )
    products = [
        ("f89a801b62bbd21a", "5634783", "6.9\" Смартфон Xiaomi Redmi 15 256 ГБ черный", 16999),
        ("514f4e3a8158d582", "5638384", "6.7\" Смартфон Samsung Galaxy S25 FE 512 ГБ черный", 60499),
    ]
    rows = []
    for pid, sku, name, price in products:
        ref = DnsProductRef(
            pid,
            f"https://www.dns-shop.ru/product/{pid}/slug/",
        )
        html = f"""
        <script type="application/ld+json">{{
            "@type":"Product","name":"{name}","sku":"{sku}",
            "description":"Краткое описание для {name}",
            "offers":{{"price":{price},"priceCurrency":"RUB","availability":"InStock"}},
            "aggregateRating":{{"ratingValue":4.8,"reviewCount":100}}
        }}</script>
        <div class="product-card-top__specs-item">
            <div class="product-card-top__specs-item-title">Дисплей:</div>
            <div class="product-card-top__specs-item-content">6.9"</div>
        </div>
        """
        row = parse_product_page(html, ref, cat, page_data=mock_page_data())
        row["local_images"] = f"images/{pid}/001.jpg"
        rows.append(row)
    return rows


async def run_live(limit: int) -> list[dict]:
    from dns_parser import DnsScraper, GlobalCheckpoint, scrape_once
    import argparse

    args = argparse.Namespace(
        output="output/dns",
        category="17a8a01d16404e77",
        limit=limit,
        max_categories=1,
        skip_images=True,
        headless=False,
        delay=5.0,
        category_delay=30.0,
    )
    global_cp_path = Path(args.output) / "checkpoint_global.json"
    if global_cp_path.exists():
        data = json.loads(global_cp_path.read_text(encoding="utf-8"))
        data["completed_categories"] = []
        global_cp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    cat_cp = Path(args.output) / "smartfony" / "checkpoint.json"
    if cat_cp.exists():
        cat_cp.unlink()
    await scrape_once(args)
    rows_path = Path(args.output) / "smartfony" / "checkpoint.rows.json"
    return json.loads(rows_path.read_text(encoding="utf-8"))[:limit]


def print_product(conn: sqlite3.Connection, source_id: str) -> None:
    row = conn.execute(
        """
        SELECT source, source_id, sku, name, price, old_price,
               description_short, description_detailed, description_full,
               raw_full_text, reliability_percent, reliability_text,
               display, dimensions, equipment, price_history_text
        FROM products WHERE source='dns' AND source_id=?
        """,
        (source_id,),
    ).fetchone()
    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    chars = conn.execute(
        """
        SELECT characteristics_json FROM product_characteristics pc
        JOIN products p ON p.id = pc.product_id
        WHERE p.source='dns' AND p.source_id=?
        """,
        (source_id,),
    ).fetchone()
    if chars:
        data = json.loads(chars["characteristics_json"])
        print("characteristics keys:", len(data), list(data.items())[:4])
    prices = conn.execute(
        """
        SELECT ph.price, ph.old_price, ph.recorded_at FROM price_history ph
        JOIN products p ON p.id = ph.product_id
        WHERE p.source='dns' AND p.source_id=?
        ORDER BY ph.id
        """,
        (source_id,),
    ).fetchall()
    print("price_history:", [dict(x) for x in prices])
    images = conn.execute(
        """
        SELECT path, kind FROM product_images pi
        JOIN products p ON p.id = pi.product_id
        WHERE p.source='dns' AND p.source_id=?
        ORDER BY pi.position
        LIMIT 3
        """,
        (source_id,),
    ).fetchall()
    print("images:", [dict(x) for x in images])
    print("-" * 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Parse 2 products via CloakBrowser")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--fresh-db", action="store_true", help="Delete products.db before test")
    args = parser.parse_args()

    if args.fresh_db and DB_PATH.exists():
        DB_PATH.unlink()

    if args.live:
        rows = asyncio.run(run_live(args.limit))
    else:
        rows = build_mock_rows()[: args.limit]

    cat_dir = PROJECT_ROOT / "output/dns/smartfony"
    repo = get_repository()
    for row in rows:
        record = normalize_dns_row(row, category_dir=cat_dir)
        repo.upsert_product(record)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    print("=== STRUCTURED DNS TEST ===")
    for row in rows:
        print_product(conn, row["product_id"])
    conn.close()


if __name__ == "__main__":
    main()
