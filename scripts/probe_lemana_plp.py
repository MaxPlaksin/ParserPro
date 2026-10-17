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
() => {
  const clip = (obj, depth) => {
    if (depth <= 0 || obj == null) return obj;
    if (Array.isArray(obj)) {
      return { __array: obj.length, first: obj[0] ? clip(obj[0], depth - 1) : null };
    }
    if (typeof obj !== 'object') return obj;
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === 'string' && v.length > 180) out[k] = v.slice(0, 180);
      else if (v && typeof v === 'object') out[k] = clip(v, depth - 1);
      else out[k] = v;
    }
    return out;
  };
  const header = window.INITIAL_STATE?.['plp-header'] || {};
  const plp = window.INITIAL_STATE?.plp?.plp || {};
  return {
    orchestratorHost: header.orchestratorHost,
    orchestratorBackendHost: header.orchestratorBackendHost,
    apiKey: header.apiKey,
    host: header.host,
    plpKeys: Object.keys(plp),
    plp: clip(plp, 3),
  };
}
"""

PDP_JS = r"""
() => {
  const clip = (obj, depth) => {
    if (depth <= 0 || obj == null) return null;
    if (Array.isArray(obj)) return { __array: obj.length, firstKeys: obj[0] && typeof obj[0] === 'object' ? Object.keys(obj[0]).slice(0, 30) : typeof obj[0] };
    if (typeof obj !== 'object') return typeof obj === 'string' ? obj.slice(0, 120) : obj;
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[k] = clip(v, depth - 1);
    return out;
  };
  const pdp = window.INITIAL_STATE?.pdp?.pdp || {};
  return { keys: Object.keys(pdp), tree: clip(pdp, 3) };
}
"""


async def main():
    out = ROOT / "output" / "lemana"
    async with CloakSession(headless=False, profile_dir=str(out / "profile")) as session:
        page = await session.new_page()
        await page.goto("https://lemanapro.ru/catalogue/dekorativnye-paneli/", wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(4000)
        data = await page.evaluate(JS)
        (out / "plp_clip.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("host", data.get("orchestratorHost"), "key", str(data.get("apiKey"))[:12])
        print("plpKeys", data.get("plpKeys"))

        await page.goto(
            "https://lemanapro.ru/product/stenovaya-panel-pvh-belyy-matovyy-3000x250x5-mm-075-m-81944895/",
            wait_until="domcontentloaded",
            timeout=120000,
        )
        await page.wait_for_timeout(4000)
        pdp = await page.evaluate(PDP_JS)
        (out / "pdp_clip.json").write_text(json.dumps(pdp, ensure_ascii=False, indent=2), encoding="utf-8")
        print("pdp keys", pdp.get("keys"))


if __name__ == "__main__":
    asyncio.run(main())
