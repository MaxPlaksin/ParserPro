#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common import CloakSession, setup_logging

setup_logging()

JS = """
() => {
  const st = window.INITIAL_STATE || null;
  const plp = st?.plp?.plp || null;
  const products = plp?.products || {};
  return {
    href: location.href,
    hasState: !!st,
    top: st ? Object.keys(st).slice(0, 15) : [],
    plpKeys: plp ? Object.keys(plp) : [],
    ids: (products.productsIds || []).length,
    data: (products.productsData || []).length,
    count: products.productsCount || null,
    dataKeys: products.productsData && products.productsData[0] ? Object.keys(products.productsData[0]).slice(0, 8) : [],
  };
}
"""


async def main():
    async with CloakSession(headless=False, profile_dir=str(ROOT / "output" / "lemana" / "profile")) as session:
        page = await session.new_page()
        await page.goto("https://lemanapro.ru/catalogue/dekorativnye-paneli/", wait_until="domcontentloaded", timeout=120000)
        for i in range(6):
            await page.wait_for_timeout(2000)
            data = await page.evaluate(JS)
            print(i, json.dumps(data, ensure_ascii=False))
            if data.get("data"):
                break


if __name__ == "__main__":
    asyncio.run(main())
