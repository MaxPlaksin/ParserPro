#!/usr/bin/env python3
from pathlib import Path

t = Path("output/lemana/plp.js").read_text(encoding="utf-8")
idx = 0
n = 0
while n < 15:
    i = t.find("ещ", idx)
    if i < 0:
        break
    snippet = t[max(0, i - 80) : i + 40]
    if "show" in snippet or "more" in snippet or "product" in snippet or "кноп" in snippet.lower() or "Показ" in snippet:
        print(snippet.replace("\n", " "))
        print("---")
        n += 1
    idx = i + 2
print("total done", n)
