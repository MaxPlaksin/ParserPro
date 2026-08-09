#!/usr/bin/env python3
"""Запуск парсеров DNS и M.Video."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from parser_policy import dns_cli_overrides, mvideo_cli_overrides


def run_module(module: str, extra: list[str]) -> int:
    cmd = [sys.executable, module, *extra]
    print(">>>", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> None:
    p = argparse.ArgumentParser(description="Парсеры DNS + M.Video")
    p.add_argument("--test", action="store_true", help="Тест: 1 категория, 5 товаров")
    p.add_argument("--full", action="store_true", help="Полный парсинг всех категорий")
    p.add_argument("--mvideo-only", action="store_true")
    p.add_argument("--dns-only", action="store_true")
    p.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Только M.Video; DNS всегда в видимом браузере (parser_policy)",
    )
    args = p.parse_args()

    base = Path(__file__).parent
    # DNS: headless никогда не передаём — правила в parser_policy.py
    throttle_dns = dns_cli_overrides()
    throttle_mvideo = mvideo_cli_overrides(headless=args.headless)

    if args.test:
        mvideo_args = throttle_mvideo + ["--max-categories", "1", "--limit", "5", "-o", "output/mvideo"]
        dns_args = throttle_dns + ["--max-categories", "1", "--limit", "5", "-o", "output/dns"]
    elif args.full:
        mvideo_args = throttle_mvideo + ["-o", "output/mvideo"]
        dns_args = throttle_dns + ["-o", "output/dns"]
    else:
        mvideo_args = throttle_mvideo + [
            "--category", "118", "--category-slug", "noutbuki", "--limit", "3", "-o", "output/mvideo",
        ]
        dns_args = throttle_dns + [
            "--category", "17a8a01d16404e77", "--limit", "3", "-o", "output/dns",
        ]

    rc = 0
    if not args.dns_only:
        rc = run_module(str(base / "mvideo_parser.py"), mvideo_args) or rc
    if not args.mvideo_only:
        rc = run_module(str(base / "dns_parser.py"), dns_args) or rc
    sys.exit(rc)


if __name__ == "__main__":
    main()
