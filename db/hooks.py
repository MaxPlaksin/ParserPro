"""Persist normalized parser rows to SQLite."""

from __future__ import annotations

import logging
from pathlib import Path

from db.repository import get_repository
from normalizer.dns import normalize_dns_row
from normalizer.mvideo import normalize_mvideo_row

logger = logging.getLogger(__name__)


def persist_dns_row(row: dict, category_dir: Path | None = None) -> None:
    if not row.get("product_id"):
        return
    try:
        record = normalize_dns_row(row, category_dir=category_dir)
        product_id = get_repository().upsert_product(record)
        logger.info("DB upsert dns:%s -> product_id=%s", row["product_id"], product_id)
    except Exception as exc:
        logger.warning("DB upsert dns:%s failed: %s", row.get("product_id"), exc)


def persist_mvideo_row(row: dict, category_dir: Path | None = None) -> None:
    if not row.get("product_id"):
        return
    try:
        record = normalize_mvideo_row(row, category_dir=category_dir)
        product_id = get_repository().upsert_product(record)
        logger.info("DB upsert mvideo:%s -> product_id=%s", row["product_id"], product_id)
    except Exception as exc:
        logger.warning("DB upsert mvideo:%s failed: %s", row.get("product_id"), exc)
