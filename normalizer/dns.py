"""Normalize DNS parser rows into ProductRecord."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models.product_record import PriceHistoryEntry, ProductRecord
from normalizer.common import build_images_from_row, extract_spec_fields
from normalizer.content_filters import clean_review_noise
from normalizer.structured import build_description_full, build_price_history_entries, observed_price_bounds


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


def normalize_dns_row(row: dict[str, Any], *, category_dir: Path | None = None) -> ProductRecord:
    source_id = str(row.get("product_id", "")).strip()
    category_source_id = str(row.get("category_id") or "")

    characteristics = extract_spec_fields(row)
    marketing = clean_review_noise(str(row.get("description_detailed") or ""))
    if not marketing:
        # Старые checkpoint-строки хранили только поле description.
        marketing = clean_review_noise(str(row.get("description") or ""))
    description_short = clean_review_noise(str(row.get("description_short") or marketing))[:5000]
    description_detailed = marketing[:20000]
    raw_full_text = clean_review_noise(str(row.get("raw_full_text") or marketing))[:50000]

    structured_row = {
        **row,
        "description_detailed": description_detailed,
        "characteristics": characteristics,
    }
    description_full = clean_review_noise(build_description_full(structured_row))[:50000]
    description = description_full or description_detailed or description_short

    price_entries_raw = build_price_history_entries(row)
    price_history_entries = [
        PriceHistoryEntry(
            price=float(item["price"]),
            old_price=_to_float(item.get("old_price")),
            kind=str(item.get("kind") or "snapshot"),
        )
        for item in price_entries_raw
        if item.get("price") is not None
    ]
    price_min_observed, price_max_observed = observed_price_bounds(row)

    extra = {
        key: row[key]
        for key in (
            "product_uuid",
            "media_3d",
            "media_3d_count",
            "spec_count",
            "description_len",
            "price_history_min",
            "price_history_max",
            "price_min_observed",
            "price_max_observed",
            "category_path",
        )
        if row.get(key) not in (None, "")
    }

    return ProductRecord(
        source="dns",
        source_id=source_id,
        url=str(row.get("url") or ""),
        name=str(row.get("name") or ""),
        category_source="dns",
        category_source_id=category_source_id,
        category_name=str(row.get("category_name") or ""),
        category_path=str(row.get("category_path") or ""),
        brand=str(row.get("brand") or ""),
        sku=str(row.get("sku") or ""),
        price=_to_float(row.get("sale_price")),
        old_price=_to_float(row.get("old_price")),
        price_min_observed=price_min_observed,
        price_max_observed=price_max_observed,
        currency=str(row.get("price_currency") or "RUB"),
        available=row.get("in_stock") if row.get("in_stock") is not None else None,
        rating=_to_float(row.get("rating_star")),
        reviews_count=_to_int(row.get("rating_count")),
        description=description,
        description_short=description_short,
        description_detailed=description_detailed,
        description_full=description_full,
        raw_full_text=raw_full_text,
        reliability_percent=_to_float(row.get("reliability_percent")),
        reliability_text=clean_review_noise(str(row.get("reliability_text") or "")),
        display=clean_review_noise(str(row.get("display") or "")),
        dimensions=clean_review_noise(str(row.get("dimensions") or "")),
        equipment=clean_review_noise(str(row.get("equipment") or "")),
        price_history_text=clean_review_noise(str(row.get("price_history_text") or "")),
        characteristics=characteristics,
        images=build_images_from_row(
            row,
            source="dns",
            source_id=source_id,
            category_source_id=category_source_id,
            category_dir=category_dir,
        ),
        price_history_entries=price_history_entries,
        extra=extra,
    )
