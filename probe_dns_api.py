#!/usr/bin/env python3
import asyncio
import json
import re

from cloakbrowser import launch_async


async def main() -> None:
    browser = await launch_async(headless=False, humanize=True)
    page = await browser.new_page()
    url = "https://www.dns-shop.ru/product/f89a801b62bbd21a/69-smartfon-xiaomi-redmi-15-256-gb-cernyj/"
    await page.goto(url, wait_until="load", timeout=120000)
    await asyncio.sleep(5)
    html = await page.content()
    scripts = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    for s in scripts:
        try:
            data = json.loads(s.strip())
            print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        except Exception as exc:
            print("parse err", exc, s[:200])

    imgs = re.findall(r'https://c\.dns-shop\.ru/[^"\']+\.(?:webp|jpg|jpeg|png)', html)
    print("images:", len(set(imgs)), list(set(imgs))[:5])

    # pagination
    await page.goto(
        "https://www.dns-shop.ru/catalog/17a8a01d16404e77/smartfony/?p=2",
        wait_until="load",
        timeout=120000,
    )
    await asyncio.sleep(4)
    html2 = await page.content()
    p2 = re.findall(r'href="(/product/[a-f0-9]{16}/[^"]+)"', html2)
    print("page2 products:", len(p2), p2[0] if p2 else None)

    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
