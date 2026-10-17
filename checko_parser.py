#!/usr/bin/env python3
"""
Парсер Checko.ru — CLI-обёртка над CheckoWebProvider.

Примеры:
  python checko_parser.py --inn 7707083893
  python checko_parser.py --file inns.txt -o output/checko
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from common import atomic_write_json, setup_logging
from counterparty.providers.checko_web import CheckoWebProvider
from counterparty.version import PARSER_VERSION
from db.counterparty_repository import get_counterparty_repository

CHECKO_DELAY_SEC = 5.0

setup_logging()
logger = logging.getLogger("checko_parser")


async def run_batch(
    inns: list[str],
    *,
    headless: bool,
    settle_sec: float,
    output_dir: Path,
    save_db: bool,
) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    provider = CheckoWebProvider(
        headless=headless,
        delay_sec=settle_sec,
        profile_dir=str(output_dir / "cloak_profile"),
    )
    results: list[dict] = []
    repo = get_counterparty_repository() if save_db else None

    for idx, inn in enumerate(inns, 1):
        inn = inn.strip()
        if not inn:
            continue
        logger.info("[%s/%s] Checko INN %s", idx, len(inns), inn)
        try:
            record = await provider.fetch(inn)
            data = record.__dict__.copy()
            data["status"] = "ok"
            atomic_write_json(output_dir / f"{inn}.json", data)
            if repo:
                repo.upsert_check(record)
            results.append(data)
            logger.info(
                "OK %s revenue=%s/%s enforcement=%s arbitration_def=%s",
                record.name or inn,
                record.revenue_value_1,
                record.revenue_value_2,
                record.enforcement_total_count,
                record.arbitration_defendant_total_count,
            )
        except Exception as exc:
            logger.error("FAIL INN %s: %s", inn, exc)
            err = {
                "inn": inn,
                "source": provider.name,
                "status": "error",
                "error": str(exc),
                "parser_version": PARSER_VERSION,
            }
            atomic_write_json(output_dir / f"{inn}.error.json", err)
            if repo:
                repo.save_error(inn, company_id=None, source=provider.name, error=str(exc), parser_version=PARSER_VERSION)
            results.append(err)
        if idx < len(inns):
            await asyncio.sleep(settle_sec)

    atomic_write_json(output_dir / "summary.json", results)
    return results


def _load_inns(args: argparse.Namespace) -> list[str]:
    if args.inn:
        return args.inn
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
        return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    raise SystemExit("Provide --inn or --file")


def main() -> None:
    parser = argparse.ArgumentParser(description="Checko.ru counterparty parser (CloakBrowser)")
    parser.add_argument("--inn", action="append", help="INN (можно несколько раз)")
    parser.add_argument("--file", help="Файл: один ИНН на строку")
    parser.add_argument("-o", "--output", default="output/checko")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--delay", type=float, default=CHECKO_DELAY_SEC)
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    inns = _load_inns(args)
    from cloakbrowser.download import ensure_binary

    logger.info("CloakBrowser: %s", ensure_binary())
    logger.info("Parser version: %s", PARSER_VERSION)
    logger.info("INNs: %s", ", ".join(inns))

    results = asyncio.run(
        run_batch(
            inns,
            headless=args.headless,
            settle_sec=args.delay,
            output_dir=Path(args.output),
            save_db=not args.no_db,
        )
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
