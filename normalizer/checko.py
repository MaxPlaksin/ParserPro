"""Parse Checko.ru HTML into CompanyCheckRecord."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from models.company_check_record import CompanyCheckRecord

STATEMENT_DATA_RE = re.compile(r"statementData:\s*(\{.*?\})\s*,\s*codesByYear", re.S)
OGRN_FROM_URL_RE = re.compile(r"/company/(?:[^/]+-)?(\d{13})(?:/|$)")
INN_RE = re.compile(r"ИНН\s*(\d{10}|\d{12})")
OGRN_TEXT_RE = re.compile(r"ОГРН\s*(\d{13}|\d{15})")


def _pct_change(old: float | None, new: float | None) -> float | None:
    if old is None or new is None or old == 0:
        return None
    return round((new - old) / abs(old) * 100, 2)


def _parse_int(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def _parse_money_rub(text: str) -> float | None:
    """Parse '34,1 млн руб.' / '26,1 млрд руб.' / '56,7 трлн руб.' to rubles."""
    if not text:
        return None
    clean = unescape(re.sub(r"<[^>]+>", " ", text))
    clean = clean.replace("\u2009", " ").replace("\xa0", " ")
    num_match = re.search(r"([\d\s,]+)", clean)
    if not num_match:
        return None
    num_str = num_match.group(1).replace(" ", "").replace(",", ".")
    try:
        value = float(num_str)
    except ValueError:
        return None
    low = clean.lower()
    if "трлн" in low:
        return value * 1_000_000_000_000
    if "млрд" in low:
        return value * 1_000_000_000
    if "млн" in low:
        return value * 1_000_000
    if "тыс" in low:
        return value * 1_000
    return value


def extract_ogrn_from_url(url: str) -> str:
    match = OGRN_FROM_URL_RE.search(url or "")
    return match.group(1) if match else ""


def parse_statement_data(html: str) -> dict[int, dict[str, float | None]]:
    """Extract revenue (2110) and net profit (2400) by year from accounting page."""
    match = STATEMENT_DATA_RE.search(html)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}

    out: dict[int, dict[str, float | None]] = {}
    for year_str, codes in data.items():
        if not isinstance(codes, dict):
            continue
        try:
            year = int(year_str)
        except ValueError:
            continue
        revenue = codes.get("2110")
        profit = codes.get("2400")
        out[year] = {
            "revenue": float(revenue) if revenue is not None else None,
            "profit": float(profit) if profit is not None else None,
        }
    return out


def _pick_last_two_years(fin: dict[int, dict[str, float | None]]) -> tuple[tuple[int, float | None] | None, tuple[int, float | None] | None]:
    years = sorted(fin.keys())
    if len(years) < 2:
        return None, None
    y1, y2 = years[-2], years[-1]
    return (y1, fin[y1].get("revenue")), (y2, fin[y2].get("revenue"))


def parse_role_block(html: str, role: str) -> dict[str, Any]:
    """Parse 'Истец' / 'Ответчик' stats from legal-cases page."""
    pattern = (
        rf'<div class="mb-2 fw-700">{role}</div>\s*'
        r'<div class="text-huge[^"]*">.*?<\/a>\s*<\/div>\s*'
        r'<div class="mt-2">([^<]+)</div>'
    )
    block_re = re.compile(pattern, re.S | re.I)
    amount_re = re.compile(
        rf'<div class="mb-2 fw-700">{role}</div>\s*<div class="text-huge[^"]*">(.*?)</div>',
        re.S | re.I,
    )
    count_match = block_re.search(html)
    amount_match = amount_re.search(html)
    count_text = count_match.group(1) if count_match else ""
    amount_html = amount_match.group(1) if amount_match else ""
    return {
        "count": _parse_int(count_text),
        "amount": _parse_money_rub(amount_html),
    }


def parse_enforcements(html: str) -> dict[str, Any]:
    total_match = re.search(
        r"открыто\s*<a[^>]*>(\d+)\s*исполнительных производств</a>\s*на общую сумму\s*"
        r"(<span class=\"fw-700\">.*?</span>)",
        html,
        re.S | re.I,
    )
    active_match = re.search(
        r'<div class="text-huge mb-1"><a[^>]*>(\d+)</a></div>',
        html,
    )
    debt_match = re.search(
        r'class="text-huge mb-1"><a class="link-danger"[^>]*>(.*?)</a>',
        html,
        re.S,
    )
    return {
        "total_count": int(total_match.group(1)) if total_match else _parse_int(
            active_match.group(1) if active_match else ""
        ),
        "active_count": int(active_match.group(1)) if active_match else None,
        "total_amount": _parse_money_rub(total_match.group(2) if total_match else ""),
        "debt_amount": _parse_money_rub(debt_match.group(1) if debt_match else ""),
    }


def parse_company_identity(html: str, url: str) -> dict[str, str]:
    title = ""
    title_match = re.search(r"<title>([^<]+)</title>", html, re.I)
    if title_match:
        title = unescape(title_match.group(1).split(" - ")[0].strip())
    inn_match = INN_RE.search(html)
    ogrn_match = OGRN_TEXT_RE.search(html)
    return {
        "name": title,
        "inn": inn_match.group(1) if inn_match else "",
        "ogrn": ogrn_match.group(1) if ogrn_match else extract_ogrn_from_url(url),
        "url": url,
    }


def build_company_check_record(
    *,
    inn: str,
    main_html: str,
    accounting_html: str,
    legal_html: str,
    enforcement_html: str,
    company_url: str,
    source: str = "checko_web",
    parser_version: str = "",
) -> CompanyCheckRecord:
    identity = parse_company_identity(main_html or accounting_html, company_url)
    finances = parse_statement_data(accounting_html)
    finances_source = "embedded_json_statementData" if finances else "none"
    years_sorted = sorted(finances.keys())

    rev_y1 = rev_y2 = prof_y1 = prof_y2 = None
    rev_v1 = rev_v2 = prof_v1 = prof_v2 = None
    if len(years_sorted) >= 2:
        y_a, y_b = years_sorted[-2], years_sorted[-1]
        rev_y1, rev_y2 = y_a, y_b
        rev_v1 = finances[y_a].get("revenue")
        rev_v2 = finances[y_b].get("revenue")
        prof_y1, prof_y2 = y_a, y_b
        prof_v1 = finances[y_a].get("profit")
        prof_v2 = finances[y_b].get("profit")

    defendant = parse_role_block(legal_html, "Ответчик")
    defendant_active = parse_role_block(
        legal_html.split('id="legal-cases-tab-2"', 1)[-1] if 'id="legal-cases-tab-2"' in legal_html else "",
        "Ответчик",
    )
    enforcement = parse_enforcements(enforcement_html or main_html)

    record = CompanyCheckRecord(
        inn=inn or identity.get("inn", ""),
        source=source,
        ogrn=identity.get("ogrn", ""),
        name=identity.get("name", ""),
        url=company_url,
        source_url=company_url,
        parser_version=parser_version,
        status="ok",
        revenue_year_1=rev_y1,
        revenue_value_1=rev_v1,
        revenue_year_2=rev_y2,
        revenue_value_2=rev_v2,
        revenue_change_percent=_pct_change(rev_v1, rev_v2),
        profit_year_1=prof_y1,
        profit_value_1=prof_v1,
        profit_year_2=prof_y2,
        profit_value_2=prof_v2,
        profit_change_percent=_pct_change(prof_v1, prof_v2),
        enforcement_total_count=enforcement.get("total_count"),
        enforcement_active_count=enforcement.get("active_count"),
        enforcement_total_amount=enforcement.get("total_amount") or enforcement.get("debt_amount"),
        arbitration_defendant_total_count=defendant.get("count"),
        arbitration_defendant_active_count=defendant_active.get("count"),
        arbitration_defendant_total_amount=defendant.get("amount"),
        raw_data={
            "parse_meta": {
                "finances": finances_source,
                "legal": "html_blocks",
                "enforcement": "html_blocks_fallback" if enforcement_html else "html_main_fallback",
            },
            "finances_by_year": finances,
            "defendant": defendant,
            "defendant_active": defendant_active,
            "enforcement": enforcement,
            "identity": identity,
        },
    )
    return record
