#!/usr/bin/env python3
import asyncio
import json
from cloakbrowser import launch_async

async def main():
    browser = await launch_async(headless=False, humanize=True)
    page = await browser.new_page()
    captured = []
    async def on_resp(r):
        if r.status != 200 or "json" not in (r.headers.get("content-type") or ""):
            return
        url = r.url
        if any(x in url for x in ("product", "characteristic", "catalog", "api")):
            try:
                body = await r.json()
                captured.append((url[:200], body))
            except Exception:
                pass
    page.on("response", on_resp)
    url = "https://www.dns-shop.ru/product/f89a801b62bbd21a/69-smartfon-xiaomi-redmi-15-256-gb-cernyj/"
    await page.goto(url, wait_until="load", timeout=120000)
    await asyncio.sleep(8)
    for u, body in captured[:25]:
        keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
        print(u)
        print(" ", keys)
        if isinstance(body, dict) and "data" in body:
            d = body["data"]
            if isinstance(d, dict):
                print("  data keys", list(d.keys())[:20])
    await browser.close()

asyncio.run(main())
