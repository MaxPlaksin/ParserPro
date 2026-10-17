#!/usr/bin/env python3
"""Verify Kontur session after manual login."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

PROFILE_DIR = str(ROOT / "output" / "kontur_profile")
TEST_INN = "7736207543"  # ООО Яндекс — выручка на Checko


async def main() -> None:
    setup_logging()
    async with CloakSession(headless=False, humanize=True, profile_dir=PROFILE_DIR) as session:
        page = await session.new_page()
        await page.goto("https://focus.kontur.ru/", wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(4)
        home_html = await page.content()
        login_btn = await page.locator('button:has-text("Войти")').count()
        print("=== HOME ===")
        print("URL:", page.url)
        print("Title:", await page.title())
        print("Login button visible:", login_btn > 0)
        print("Logged in:", login_btn == 0)

        url = f"https://focus.kontur.ru/entity?query={TEST_INN}"
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(6)
        html = await page.content()
        low = html.lower()
        print("\n=== ENTITY", TEST_INN, "===")
        print("URL:", page.url)
        print("Title:", await page.title())
        print("HTML len:", len(html))
        print("INN in page:", TEST_INN in html)
        hints = []
        if re.search(r"выручк", low):
            hints.append("revenue")
        if re.search(r"прибыл", low):
            hints.append("profit")
        if re.search(r"исполнительн", low):
            hints.append("enforcement")
        if re.search(r"арбитраж", low):
            hints.append("arbitration")
        if re.search(r"бухгалтерск", low):
            hints.append("accounting")
        print("Hints:", hints or "none")
        print("Captcha:", "captcha" in low or "smartcaptcha" in low)
        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
