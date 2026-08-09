"""Assemble structured product fields from parser rows."""

from __future__ import annotations

import re
from typing import Any

from normalizer.content_filters import clean_review_noise, format_specs_block


def pick_spec_value(specs: dict[str, str], *needles: str) -> str:
    for key, value in specs.items():
        key_lower = key.lower()
        if any(needle.lower() in key_lower for needle in needles):
            return value.strip()
    return ""


def parse_price_int(text: str) -> float | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", str(text))
    if not digits:
        return None
    try:
        return float(int(digits))
    except ValueError:
        return None


def parse_price_range(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    numbers = [parse_price_int(part) for part in re.findall(r"[\d\s]+(?:₽|&#8381;|руб)?", text)]
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return min(numbers), max(numbers)


def build_description_full(record: dict[str, Any]) -> str:
    parts: list[str] = []
    detailed = clean_review_noise(str(record.get("description_detailed") or "").strip())
    if detailed:
        parts.append(detailed)
    for key in ("display", "equipment", "dimensions"):
        value = clean_review_noise(str(record.get(key) or "").strip())
        if value and value not in detailed:
            label = {"display": "Дисплей", "equipment": "Комплектация", "dimensions": "Габариты и вес"}[key]
            parts.append(f"{label}\n{value}")
    reliability_text = clean_review_noise(str(record.get("reliability_text") or "").strip())
    reliability_percent = record.get("reliability_percent")
    if reliability_percent is not None:
        block = f"Надёжность: {reliability_percent}%"
        if reliability_text:
            block = f"{block}\n{reliability_text}"
        if block not in "\n".join(parts):
            parts.append(block)
    price_history_text = clean_review_noise(str(record.get("price_history_text") or "").strip())
    if price_history_text and price_history_text not in "\n".join(parts):
        parts.append(f"История цены\n{price_history_text}")
    specs = record.get("characteristics") or {}
    if isinstance(specs, str):
        spec_block = specs
    else:
        grouped = record.get("_grouped_specs") or []
        spec_block = format_specs_block(grouped) if grouped else ""
        if not spec_block and isinstance(specs, dict) and specs:
            spec_block = format_specs_block([[ "", k, v] for k, v in specs.items()])
    if spec_block and spec_block not in "\n".join(parts):
        parts.append(spec_block)
    return clean_review_noise("\n\n".join(parts))


def build_price_history_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Only real observations with scrape timestamp — not DNS min/max range."""
    current = parse_price_int(row.get("sale_price") or row.get("price"))
    old = parse_price_int(row.get("old_price"))
    if current is None:
        return []
    return [{"price": current, "old_price": old, "kind": "snapshot"}]


def observed_price_bounds(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """DNS price range without dates — stored separately from price_history."""
    min_price = parse_price_int(row.get("price_min_observed") or row.get("price_history_min"))
    max_price = parse_price_int(row.get("price_max_observed") or row.get("price_history_max"))
    return min_price, max_price
