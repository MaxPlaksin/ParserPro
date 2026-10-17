#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import CloakSession, setup_logging

setup_logging()

DUMP_JS = """
() => {
  const st = window.INITIAL_STATE || {};
  const summary = {};
  for (const [k, v] of Object.entries(st)) {
    if (v && typeof v === 'object') {
      summary[k] = Array.isArray(v) ? ['array', v.length] : Object.keys(v).slice(0, 30);
    } else {
      summary[k] = typeof v;
    }
  }
  return summary;
}
"""

PRODUCT = "https://lemanapro.ru/product/stenovaya-panel-pvh-belyy-matovyy-3000x250x5-mm-075-m-81944895/"
CATALOG = "https://lemanapro.ru/catalogue/dekorativnye-paneli/"


async def main():
    out = ROOT / "output" / "lemana"
    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()
        await page.goto(CATALOG, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(5000)
        summary = await page.evaluate(DUMP_JS)
        (out / "initial_state_keys.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("CATALOG KEYS")
        print(json.dumps(summary, ensure_ascii=False, indent=2)[:5000])

        sample = await page.evaluate(
            """() => {
              const st = window.INITIAL_STATE || {};
              const blob = JSON.stringify(st);
              const keys = ['products', 'plp', 'catalogue', 'catalog', 'listing', 'facets'];
              const found = {};
              function walk(obj, path, depth) {
                if (!obj || typeof obj !== 'object' || depth > 4) return;
                if (Array.isArray(obj)) {
                  if (obj.length && obj[0] && (obj[0].productId || obj[0].articul || obj[0].displayedName || obj[0].price)) {
                    found[path] = {len: obj.length, itemKeys: Object.keys(obj[0]).slice(0, 25)};
                  }
                  return;
                }
                for (const [k, v] of Object.entries(obj)) {
                  if (keys.some(x => k.toLowerCase().includes(x)) || depth < 2) walk(v, path + '.' + k, depth + 1);
                }
              }
              walk(st, 'root', 0);
              return {found, len: blob.length};
            }"""
        )
        print("SAMPLE", json.dumps(sample, ensure_ascii=False)[:4000])
        (out / "listing_shape.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")

        await page.goto(PRODUCT, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(5000)
        prod = await page.evaluate(
            """() => {
              const st = window.INITIAL_STATE || {};
              const summary = {};
              for (const [k, v] of Object.entries(st)) {
                summary[k] = (v && typeof v === 'object' && !Array.isArray(v)) ? Object.keys(v).slice(0, 20) : (Array.isArray(v) ? v.length : typeof v);
              }
              return summary;
            }"""
        )
        print("PRODUCT KEYS", json.dumps(prod, ensure_ascii=False)[:3000])
        (out / "product_state_keys.json").write_text(json.dumps(prod, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
