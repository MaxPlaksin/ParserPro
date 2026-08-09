"""Unified product model for DNS and M.Video parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProductImage:
    path: str
    source_url: str = ""
    kind: str = "image"  # image | gif | video
    position: int = 0


@dataclass
class PriceHistoryEntry:
    price: float
    old_price: float | None = None
    kind: str = "current"  # current | history_min | history_max | snapshot


@dataclass
class ProductRecord:
    source: str
    source_id: str
    url: str
    name: str
    category_source: str
    category_source_id: str
    category_name: str
    category_path: str
    brand: str = ""
    sku: str = ""
    price: float | None = None
    old_price: float | None = None
    price_min_observed: float | None = None
    price_max_observed: float | None = None
    available: bool | None = None
    rating: float | None = None
    reviews_count: int | None = None
    description: str = ""
    description_short: str = ""
    description_detailed: str = ""
    description_full: str = ""
    raw_full_text: str = ""
    reliability_percent: float | None = None
    reliability_text: str = ""
    display: str = ""
    dimensions: str = ""
    equipment: str = ""
    price_history_text: str = ""
    characteristics: dict[str, str] = field(default_factory=dict)
    images: list[ProductImage] = field(default_factory=list)
    price_history_entries: list[PriceHistoryEntry] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    currency: str = "RUB"
    is_active: bool = True
