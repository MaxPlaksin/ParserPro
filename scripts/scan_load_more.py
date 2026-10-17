#!/usr/bin/env python3
from pathlib import Path

t = Path("output/lemana/plp.js").read_text(encoding="utf-8")
i = t.find("loadMoreProducts")
print(t[i : i + 1500])
print("\n\n==== data-qa")
idx = 0
n = 0
while n < 20:
    j = t.find("data-qa", idx)
    if j < 0:
        break
    snippet = t[j : j + 80]
    if any(x in snippet.lower() for x in ("product", "more", "pagination", "list")):
        print(snippet)
        n += 1
    idx = j + 7
