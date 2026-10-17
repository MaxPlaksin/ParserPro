#!/usr/bin/env python3
from pathlib import Path

t = Path("output/lemana/plp.js").read_text(encoding="utf-8")
for needle in ("loadMore", "LOAD_MORE", "show-more", "showMore", "fetchMore", "isLoadingMoreProducts"):
    i = t.find(needle)
    print(needle, i)
    if i >= 0:
        print(t[max(0, i - 100) : i + 180])
        print("---")
