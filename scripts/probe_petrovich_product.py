#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()
PRODUCT = "https://petrovich.ru/product/654059/"


async def main():
    out = ROOT / "output" / "petrovich"
    captured: list[dict] = []

    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()

        async def on_response(resp):
            url = resp.url
            if "api.petrovich.ru" not in url:
                return
            if not any(x in url for x in ("/product", "/catalog", "/document", "/certificate", "/file")):
                return
            try:
                body = await resp.text()
            except Exception:
                return
            captured.append({"url": url, "status": resp.status, "body": body[:8000]})

        page.on("response", on_response)
        await page.goto(PRODUCT, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(8000)
        (out / "api_product.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        for item in captured:
            print(item["status"], item["url"][:140])


if __name__ == "__main__":
    asyncio.run(main())
