"""Normalize Regard parser rows into ProductRecord."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models.product_record import PriceHistoryEntry, ProductRecord
from normalizer.common import build_images_from_row, extract_spec_fields
from normalizer.content_filters import clean_html_description, clean_review_noise
from normalizer.structured import build_description_full, build_price_history_entries


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_regard_row(row: dict[str, Any], *, category_dir: Path | None = None) -> ProductRecord:
    source_id = str(row.get("product_id", "")).strip()
    category_source_id = str(row.get("category_id") or "")

    characteristics = extract_spec_fields(row)
    description_short = clean_review_noise(str(row.get("description_short") or ""))[:5000]
    description_detailed = clean_html_description(str(row.get("description_detailed") or ""))[:20000]
    raw_full_text = clean_review_noise(str(row.get("raw_full_text") or ""))[:50000]

    structured_row = {
        **row,
        "description_detailed": description_detailed,
        "characteristics": characteristics,
    }
    description_full = clean_review_noise(build_description_full(structured_row))[:50000]
    description = description_full or description_detailed or description_short

    price_history_entries = [
        PriceHistoryEntry(
            price=float(item["price"]),
            old_price=_to_float(item.get("old_price")),
            kind=str(item.get("kind") or "snapshot"),
        )
        for item in build_price_history_entries(row)
        if item.get("price") is not None
    ]

    extra: dict[str, Any] = {}
    for key in (
        "model",
        "vendor_code",
        "mpn",
        "ean",
        "gtin",
        "regard_product_id",
        "spec_count",
        "spec_groups",
        "category_path",
        "availability_status",
        "preorder",
        "show_flag",
        "grpid",
        "reviews_count",
        "reviews_stars",
    ):
        if row.get(key) not in (None, ""):
            extra[key] = row[key]

    return ProductRecord(
        source="regard",
        source_id=source_id,
        url=str(row.get("url") or ""),
        name=str(row.get("name") or ""),
        category_source="regard",
        category_source_id=category_source_id,
        category_name=str(row.get("category_name") or ""),
        category_path=str(row.get("category_path") or ""),
        brand=str(row.get("brand") or ""),
        sku=str(row.get("sku") or ""),
        price=_to_float(row.get("sale_price")),
        old_price=_to_float(row.get("old_price")),
        currency="RUB",
        available=row.get("in_stock") if row.get("in_stock") is not None else None,
        rating=_to_float(row.get("rating_star")),
        reviews_count=_to_int(row.get("rating_count")),
        description=description,
        description_short=description_short,
        description_detailed=description_detailed,
        description_full=description_full,
        raw_full_text=raw_full_text,
        characteristics=characteristics,
        images=build_images_from_row(
            row,
            source="regard",
            source_id=source_id,
            category_source_id=category_source_id,
            category_dir=category_dir,
        ),
        price_history_entries=price_history_entries,
        extra=extra,
    )
