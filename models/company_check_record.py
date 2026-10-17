"""Normalized counterparty check record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompanyCheckRecord:
    inn: str
    source: str = "checko_web"
    company_id: str = ""
    ogrn: str = ""
    name: str = ""
    url: str = ""
    source_url: str = ""

    revenue_year_1: int | None = None
    revenue_value_1: float | None = None
    revenue_year_2: int | None = None
    revenue_value_2: float | None = None
    revenue_change_percent: float | None = None

    profit_year_1: int | None = None
    profit_value_1: float | None = None
    profit_year_2: int | None = None
    profit_value_2: float | None = None
    profit_change_percent: float | None = None

    enforcement_total_count: int | None = None
    enforcement_active_count: int | None = None
    enforcement_total_amount: float | None = None

    arbitration_defendant_total_count: int | None = None
    arbitration_defendant_active_count: int | None = None
    arbitration_defendant_total_amount: float | None = None

    status: str = "ok"
    error: str = ""
    parser_version: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
