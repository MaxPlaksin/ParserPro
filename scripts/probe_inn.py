#!/usr/bin/env python3
"""Quick probe for a single INN on Checko and Kontur."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging
from counterparty.providers.checko_web import CheckoWebProvider

INN = sys.argv[1] if len(sys.argv) > 1 else "9717108903"
PROFILE = str(ROOT / "output" / "kontur_profile")


async def checko() -> None:
    rec = await CheckoWebProvider(headless=True, delay_sec=3).fetch(INN)
    print("=== CHECKO ===")
    print("name:", rec.name)
    print("inn:", rec.inn, "ogrn:", rec.ogrn)
    print("url:", rec.url)
    print(
        "revenue:",
        rec.revenue_year_1,
        rec.revenue_value_1,
        "|",
        rec.revenue_year_2,
        rec.revenue_value_2,
        "chg",
        rec.revenue_change_percent,
    )
    print(
        "profit:",
        rec.profit_year_1,
        rec.profit_value_1,
        "|",
        rec.profit_year_2,
        rec.profit_value_2,
        "chg",
        rec.profit_change_percent,
    )
    print(
        "enforcements:",
        rec.enforcement_total_count,
        rec.enforcement_active_count,
        rec.enforcement_total_amount,
    )
    print(
        "arbitration (defendant):",
        rec.arbitration_defendant_total_count,
        rec.arbitration_defendant_active_count,
        rec.arbitration_defendant_total_amount,
    )


async def kontur() -> None:
    setup_logging()
    async with CloakSession(headless=True, humanize=True, profile_dir=PROFILE) as session:
        page = await session.new_page()
        for url in (
            f"https://focus.kontur.ru/entity?query={INN}",
            f"https://focus.kontur.ru/search?query={INN}",
        ):
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            await asyncio.sleep(8)
            html = await page.content()
            low = html.lower()
            print(f"\n=== KONTUR {url.split('?')[0].split('/')[-1]} ===")
            print("URL:", page.url)
            print("Title:", await page.title())
            print("not_found:", "не существует" in low or "не найдено" in low)
            print("inn:", INN in html)
            print("name hints:", "аир" in low, "групп" in low)
        await page.close()


async def main() -> None:
    await checko()
    try:
        await kontur()
    except Exception as exc:
        print("\n=== KONTUR ERROR ===", type(exc).__name__, exc)


if __name__ == "__main__":
    asyncio.run(main())
