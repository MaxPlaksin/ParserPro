#!/usr/bin/env python3
"""Minimal Citilink browser launch smoke (same CloakSession path as discovery)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from citilink_parser import citilink_profile_dir
from common import CloakSession

DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "citilink"


async def smoke(args: argparse.Namespace) -> None:
    output_root = Path(args.output)
    profile = citilink_profile_dir(output_root, args.profile_suffix)
    print(f"profile={profile}")
    print(f"headless={args.headless}")

    async with CloakSession(headless=args.headless, humanize=True, profile_dir=profile) as session:
        print("browser launched")
        page = await session.new_page()
        await page.goto("https://www.citilink.ru/catalog/processory/", wait_until="load", timeout=120_000)
        title = await page.title()
        print(f"listing opened: {title}")
        await page.close()
    print("browser closed normally")


def main() -> None:
    p = argparse.ArgumentParser(description="Citilink CloakSession browser smoke")
    p.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--profile-suffix", default="_run")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    args = p.parse_args()
    asyncio.run(smoke(args))


if __name__ == "__main__":
    main()
