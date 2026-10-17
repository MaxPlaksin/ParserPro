#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()

FETCH_JS = """
async (productCode) => {
  const base = window.pet4Data?.apiHost || 'https://api.petrovich.ru';
  const city = window.pet4Data?.city?.code || 'spb';
  const q = `city_code=${city}&client_id=pet_site&pet_case=camel`;
  const urls = [
    `${base}/catalog/v5/products/${productCode}?${q}`,
    `${base}/catalog/v5/sections/12101?${q}`,
    `${base}/catalog/v5/sections/12101/products?limit=2&offset=0&sort=popularity_desc&path=/catalog/12101/&${q}`,
  ];
  const out = {};
  for (const url of urls) {
    const r = await fetch(url, { credentials: 'include' });
    out[url] = { status: r.status, json: await r.json() };
  }
  return out;
}
"""


async def main():
    out = ROOT / "output" / "petrovich"
    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()
        await page.goto("https://petrovich.ru/", wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(3000)
        data = await page.evaluate(FETCH_JS, 654059)
        (out / "fetch_probe.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for url, payload in data.items():
            print(payload["status"], url.split("?")[0].split("/")[-2:])
        prod = None
        for url, payload in data.items():
            if "/products/654059" in url and payload["status"] == 200:
                prod = payload["json"]["data"]["product"]
        if prod:
            print("product keys", sorted(prod.keys()))
            Path(out / "product_654059.json").write_text(json.dumps(prod, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
