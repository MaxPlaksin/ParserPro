#!/usr/bin/env python3
from pathlib import Path

t = Path("output/lemana/plp.js").read_text(encoding="utf-8")
for needle in ("LeroymerlinWebsite", "navigation-plp", "orchestrator", "x-api-key", "apiKey"):
    i = 0
    n = 0
    print("\n====", needle)
    while n < 3:
        i = t.find(needle, i)
        if i < 0:
            break
        print(t[max(0, i - 120) : i + 220].replace("\n", " "))
        print("---")
        i += len(needle)
        n += 1
