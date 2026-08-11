import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "products.db"
conn = sqlite3.connect(DB)
regard = conn.execute("SELECT COUNT(*) FROM products WHERE source='regard'").fetchone()[0]
dups = conn.execute(
    "SELECT COUNT(*) FROM (SELECT source, source_id FROM products GROUP BY source, source_id HAVING COUNT(*)>1)"
).fetchone()[0]
print("regard_products", regard)
print("duplicate_groups", dups)
for row in conn.execute("SELECT source, COUNT(*) FROM products GROUP BY source ORDER BY source"):
    print(row[0], row[1])
conn.close()
