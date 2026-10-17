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

EXTRACT_JS = """
() => {
  const html = document.documentElement.innerHTML;
  const apis = [...new Set((html.match(/https?:\\/\\/[^"'\\s]+(?:api|catalog|product)[^"'\\s]*/gi) || []).slice(0, 80))];
  const next = document.querySelector('#__NEXT_DATA__');
  let nextData = null;
  if (next) {
    try { nextData = JSON.parse(next.textContent); } catch (e) {}
  }
  const stateKeys = nextData ? Object.keys(nextData?.props?.pageProps || nextData?.props || {}) : [];
  return {
    title: document.title,
    url: location.href,
    apis: apis.slice(0, 40),
    hasNext: !!next,
    stateKeys,
    nextSnippet: next ? next.textContent.slice(0, 2000) : '',
    productLinks: [...document.querySelectorAll('a[href*="/product/"]')].slice(0, 5).map(a => a.href),
    subcats: [...document.querySelectorAll('a[href*="/catalog/"]')].slice(0, 15).map(a => ({href: a.href, text: a.textContent.trim().slice(0, 80)})),
  };
}
"""


async def main():
    url = "https://petrovich.ru/catalog/12101/"
    out_dir = ROOT / "output" / "petrovich"
    out_dir.mkdir(parents=True, exist_ok=True)
    async with CloakSession(headless=False, profile_dir=str(out_dir / "profile")) as session:
        page = await session.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(5000)
        data = await page.evaluate(EXTRACT_JS)
        (out_dir / "probe.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        html = await page.content()
        (out_dir / "probe.html").write_text(html, encoding="utf-8")
        print(json.dumps({k: data[k] for k in data if k != "nextSnippet"}, ensure_ascii=False, indent=2))
        print("nextSnippet len", len(data.get("nextSnippet") or ""))


if __name__ == "__main__":
    asyncio.run(main())
