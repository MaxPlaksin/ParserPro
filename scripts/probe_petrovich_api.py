#!/usr/bin/env python3
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()

CATALOG = "https://petrovich.ru/catalog/12101/"
PRODUCT = "https://petrovich.ru/product/654059/"


async def main():
    out = ROOT / "output" / "petrovich"
    out.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []

    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()

        async def on_response(resp):
            url = resp.url
            if "api.petrovich.ru" not in url:
                return
            ct = resp.headers.get("content-type", "")
            if "json" not in ct and "javascript" not in ct:
                return
            try:
                body = await resp.text()
            except Exception:
                return
            if len(body) > 500_000:
                body = body[:500_000] + "...(truncated)"
            captured.append({"url": url, "status": resp.status, "sample": body[:4000]})

        page.on("response", on_response)

        for url in (CATALOG, PRODUCT):
            captured.clear()
            await page.goto(url, wait_until="networkidle", timeout=180000)
            await page.wait_for_timeout(3000)
            html = await page.content()
            keys = re.findall(r"window\.pet4Data\.(\w+)\s*=", html)
            pet4 = await page.evaluate(
                """() => {
                  const d = window.pet4Data || {};
                  const out = {};
                  for (const k of Object.keys(d)) {
                    if (k.startsWith('hydration') || k === 'city' || k === 'apiHost') {
                      try { out[k] = d[k]; } catch (e) { out[k] = String(d[k]); }
                    }
                  }
                  return out;
                }"""
            )
            slug = "catalog" if "catalog" in url else "product"
            (out / f"pet4_{slug}.json").write_text(
                json.dumps(pet4, ensure_ascii=False, indent=2)[:2_000_000], encoding="utf-8"
            )
            (out / f"api_{slug}.json").write_text(
                json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(slug, "pet4 keys", sorted(keys))
            print(slug, "api calls", len(captured))
            for item in captured[:15]:
                print(" ", item["status"], item["url"][:120])


if __name__ == "__main__":
    asyncio.run(main())
