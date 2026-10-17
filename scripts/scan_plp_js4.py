#!/usr/bin/env python3
from pathlib import Path

t = Path("output/lemana/plp.js").read_text(encoding="utf-8")
for needle in ("getProductsByIds:", "\"list\"", "PRODUCTS_HOST", "API_HOST", "ORCHESTRATOR_HOST", "get-sections"):
    i = t.find(needle)
    print("\n====", needle, i)
    if i >= 0:
        print(t[max(0, i - 200) : i + 350])
