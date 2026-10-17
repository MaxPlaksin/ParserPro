"""Checko.ru web provider — CloakBrowser + HTML/embedded JSON parsing."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict

from common import CloakSession
from counterparty.providers.base import CounterpartyCheckProvider
from counterparty.version import PARSER_VERSION
from models.company_check_record import CompanyCheckRecord
from normalizer.checko import build_company_check_record, extract_ogrn_from_url

logger = logging.getLogger(__name__)

CHECKO_SEARCH = "https://checko.ru/search?query={inn}"
BLOCKED_STATUS = {403, 429}
DEFAULT_PROFILE = "output/checko/cloak_profile"


class CheckoWebProvider(CounterpartyCheckProvider):
    name = "checko_web"

    def __init__(
        self,
        *,
        headless: bool | None = None,
        delay_sec: float | None = None,
        profile_dir: str | None = None,
    ):
        if headless is None:
            headless = os.getenv("CHECKO_HEADLESS", "true").lower() in ("1", "true", "yes")
        self.headless = headless
        self.delay_sec = delay_sec if delay_sec is not None else float(os.getenv("CHECKO_DELAY_SEC", "5"))
        self.profile_dir = profile_dir or os.getenv("CHECKO_PROFILE_DIR", DEFAULT_PROFILE)

    async def _fetch_html(self, page, url: str) -> tuple[str, str, int | None]:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await asyncio.sleep(self.delay_sec)
        status = resp.status if resp else None
        if status in BLOCKED_STATUS:
            raise RuntimeError(f"Checko blocked HTTP {status}: {url}")
        return await page.content(), page.url, status

    async def _resolve_company(self, page, inn: str) -> tuple[str, str, str]:
        html, final_url, _ = await self._fetch_html(page, CHECKO_SEARCH.format(inn=inn))
        if "/company/" not in final_url:
            raise RuntimeError(f"Company not found for INN {inn}")
        ogrn = extract_ogrn_from_url(final_url)
        if not ogrn:
            raise RuntimeError(f"Cannot extract OGRN from {final_url}")
        return ogrn, html, final_url

    async def fetch(self, inn: str) -> CompanyCheckRecord:
        async with CloakSession(
            headless=self.headless,
            humanize=True,
            profile_dir=self.profile_dir,
        ) as session:
            page = await session.new_page()
            try:
                ogrn, main_html, company_url = await self._resolve_company(page, inn)
                base = f"https://checko.ru/company/{ogrn}"
                pages: dict[str, tuple[str, str]] = {}

                for key, path in (
                    ("accounting", f"{base}/finances/accounting"),
                    ("legal", f"{base}/legal-cases"),
                    ("enforcement", f"{base}/enforcements"),
                ):
                    html, url, _ = await self._fetch_html(page, path)
                    pages[key] = (html, url)

                record = build_company_check_record(
                    inn=inn,
                    main_html=main_html,
                    accounting_html=pages["accounting"][0],
                    legal_html=pages["legal"][0],
                    enforcement_html=pages["enforcement"][0],
                    company_url=company_url,
                    source=self.name,
                    parser_version=PARSER_VERSION,
                )
                record.source_url = company_url
                record.raw_data["source_urls"] = {
                    "company": company_url,
                    "accounting": pages["accounting"][1],
                    "legal": pages["legal"][1],
                    "enforcement": pages["enforcement"][1],
                }
                logger.info("CheckoWebProvider OK INN=%s name=%s", inn, record.name)
                return record
            finally:
                await page.close()

    async def fetch_dict(self, inn: str) -> dict:
        return asdict(await self.fetch(inn))
