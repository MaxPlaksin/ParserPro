"""Зафиксированные правила запуска парсеров DNS и M.Video.

Не менять без live-проверки — DNS ломается при headless и агрессивных retry.
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger(__name__)

# --- DNS (проверено live 2026-08-07) ---
DNS_HEADLESS = False
DNS_MIN_DELAY_SEC = 5.0
DNS_MIN_CATEGORY_DELAY_SEC = 30.0
DNS_WARMUP_DELAY_SEC = 8.0
DNS_PAGE_SETTLE_SEC = 6.0
DNS_CATEGORY_BLOCK_RETRIES = 2
DNS_SESSION_RESTARTS = 3

# --- M.Video ---
MVIDEO_HEADLESS_DEFAULT = False
MVIDEO_MIN_DELAY_SEC = 4.0
MVIDEO_MIN_CATEGORY_DELAY_SEC = 20.0
MVIDEO_MAX_WORKERS = 2


def apply_dns_policy(args: argparse.Namespace) -> None:
    """Принудительно применить рабочую политику DNS."""
    if getattr(args, "headless", False):
        logger.warning(
            "DNS policy: headless запрещён — принудительно видимый браузер "
            "(см. parser_policy.py)"
        )
        args.headless = DNS_HEADLESS
    if args.delay < DNS_MIN_DELAY_SEC:
        logger.warning("DNS policy: delay поднят до %s сек", DNS_MIN_DELAY_SEC)
        args.delay = DNS_MIN_DELAY_SEC
    if args.category_delay < DNS_MIN_CATEGORY_DELAY_SEC:
        logger.warning(
            "DNS policy: category-delay поднят до %s сек",
            DNS_MIN_CATEGORY_DELAY_SEC,
        )
        args.category_delay = DNS_MIN_CATEGORY_DELAY_SEC


def apply_mvideo_policy(args: argparse.Namespace) -> None:
    """Базовые лимиты M.Video."""
    if args.delay < MVIDEO_MIN_DELAY_SEC:
        args.delay = MVIDEO_MIN_DELAY_SEC
    if args.category_delay < MVIDEO_MIN_CATEGORY_DELAY_SEC:
        args.category_delay = MVIDEO_MIN_CATEGORY_DELAY_SEC
    if getattr(args, "workers", 2) > MVIDEO_MAX_WORKERS:
        logger.warning("M.Video policy: workers ограничен до %s", MVIDEO_MAX_WORKERS)
        args.workers = MVIDEO_MAX_WORKERS


def dns_cli_overrides() -> list[str]:
    """Аргументы CLI для DNS — всегда видимый браузер."""
    return [
        "--no-headless",
        "--delay",
        str(int(DNS_MIN_DELAY_SEC)),
        "--category-delay",
        str(int(DNS_MIN_CATEGORY_DELAY_SEC)),
    ]


def mvideo_cli_overrides(*, headless: bool = False) -> list[str]:
    flags = ["--no-headless"] if not headless else ["--headless"]
    return flags + [
        "--delay",
        str(int(MVIDEO_MIN_DELAY_SEC)),
        "--category-delay",
        str(int(MVIDEO_MIN_CATEGORY_DELAY_SEC)),
        "--workers",
        str(MVIDEO_MAX_WORKERS),
    ]
