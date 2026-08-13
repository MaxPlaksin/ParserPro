#!/usr/bin/env python3
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "output" / "stores_overview.html"
text = p.read_text(encoding="utf-8")
lines = text.splitlines()
print("size_bytes", p.stat().st_size)
print("lines", len(lines))
print("max_line_len", max(len(l) for l in lines))
print("products", text.count('article class="product"'))
print("data_img", text.count("data-img="))
print("img_payloads", "img-payloads" in text)
print("ends_html", text.rstrip().endswith("</html>"))
print("has_analytics", "Аналитика по магазинам" in text)
