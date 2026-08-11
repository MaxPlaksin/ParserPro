"""Prepare partial checkpoint for resume test (3 done, 17 pending)."""
from __future__ import annotations

import json
from pathlib import Path

CP_DIR = Path(__file__).resolve().parent.parent / "output" / "regard" / "Комплектующие для ПК" / "Процессоры"
CP = CP_DIR / "checkpoint.json"
ROWS = CP_DIR / "checkpoint.rows.json"
GLOBAL = Path(__file__).resolve().parent.parent / "output" / "regard" / "checkpoint_global.json"

meta = json.loads(CP.read_text(encoding="utf-8"))
rows = json.loads(ROWS.read_text(encoding="utf-8"))
meta["completed_ids"] = ["73872", "718588", "661697"]
meta["pending"] = [
    item
    for item in meta.get("pending", [])
    if str(item.get("product_id")) not in set(meta["completed_ids"])
]
if not meta["pending"]:
    all_pending = [
        {"product_id": str(r["product_id"]), "url": r["url"], "seo_url": r["url"].split("/product/")[1].split("/", 1)[1] if "/product/" in r["url"] else "product"}
        for r in rows[3:20]
    ]
    meta["pending"] = [
        {
            "product_id": str(r["product_id"]),
            "url": r["url"],
            "seo_url": r["url"].rstrip("/").split("/")[-1],
        }
        for r in rows[3:20]
    ]
meta["rows_count"] = 3
meta["category"]["list_complete"] = True
CP.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
ROWS.write_text(json.dumps(rows[:3], ensure_ascii=False, indent=2), encoding="utf-8")
GLOBAL.write_text(json.dumps({"completed_categories": []}, ensure_ascii=False, indent=2), encoding="utf-8")
print("resume checkpoint ready:", len(meta["completed_ids"]), "done,", len(meta["pending"]), "pending")
