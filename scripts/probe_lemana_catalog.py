#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()
CATALOG = "https://lemanapro.ru/catalogue/dekorativnye-paneli/"


async def main():
    out = ROOT / "output" / "lemana"
    captured: list[dict] = []

    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()

        async def on_response(resp):
            url = resp.url
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct and "javascript" not in ct:
                return
            if any(x in url for x in ("sentry", "mindbox", "google", "yandex", "aclpro")):
                return
            captured.append({"status": resp.status, "url": url[:400], "ct": ct[:40]})

        page.on("response", on_response)
        await page.goto(CATALOG, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(8000)
        state = await page.evaluate(
            """() => {
              const keys = Object.keys(window).filter(k => /data|state|nuxt|next|redux|initial/i.test(k)).slice(0, 40);
              const productLinks = [...document.querySelectorAll('a[href*="/product/"]')].slice(0, 8).map(a => a.href);
              const sub = [...document.querySelectorAll('a[href*="/catalogue/"]')].slice(0, 15).map(a => ({href: a.href, text: (a.textContent||'').replace(/\\s+/g,' ').trim().slice(0,60)}));
              return { url: location.href, title: document.title, keys, productLinks, sub };
            }"""
        )
        (out / "catalog_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "catalog_api.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(state, ensure_ascii=False, indent=2)[:4000])
        print("--- api", len(captured))
        for item in captured[:50]:
            print(item["status"], item["url"][:180])


if __name__ == "__main__":
    asyncio.run(main())
