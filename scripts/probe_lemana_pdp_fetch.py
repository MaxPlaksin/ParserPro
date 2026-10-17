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
async (productId) => {
  const paths = [
    `/experience/LeroymerlinWebsite/v1/navigation-pdp-api/get-product-main-info?productId=${productId}&regionCode=moscow&lang=ru`,
    `/experience/LeroymerlinWebsite/v1/navigation-pdp-api/get-product?productId=${productId}&regionCode=moscow&lang=ru`,
    `/experience/LeroymerlinWebsite/v1/navigation-pdp-api/get-product-info?productId=${productId}&regionCode=moscow&lang=ru`,
    `/hybrid/v1/products/${productId}?regionCode=moscow&lang=ru`,
    `/hybrid/v1/products/${productId}/main-info?regionCode=moscow&lang=ru`,
    `/experience/LeroymerlinWebsite/v1/navigation-plp-api/get-products?productIds=${productId}&regionCode=moscow&lang=ru`,
  ];
  const html = document.documentElement.innerHTML;
  const markers = ['displayedName', 'characteristics', 'certificate', 'description', '__NEXT_DATA__', 'productId'];
  const hits = {};
  for (const m of markers) hits[m] = html.includes(m);
  const out = {hits, calls: []};
  for (const path of paths) {
    const url = 'https://api.lemanapro.ru' + path;
    try {
      const r = await fetch(url, {credentials: 'include'});
      const text = await r.text();
      out.calls.push({status: r.status, path, sample: text.slice(0, 250)});
    } catch (e) {
      out.calls.push({path, error: String(e)});
    }
  }
  return out;
}
"""


async def main():
    out = ROOT / "output" / "lemana"
    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()
        await page.goto(
            "https://lemanapro.ru/product/stenovaya-panel-pvh-belyy-matovyy-3000x250x5-mm-075-m-81944895/",
            wait_until="domcontentloaded",
            timeout=120000,
        )
        await page.wait_for_timeout(3000)
        data = await page.evaluate(JS, "81944895")
        (out / "pdp_fetch.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    asyncio.run(main())
