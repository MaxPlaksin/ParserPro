#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()

JS = r"""
async () => {
  const st = window.INITIAL_STATE || {};
  const header = st['pdp-header'] || st['plp-header'] || {};
  const apiKey = header.apiKey || '';
  const html = document.documentElement.innerHTML;
  const paths = [...new Set((html.match(/navigation-pdp-api\/[a-z0-9-]+/g) || []).concat(html.match(/navigation-plp-api\/[a-z0-9-]+/g) || []))];
  const ids = st?.plp?.plp?.products?.productsIds || [];
  const count = st?.plp?.plp?.products?.productsCount || null;
  let authed = null;
  if (apiKey) {
    const url = 'https://api.lemanapro.ru/experience/LeroymerlinWebsite/v1/navigation-pdp-api/get-product-main-info?productId=81944895&regionCode=moscow&lang=ru';
    const r = await fetch(url, {headers: {'x-api-key': apiKey}});
    authed = {status: r.status, sample: (await r.text()).slice(0, 400)};
  }
  return {apiKeyLen: apiKey.length, paths, ids: ids.slice(0, 3), count, authed};
}
"""


async def main():
    out = ROOT / "output" / "lemana"
    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()
        for url in (
            "https://lemanapro.ru/catalogue/dekorativnye-paneli/?page=2",
            "https://lemanapro.ru/catalogue/stolyarnye-izdeliya/dekorativnye-paneli/paneli-pvh.html",
        ):
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            await page.wait_for_timeout(4000)
            data = await page.evaluate(JS)
            print(url, json.dumps(data, ensure_ascii=False)[:2000])
            print("---")


if __name__ == "__main__":
    asyncio.run(main())
