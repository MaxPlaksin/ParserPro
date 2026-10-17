#!/usr/bin/env python3
"""Снять API-вызовы витрины Лемана ПРО (бывший Леруа Мерлен)."""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()

START_URLS = [
    "https://lemanapro.ru/",
    "https://spb.lemanapro.ru/",
    "https://leroymerlin.ru/",
]


async def main():
    out = ROOT / "output" / "lemana"
    out.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []

    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()

        async def on_response(resp):
            url = resp.url
            if any(x in url for x in ("lemana", "leroymerlin", "lmru", "api.")):
                if any(x in url for x in (".js", ".css", ".png", ".jpg", ".woff", "google", "yandex", "mindbox")):
                    return
                captured.append({"status": resp.status, "url": url[:300]})

        page.on("response", on_response)
        for url in START_URLS:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(4000)
                print("OPEN", page.url, "title", await page.title())
                break
            except Exception as exc:
                print("FAIL", url, exc)

        links = await page.evaluate(
            """() => [...document.querySelectorAll('a[href*="catalog"], a[href*="catalogue"]')]
                .slice(0, 30)
                .map(a => ({href: a.href, text: (a.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80)}))"""
        )
        (out / "probe_links.json").write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "probe_api.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        print("links", len(links), "api", len(captured))
        for item in captured[:40]:
            print(item["status"], item["url"][:160])


if __name__ == "__main__":
    asyncio.run(main())
