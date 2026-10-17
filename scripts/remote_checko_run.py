#!/usr/bin/env python3
"""Медленный проход Checko по HTTP с отдельного сервера. Только эта папка."""

from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _stub(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Dummy:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


_stub("openpyxl", load_workbook=_Dummy)
_stub("openpyxl.styles", Alignment=_Dummy, Font=_Dummy, PatternFill=_Dummy)
_stub("openpyxl.utils", get_column_letter=lambda index: "A")
_stub("common", CloakSession=_Dummy, atomic_write_json=lambda *a, **k: None, setup_logging=lambda *a, **k: None)
providers = _stub("counterparty.providers")
web = _stub("counterparty.providers.checko_web", CheckoWebProvider=_Dummy)
import counterparty  # noqa: E402
import counterparty.version  # noqa: E402

counterparty.providers = providers
providers.checko_web = web

from score_glavsnab import (  # noqa: E402
    _inactive,
    _plain,
    _section,
    apply_chart_finances,
    build_company_check_record,
    empty_result,
    parse_checko_rating,
    parse_profile,
    parse_status,
    score_record,
)

OUT = ROOT / "out"
INNS = ROOT / "inns.txt"
PAGE_PAUSE = 0.4
COMPANY_PAUSE = 1.2
BLOCK_PAUSE = 60


def fetch(opener: urllib.request.OpenerDirector, url: str) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "ru",
        },
    )
    with opener.open(request, timeout=40) as response:
        status = getattr(response, "status", 200)
        if status in (403, 429):
            raise RuntimeError(f"Checko blocked HTTP {status}: {url}")
        body = response.read().decode("utf-8", "replace")
        return body, response.geturl()


def one(opener: urllib.request.OpenerDirector, inn: str) -> dict:
    html, url = fetch(opener, f"https://checko.ru/search?query={inn}")
    time.sleep(PAGE_PAUSE)
    if "/company/" not in url and "/entrepreneur/" not in url:
        import re

        match = re.search(r'href="(?:https://checko\.ru)?(/(?:company|entrepreneur)/[^"#?]+)"', html)
        if not match:
            return empty_result(f"Company not found for INN {inn}")
        html, url = fetch(opener, "https://checko.ru" + match.group(1))
        time.sleep(PAGE_PAUSE)
    legal = _section(html, "legal-cases") or html
    enforcement_html = _section(html, "enforcements") or html
    record = build_company_check_record(
        inn=inn,
        main_html=html,
        accounting_html="",
        legal_html=legal,
        enforcement_html=enforcement_html,
        company_url=url,
        source="checko_http_remote",
        parser_version="remote",
    )
    enforcement = (record.raw_data or {}).get("enforcement") or {}
    debt = enforcement.get("debt_amount")
    if debt is not None:
        record.enforcement_total_amount = debt
    if record.enforcement_total_count is None and record.enforcement_active_count is None:
        plain = _plain(enforcement_html).lower()
        if "нет сведений" in plain or "не найден" in plain or "отсутств" in plain:
            record.enforcement_total_count = 0
            record.enforcement_active_count = 0
    apply_chart_finances(record, html)
    status, bankruptcy = parse_status(html)
    result = score_record(
        record,
        status=status,
        bankruptcy=bankruptcy,
        checko_rating=parse_checko_rating(html),
        error="",
        profile=parse_profile(html, url),
    )
    if _inactive(status, bankruptcy) and result.get("Светофор") == "Надежный":
        result["Светофор"] = "Рискованный"
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _blocked(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
        return True
    text = str(exc).lower()
    return any(token in text for token in ("429", "403", "blocked", "timed out", "timeout", "connection"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    inns = [line.strip() for line in INNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    print(f"queue {len(inns)}", flush=True)
    for index, inn in enumerate(inns, 1):
        target = OUT / f"{inn}.json"
        if target.exists():
            try:
                previous = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                previous = {}
            error = (previous.get("Ошибка") or "").lower()
            useful = previous.get("Светофор") and not any(
                token in error for token in ("429", "403", "blocked", "timeout", "timed out")
            )
            if useful and (previous.get("Название") or previous.get("Ссылка Checko")):
                print(f"[{index}/{len(inns)}] skip {inn}", flush=True)
                continue
        print(f"[{index}/{len(inns)}] {inn}", flush=True)
        payload = None
        blocked_attempts = 0
        while True:
            try:
                payload = one(opener, inn)
                break
            except Exception as exc:  # noqa: BLE001
                if _blocked(exc):
                    blocked_attempts += 1
                    if blocked_attempts >= 2:
                        with (ROOT / "deferred.txt").open("a", encoding="utf-8") as handle:
                            handle.write(f"{inn}\n")
                        print(f"defer {inn}: {exc}", flush=True)
                        break
                    print(f"blocked, sleep {BLOCK_PAUSE}: {exc}", flush=True)
                    time.sleep(BLOCK_PAUSE)
                    continue
                payload = empty_result(str(exc))
                break
        if payload is None:
            if index < len(inns):
                time.sleep(COMPANY_PAUSE)
            continue
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(
            inn,
            payload.get("Светофор"),
            payload.get("Выручка год 2, руб"),
            flush=True,
        )
        if index < len(inns):
            time.sleep(COMPANY_PAUSE)


if __name__ == "__main__":
    main()
