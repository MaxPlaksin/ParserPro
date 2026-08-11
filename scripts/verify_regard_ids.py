import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "products.db"
conn = sqlite3.connect(DB)
row = conn.execute(
    "SELECT source_id, sku, brand, extra_json FROM products WHERE source='regard' AND source_id='73872'"
).fetchone()
extra = json.loads(row[3])
print("source_id:", row[0])
print("sku:", row[1])
print("brand:", row[2])
print("vendor_code:", extra.get("vendor_code"))
print("model:", extra.get("model"))
print("mpn:", extra.get("mpn"))
conn.close()
