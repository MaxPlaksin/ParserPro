#!/usr/bin/env python3
from pathlib import Path

t = Path("output/lemana/plp.js").read_text(encoding="utf-8")
idx = 0
n = 0
while n < 6:
    i = t.find("offset:", idx)
    if i < 0:
        break
    snippet = t[max(0, i - 80) : i + 120]
    if "family" in snippet or "product" in snippet or "limit" in snippet:
        print("---", n)
        print(snippet)
        n += 1
    idx = i + 7
