#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()

URLS = [
    "https://lemanapro.ru/catalogue/dekorativnye-paneli/",
    "https://lemanapro.ru/product/stenovaya-panel-pvh-belyy-matovyy-3000x250x5-mm-075-m-81944895/",
]


async def main():
    out = ROOT / "output" / "lemana"
    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()
        seen: list[str] = []

        def on_request(req):
            url = req.url
            if "api.lemanapro.ru" in url or "/catalogue/" in url and "offset" in url:
                if url not in seen:
                    seen.append(url[:300])

        page.on("request", on_request)
        for url in URLS:
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            await page.wait_for_timeout(7000)
            print("PAGE", page.url)

        info = await page.evaluate(
            """() => {
              const info = window.INITIAL_STATE?.pdp?.pdp?.product?.info;
              if (!info) return {info: null, mediaKeys: Object.keys(window.INITIAL_STATE?.pdp?.pdp?.product?.media || {})};
              return {infoKeys: Object.keys(info), sample: JSON.parse(JSON.stringify(info)).displayedName || info.productId};
            }"""
        )
        (out / "api_urls.json").write_text(json.dumps({"urls": seen, "pdp": info}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("URLS", len(seen))
        for u in seen:
            print(u)
        print("PDP", json.dumps(info, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    asyncio.run(main())
