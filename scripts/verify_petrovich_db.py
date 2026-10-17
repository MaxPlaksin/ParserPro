#!/usr/bin/env python3
import json
import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parent.parent / "data" / "products.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
print("catalog_sections by kind:", conn.execute(
    "SELECT kind, COUNT(*) AS c FROM catalog_sections GROUP BY kind"
).fetchall())
print("sample subgroups:", conn.execute(
    """SELECT section_code, name, kind, depth, product_qty
       FROM catalog_sections WHERE kind = 'subgroup' ORDER BY section_code LIMIT 5"""
).fetchall())
row = conn.execute(
    "SELECT source_id, name, sku, price, url, extra_json FROM products WHERE source = 'petrovich' LIMIT 1"
).fetchone()
extra = json.loads(row["extra_json"])
print("product:", row["source_id"], row["name"][:50], row["price"])
print("resource_links keys:", list((extra.get("resource_links") or {}).keys()))
