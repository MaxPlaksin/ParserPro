#!/usr/bin/env python3
import json
import re
from pathlib import Path

html = Path("output/petrovich/probe.html").read_text(encoding="utf-8", errors="ignore")
m = re.search(r"hydrationHeaderState = (\{.*?\})\s*;\s*\n", html)
if not m:
    raise SystemExit("header not found")
data = json.loads(m.group(1))
tops = data.get("topSections") or []
lines = [f"top sections: {len(tops)}"]
for sec in tops:
    lines.append(f"{sec.get('code')}\tqty={sec.get('productQty')}\t{sec.get('title')}")
    for child in (sec.get("sections") or [])[:20]:
        lines.append(f"  {child.get('code')}\tqty={child.get('productQty')}\t{child.get('title')}")
Path("output/petrovich/top_sections.txt").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
