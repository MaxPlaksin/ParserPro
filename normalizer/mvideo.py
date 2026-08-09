"""Normalize M.Video parser rows into ProductRecord."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models.product_record import PriceHistoryEntry, ProductRecord
from normalizer.common import build_images_from_row, extract_spec_fields
from normalizer.content_filters import clean_html_description, is_review_media_url
from normalizer.structured import build_description_full, build_price_history_entries, pick_spec_value


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


def normalize_mvideo_row(row: dict[str, Any], *, category_dir: Path | None = None) -> ProductRecord:
    source_id = str(row.get("product_id", "")).strip()
    available = None
    if row.get("status_sold_out") is True:
        available = False
    elif row.get("status") == "available" or row.get("in_stock") is True:
        available = True
    elif row.get("status"):
        available = row.get("status") not in {"soldOut", "sold_out", "unavailable"}

    extra = {
        key: row[key]
        for key in (
            "model_name",
            "material_number",
            "detail_category_id",
            "detail_category_name",
            "category_groups",
            "weight_kg",
            "videos",
            "video_count",
            "instructions",
            "instruction_count",
            "certificates",
            "certificate_count",
            "supplier_id",
            "supplier_name",
            "supplier_inn",
            "supplier_ogrn",
            "supplier_phone",
            "supplier_address",
            "status_showcase",
            "status_sold_out",
            "status_retail_only",
            "status_no_exchange",
            "rating_percent",
            "sale_legal",
            "is_preorder",
            "variants",
            "variant_count",
            "spec_count",
            "discount_percent",
            "base_price",
        )
        if row.get(key) not in (None, "")
    }

    category_name = str(row.get("category_name") or row.get("detail_category_name") or "")
    category_path = str(row.get("category_path") or row.get("category_groups") or category_name)
    category_source_id = str(row.get("category_id") or row.get("detail_category_id") or "")
    raw_description = str(row.get("description") or "")
    description_detailed = clean_html_description(raw_description)
    raw_full_text = description_detailed
    characteristics = extract_spec_fields(row)
    description_full = build_description_full(
        {
            "description_detailed": description_detailed,
            "display": pick_spec_value(characteristics, "Дисплей", "Экран", "диагональ"),
            "characteristics": characteristics,
        }
    )
    description = description_full or description_detailed

    price_history_entries = [
        PriceHistoryEntry(
            price=float(item["price"]),
            old_price=_to_float(item.get("old_price")),
            kind=str(item.get("kind") or "current"),
        )
        for item in build_price_history_entries(row)
        if item.get("price") is not None
    ]

    if row.get("videos"):
        filtered_videos = [
            part.strip()
            for part in str(row["videos"]).split("|")
            if part.strip() and not is_review_media_url(part.strip())
        ]
        if filtered_videos:
            extra["videos"] = " | ".join(filtered_videos)
            extra["video_count"] = len(filtered_videos)
        else:
            extra.pop("videos", None)
            extra.pop("video_count", None)

    return ProductRecord(
        source="mvideo",
        source_id=source_id,
        url=str(row.get("url") or ""),
        name=str(row.get("name") or ""),
        category_source="mvideo",
        category_source_id=str(row.get("category_id") or row.get("detail_category_id") or ""),
        category_name=category_name,
        category_path=category_path,
        brand=str(row.get("brand") or ""),
        sku=str(row.get("material_number") or row.get("sku") or source_id),
        price=_to_float(row.get("sale_price")),
        old_price=_to_float(row.get("base_price") or row.get("old_price")),
        currency="RUB",
        available=available,
        rating=_to_float(row.get("rating_star")),
        reviews_count=_to_int(row.get("rating_count")),
        description=description,
        description_short=description_detailed[:5000],
        description_detailed=description_detailed,
        description_full=description_full,
        raw_full_text=raw_full_text,
        display=pick_spec_value(characteristics, "Дисплей", "Экран", "диагональ"),
        characteristics=characteristics,
        images=build_images_from_row(
            row,
            source="mvideo",
            source_id=source_id,
            category_source_id=category_source_id,
            category_dir=category_dir,
        ),
        price_history_entries=price_history_entries,
        extra=extra,
    )
