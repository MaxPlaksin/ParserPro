#!/usr/bin/env python3
import re
from pathlib import Path

t = Path("output/lemana/plp.js").read_text(encoding="utf-8")
print("--- Or() names")
for m in re.finditer(r'Or\(\s*"([^"]+)"', t):
    print(m.group(1))
print("--- quoted paths")
names = set(re.findall(r'"((?:get|post|search)[^"]{2,80})"', t, re.I))
for s in sorted(names):
    print(s)
