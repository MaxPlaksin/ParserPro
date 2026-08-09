#!/usr/bin/env python3
"""Quick CloakBrowser test for DNS and M.Video."""
import asyncio
import json

from cloakbrowser import launch_async


async def check_site(name: str, url: str) -> dict:
    browser = await launch_async(headless=False, humanize=True)
    page = await browser.new_page()
    result = {"site": name, "url": url}
    try:
        resp = await page.goto(url, wait_until="load", timeout=120000)
        await asyncio.sleep(5)
        result["status"] = resp.status if resp else None
        result["title"] = await page.title()
        result["blocked"] = "403" in (result["title"] or "") or "ошибка" in (result["title"] or "").lower()
        result["html_len"] = len(await page.content())
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        await browser.close()
    return result


async def main() -> None:
    tests = [
        ("mvideo", "https://www.mvideo.ru/noutbuki-118"),
        ("dns", "https://www.dns-shop.ru/catalog/17a8a01d16404e77/smartfony/"),
    ]
    results = []
    for name, url in tests:
        print(f"Testing {name}...")
        results.append(await check_site(name, url))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
