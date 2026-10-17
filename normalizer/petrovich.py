"""Normalize Petrovich parser rows into ProductRecord."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models.product_record import PriceHistoryEntry, ProductImage, ProductRecord
from normalizer.common import build_images_from_row
from normalizer.content_filters import clean_html_description, clean_review_noise
from normalizer.structured import build_description_full


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


def normalize_petrovich_row(row: dict[str, Any], *, category_dir: Path | None = None) -> ProductRecord:
    source_id = str(row.get("product_id") or row.get("code") or "").strip()
    characteristics = row.get("characteristics") or {}
    if isinstance(characteristics, list):
        characteristics = {}

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

    price_history_entries: list[PriceHistoryEntry] = []
    price = _to_float(row.get("sale_price") or row.get("price"))
    old_price = _to_float(row.get("old_price"))
    if price is not None:
        price_history_entries.append(
            PriceHistoryEntry(price=price, old_price=old_price, kind="snapshot")
        )

    extra: dict[str, Any] = {}
    for key in (
        "nomenclature_code",
        "product_guid",
        "barcode",
        "external_id",
        "unit_title",
        "unit_ratio",
        "product_url",
        "resource_links",
        "certificates",
        "certificates_detailed",
        "instructions",
        "instructions_additional",
        "fast_links",
        "links",
        "videos",
        "promos",
        "section_code",
        "group_section_code",
        "subgroup_section_code",
    ):
        if row.get(key) not in (None, "", [], {}):
            extra[key] = row[key]

    images = build_images_from_row(
        row,
        source="petrovich",
        source_id=source_id,
        category_dir=category_dir,
    )
    if not images and row.get("photo_urls"):
        urls = row["photo_urls"] if isinstance(row["photo_urls"], list) else str(row["photo_urls"]).split("|")
        for index, url in enumerate(urls, 1):
            if url:
                images.append(ProductImage(path="", source_url=str(url), kind="image", position=index))

    return ProductRecord(
        source="petrovich",
        source_id=source_id,
        url=str(row.get("url") or row.get("product_url") or ""),
        name=str(row.get("name") or row.get("title") or ""),
        category_source="petrovich",
        category_source_id=str(row.get("category_id") or row.get("section_code") or ""),
        category_name=str(row.get("category_name") or ""),
        category_path=str(row.get("category_path") or ""),
        brand=str(row.get("brand") or ""),
        sku=str(row.get("sku") or row.get("nomenclature_code") or source_id),
        price=price,
        old_price=old_price,
        currency="RUB",
        available=row.get("in_stock") if row.get("in_stock") is not None else None,
        rating=_to_float(row.get("rating")),
        reviews_count=_to_int(row.get("reviews_count")),
        description=description,
        description_short=description_short,
        description_detailed=description_detailed,
        description_full=description_full,
        raw_full_text=raw_full_text,
        characteristics=characteristics if isinstance(characteristics, dict) else {},
        images=images,
        price_history_entries=price_history_entries,
        extra=extra,
    )
