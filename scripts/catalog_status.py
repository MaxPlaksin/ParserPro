#!/usr/bin/env python3
import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parent.parent / "data" / "products.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

print("== products ==")
for row in conn.execute(
    """
    SELECT source,
           COUNT(*) AS rows,
           COUNT(DISTINCT source_id) AS unique_ids,
           SUM(CASE WHEN price IS NULL THEN 1 ELSE 0 END) AS no_price,
           SUM(CASE WHEN description IS NULL OR description = '' THEN 1 ELSE 0 END) AS no_desc
    FROM products
    GROUP BY source
    """
):
    print(dict(row))

print("== sections ==")
for row in conn.execute(
    """
    SELECT source, kind, COUNT(*) AS n
    FROM catalog_sections
    GROUP BY source, kind
    ORDER BY source, kind
    """
):
    print(dict(row))

print("== petrovich top ==")
for row in conn.execute(
    """
    SELECT section_code, name, product_qty, kind, depth
    FROM catalog_sections
    WHERE source = 'petrovich' AND (parent_section_code IS NULL OR depth = 0)
    ORDER BY name
    """
):
    print(dict(row))

print("== lemana groups ==")
for row in conn.execute(
    """
    SELECT name, product_qty, url
    FROM catalog_sections
    WHERE source = 'lemana' AND kind = 'group'
    """
):
    print(dict(row))
