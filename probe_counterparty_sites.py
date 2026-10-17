#!/usr/bin/env python3
"""Probe Checko, Kontur Focus and FNS web access via CloakBrowser."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from common import CloakSession, setup_logging

# Test INN: ПАО Сбербанк — есть во всех открытых реестрах.
DEFAULT_INN = "7707083893"

SITES = {
    "checko": {
        "home": "https://checko.ru/",
        # Прямой /company/{inn} даёт 404 — нужен поиск с редиректом на slug.
        "company": "https://checko.ru/search?query={inn}",
    },
    "kontur": {
        "home": "https://focus.kontur.ru/",
        "search": "https://focus.kontur.ru/entity?query={inn}",
    },
    "fns_egrul": {
        "home": "https://egrul.nalog.ru/index.html",
    },
    "fns_bo": {
        "home": "https://bo.nalog.gov.ru/",
        "search": "https://bo.nalog.gov.ru/organizations-card/{inn}",
    },
}


@dataclass
class ProbeResult:
    site: str
    url: str
    status: int | None = None
    title: str = ""
    final_url: str = ""
    html_len: int = 0
    blocked: bool = False
    captcha: bool = False
    login_required: bool = False
    has_inn: bool = False
    hints: list[str] = field(default_factory=list)
    error: str = ""


def _detect_signals(html: str, title: str, inn: str) -> tuple[bool, bool, bool, list[str]]:
    low = (html + " " + title).lower()
    blocked = any(
        x in low
        for x in (
            "403 forbidden",
            "access denied",
            "доступ запрещ",
            "заблокирован",
            "too many requests",
            "429",
        )
    )
    captcha = any(
        x in low
        for x in (
            "captcha",
            "recaptcha",
            "hcaptcha",
            "smartcaptcha",
            "подтвердите, что вы не робот",
            "проверка безопасности",
        )
    )
    login_required = any(
        x in low
        for x in (
            "войти",
            "авториза",
            "sign in",
            "log in",
            "вход в систему",
        )
    ) and "7707083893" not in html

    hints: list[str] = []
    if inn in html:
        hints.append("inn_in_html")
    if re.search(r"выручк", low):
        hints.append("revenue_keyword")
    if re.search(r"прибыл", low):
        hints.append("profit_keyword")
    if re.search(r"исполнительн", low):
        hints.append("enforcement_keyword")
    if re.search(r"арбитраж", low):
        hints.append("arbitration_keyword")
    if re.search(r"бухгалтерск", low):
        hints.append("accounting_keyword")

    return blocked, captcha, login_required, hints


async def probe_url(
    session: CloakSession,
    site: str,
    url: str,
    *,
    inn: str,
    settle_sec: float,
) -> ProbeResult:
    result = ProbeResult(site=site, url=url)
    page = await session.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(settle_sec)
        result.status = resp.status if resp else None
        result.title = (await page.title()) or ""
        result.final_url = page.url
        html = await page.content()
        result.html_len = len(html)
        blocked, captcha, login_required, hints = _detect_signals(html, result.title, inn)
        result.blocked = blocked
        result.captcha = captcha
        result.login_required = login_required
        result.has_inn = inn in html
        result.hints = hints
        if result.status and result.status >= 400:
            result.blocked = True
    except Exception as exc:
        result.error = str(exc)
    finally:
        await page.close()
    return result


async def probe_fns_egrul(
    session: CloakSession,
    inn: str,
    *,
    settle_sec: float,
) -> ProbeResult:
    """Try INN search on egrul.nalog.ru via the public form."""
    result = ProbeResult(site="fns_egrul", url=SITES["fns_egrul"]["home"])
    page = await session.new_page()
    try:
        resp = await page.goto(SITES["fns_egrul"]["home"], wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(settle_sec)
        result.status = resp.status if resp else None
        result.title = (await page.title()) or ""
        result.final_url = page.url

        filled = False
        for selector in (
            'textarea[name="query"]',
            'input[name="query"]',
            'textarea#query',
            'input#query',
            'textarea',
        ):
            el = page.locator(selector).first
            if await el.count() > 0:
                await el.fill(inn)
                filled = True
                break

        if filled:
            for selector in (
                'button[type="submit"]',
                'button:has-text("Найти")',
                'input[type="submit"]',
                '#btnSearch',
                'button.btn-search',
            ):
                btn = page.locator(selector).first
                if await btn.count() > 0:
                    await btn.click()
                    break
            await asyncio.sleep(settle_sec + 2)
            result.final_url = page.url

        html = await page.content()
        result.html_len = len(html)
        blocked, captcha, login_required, hints = _detect_signals(html, result.title, inn)
        result.blocked = blocked
        result.captcha = captcha
        result.login_required = login_required
        result.has_inn = inn in html
        result.hints = hints
        if filled:
            result.hints.append("form_submitted")
        else:
            result.hints.append("form_not_found")
    except Exception as exc:
        result.error = str(exc)
    finally:
        await page.close()
    return result


async def run_probes(inn: str, *, headless: bool, settle_sec: float) -> list[dict[str, Any]]:
    tasks: list[ProbeResult] = []
    async with CloakSession(headless=headless, humanize=True) as session:
        probes = [
            ("checko_home", SITES["checko"]["home"]),
            ("checko_company", SITES["checko"]["company"].format(inn=inn)),
            ("kontur_home", SITES["kontur"]["home"]),
            ("kontur_search", SITES["kontur"]["search"].format(inn=inn)),
            ("fns_bo", SITES["fns_bo"]["search"].format(inn=inn)),
        ]
        for site, url in probes:
            print(f"  Cloak → {site}: {url}")
            tasks.append(await probe_url(session, site, url, inn=inn, settle_sec=settle_sec))

        print("  Cloak → fns_egrul: form search")
        tasks.append(await probe_fns_egrul(session, inn, settle_sec=settle_sec))

    return [asdict(r) for r in tasks]


def summarize(results: list[dict[str, Any]]) -> None:
    print("\n=== SUMMARY ===")
    for row in results:
        site = row["site"]
        ok = not row.get("blocked") and not row.get("captcha") and not row.get("error")
        flags = []
        if row.get("blocked"):
            flags.append("BLOCKED")
        if row.get("captcha"):
            flags.append("CAPTCHA")
        if row.get("login_required"):
            flags.append("LOGIN")
        if row.get("has_inn"):
            flags.append("INN_OK")
        if row.get("hints"):
            flags.append("hints=" + ",".join(row["hints"]))
        status = row.get("status")
        print(
            f"{site:16} status={status} len={row.get('html_len', 0):6} "
            f"{'OK' if ok else 'FAIL'} [{', '.join(flags) or '-'}]"
        )
        if row.get("error"):
            print(f"  error: {row['error']}")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Probe counterparty data sources via CloakBrowser")
    parser.add_argument("--inn", default=DEFAULT_INN, help="Test INN (default: Sberbank)")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Headless mode (default: visible CloakBrowser window)",
    )
    parser.add_argument(
        "--settle-sec",
        type=float,
        default=6.0,
        help="Wait after page load (seconds)",
    )
    args = parser.parse_args()

    from cloakbrowser.download import ensure_binary

    binary = ensure_binary()
    print(f"Probing counterparty sites for INN {args.inn}...")
    print(f"CloakBrowser: {binary}")
    print(f"headless={args.headless}, settle={args.settle_sec}s")

    results = asyncio.run(
        run_probes(args.inn, headless=args.headless, settle_sec=args.settle_sec)
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    summarize(results)

    failed = sum(
        1
        for r in results
        if r.get("blocked") or r.get("captcha") or r.get("error")
    )
    sys.exit(0 if failed < len(results) else 1)


if __name__ == "__main__":
    main()
