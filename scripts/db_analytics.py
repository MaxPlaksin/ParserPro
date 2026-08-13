#!/usr/bin/env python3
"""Quick analytics for products.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "products.db"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM products WHERE is_active = 1").fetchone()[0]

    print("=" * 60)
    print(f"products.db: {DB}")
    print(f"Всего товаров: {total}")
    print(f"Активных (is_active=1): {active}")
    print("=" * 60)

    print("\nПо источнику (source):")
    print(f"{'SOURCE':<12} {'COUNT':>8} {'WITH_PRICE':>11} {'WITH_SPECS':>11} {'AVG_SPECS':>10}")
    print("-" * 56)
    for row in conn.execute(
        """
        SELECT
            p.source,
            COUNT(*) AS cnt,
            SUM(CASE WHEN p.price IS NOT NULL AND p.price > 0 THEN 1 ELSE 0 END) AS with_price,
            SUM(CASE WHEN pc.characteristics_json IS NOT NULL AND LENGTH(pc.characteristics_json) > 10 THEN 1 ELSE 0 END) AS with_specs
        FROM products p
        LEFT JOIN product_characteristics pc ON pc.product_id = p.id
        GROUP BY p.source
        ORDER BY cnt DESC
        """
    ):
        src = row["source"]
        avg_specs = conn.execute(
            """
            SELECT AVG(json_array_length(json_extract(pc.characteristics_json, '$'))) 
            FROM products p
            JOIN product_characteristics pc ON pc.product_id = p.id
            WHERE p.source = ? AND pc.characteristics_json IS NOT NULL
            """,
            (src,),
        ).fetchone()[0]
        avg_s = f"{avg_specs:.1f}" if avg_specs else "-"
        print(f"{src:<12} {row['cnt']:>8} {row['with_price']:>11} {row['with_specs']:>11} {avg_s:>10}")

    print("\nCitilink — по category_path (из extra_json, top 15):")
    paths: dict[str, int] = {}
    import json

    for (extra,) in conn.execute(
        "SELECT extra_json FROM products WHERE source='citilink' AND extra_json IS NOT NULL"
    ):
        try:
            data = json.loads(extra)
            path = str(data.get("category_path") or "?")
            leaf = path.split(" > ")[-1] if path else "?"
            paths[leaf] = paths.get(leaf, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass
    for name, cnt in sorted(paths.items(), key=lambda x: -x[1])[:15]:
        print(f"  {name[:40]:<40} {cnt:>5}")

    images = conn.execute("SELECT COUNT(*) FROM product_images").fetchone()[0]
    chars = conn.execute("SELECT COUNT(*) FROM product_characteristics").fetchone()[0]
    cats = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    print(f"\nДополнительно: images={images}, characteristics_rows={chars}, categories_table={cats}")

    print("\nCitilink — по category_name (top 15):")
    try:
        for row in conn.execute(
            """
            SELECT category_name, COUNT(*) c
            FROM products WHERE source='citilink'
            GROUP BY category_name ORDER BY c DESC LIMIT 15
            """
        ):
            name = (row["category_name"] or "?")[:40]
            print(f"  {name:<40} {row['c']:>5}")
    except sqlite3.OperationalError:
        print("  (column category_name отсутствует — см. extra_json выше)")

    print("\nCitilink — processory checkpoint vs DB:")
    proc = conn.execute(
        "SELECT COUNT(*) FROM products WHERE source='citilink' AND url LIKE '%/product/processor-%'"
    ).fetchone()[0]
    print(f"  URL pattern processor-*: {proc}")

    print("\nПоследние upsert (по last_seen_at):")
    for row in conn.execute(
        """
        SELECT source, COUNT(*) c, MAX(last_seen_at) last_seen
        FROM products GROUP BY source ORDER BY last_seen DESC
        """
    ):
        print(f"  {row['source']:<12} {row['c']:>6}  last_seen={row['last_seen']}")

    conn.close()


if __name__ == "__main__":
    main()
