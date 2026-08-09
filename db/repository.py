"""SQLite persistence with WAL, cross-process file lock and serialized writes."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from db.migrate import migrate
from models.product_record import PriceHistoryEntry, ProductImage, ProductRecord
from storage_paths import normalize_image_db_path

if sys.platform == "win32":
    import msvcrt

    def _acquire_file_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _release_file_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _acquire_file_lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _release_file_lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "products.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_repository: ProductRepository | None = None
_repository_lock = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalize_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ProductRepository:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.lock_path = self.db_path.with_suffix(".lock")
        self._thread_lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._file_lock():
            conn = self._connect()
            try:
                conn.executescript(schema_sql)
                migrate(conn)
                conn.commit()
            finally:
                conn.close()

    def _file_lock(self):
        return _FileLock(self.lock_path)

    def upsert_product(self, record: ProductRecord) -> int:
        with self._thread_lock:
            with self._file_lock():
                conn = self._connect()
                try:
                    return self._upsert_product(conn, record)
                finally:
                    conn.close()

    def _upsert_category(self, conn: sqlite3.Connection, record: ProductRecord, now: str) -> int:
        conn.execute(
            """
            INSERT INTO categories (source, source_category_id, name, path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_category_id) DO UPDATE SET
                name = excluded.name,
                path = excluded.path,
                updated_at = excluded.updated_at
            """,
            (
                record.category_source,
                record.category_source_id,
                record.category_name,
                record.category_path,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM categories WHERE source = ? AND source_category_id = ?",
            (record.category_source, record.category_source_id),
        ).fetchone()
        return int(row["id"])

    def _product_snapshot(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "category_id": row["category_id"],
            "url": row["url"] or "",
            "brand": row["brand"] or "",
            "name": row["name"] or "",
            "sku": row["sku"] or "",
            "price": row["price"],
            "old_price": row["old_price"],
            "price_min_observed": row["price_min_observed"],
            "price_max_observed": row["price_max_observed"],
            "currency": row["currency"] or "RUB",
            "available": row["available"],
            "rating": row["rating"],
            "reviews_count": row["reviews_count"],
            "description": row["description"] or "",
            "description_short": row["description_short"] or "",
            "description_detailed": row["description_detailed"] or "",
            "description_full": row["description_full"] or "",
            "raw_full_text": row["raw_full_text"] or "",
            "reliability_percent": row["reliability_percent"],
            "reliability_text": row["reliability_text"] or "",
            "display": row["display"] or "",
            "dimensions": row["dimensions"] or "",
            "equipment": row["equipment"] or "",
            "price_history_text": row["price_history_text"] or "",
            "extra_json": row["extra_json"] or "{}",
            "is_active": row["is_active"],
        }

    def _record_snapshot(self, record: ProductRecord, category_id: int) -> dict[str, Any]:
        return {
            "category_id": category_id,
            "url": record.url,
            "brand": record.brand,
            "name": record.name,
            "sku": record.sku,
            "price": record.price,
            "old_price": record.old_price,
            "price_min_observed": record.price_min_observed,
            "price_max_observed": record.price_max_observed,
            "currency": record.currency,
            "available": None if record.available is None else int(record.available),
            "rating": record.rating,
            "reviews_count": record.reviews_count,
            "description": record.description,
            "description_short": record.description_short,
            "description_detailed": record.description_detailed,
            "description_full": record.description_full,
            "raw_full_text": record.raw_full_text,
            "reliability_percent": record.reliability_percent,
            "reliability_text": record.reliability_text,
            "display": record.display,
            "dimensions": record.dimensions,
            "equipment": record.equipment,
            "price_history_text": record.price_history_text,
            "extra_json": _json_dumps(record.extra),
            "is_active": 1,
        }

    def _upsert_product(self, conn: sqlite3.Connection, record: ProductRecord) -> int:
        now = utc_now_iso()
        category_id = self._upsert_category(conn, record, now)
        incoming = self._record_snapshot(record, category_id)

        existing = conn.execute(
            "SELECT * FROM products WHERE source = ? AND source_id = ?",
            (record.source, record.source_id),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO products (
                    source, source_id, category_id, url, brand, name, sku,
                    price, old_price, price_min_observed, price_max_observed,
                    currency, available, rating, reviews_count,
                    description, description_short, description_detailed, description_full,
                    raw_full_text, reliability_percent, reliability_text,
                    display, dimensions, equipment, price_history_text, extra_json,
                    created_at, updated_at, last_seen_at, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.source,
                    record.source_id,
                    incoming["category_id"],
                    incoming["url"],
                    incoming["brand"],
                    incoming["name"],
                    incoming["sku"],
                    incoming["price"],
                    incoming["old_price"],
                    incoming["price_min_observed"],
                    incoming["price_max_observed"],
                    incoming["currency"],
                    incoming["available"],
                    incoming["rating"],
                    incoming["reviews_count"],
                    incoming["description"],
                    incoming["description_short"],
                    incoming["description_detailed"],
                    incoming["description_full"],
                    incoming["raw_full_text"],
                    incoming["reliability_percent"],
                    incoming["reliability_text"],
                    incoming["display"],
                    incoming["dimensions"],
                    incoming["equipment"],
                    incoming["price_history_text"],
                    incoming["extra_json"],
                    now,
                    now,
                    now,
                    1,
                ),
            )
            product_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            data_changed = True
        else:
            product_id = int(existing["id"])
            current = self._product_snapshot(existing)
            data_changed = current != incoming
            if data_changed:
                conn.execute(
                    """
                    UPDATE products SET
                        category_id = ?, url = ?, brand = ?, name = ?, sku = ?,
                        price = ?, old_price = ?, price_min_observed = ?, price_max_observed = ?,
                        currency = ?, available = ?,
                        rating = ?, reviews_count = ?, description = ?,
                        description_short = ?, description_detailed = ?, description_full = ?,
                        raw_full_text = ?, reliability_percent = ?, reliability_text = ?,
                        display = ?, dimensions = ?, equipment = ?, price_history_text = ?,
                        extra_json = ?, updated_at = ?, last_seen_at = ?, is_active = ?
                    WHERE id = ?
                    """,
                    (
                        incoming["category_id"],
                        incoming["url"],
                        incoming["brand"],
                        incoming["name"],
                        incoming["sku"],
                        incoming["price"],
                        incoming["old_price"],
                        incoming["price_min_observed"],
                        incoming["price_max_observed"],
                        incoming["currency"],
                        incoming["available"],
                        incoming["rating"],
                        incoming["reviews_count"],
                        incoming["description"],
                        incoming["description_short"],
                        incoming["description_detailed"],
                        incoming["description_full"],
                        incoming["raw_full_text"],
                        incoming["reliability_percent"],
                        incoming["reliability_text"],
                        incoming["display"],
                        incoming["dimensions"],
                        incoming["equipment"],
                        incoming["price_history_text"],
                        incoming["extra_json"],
                        now,
                        now,
                        1,
                        product_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE products SET last_seen_at = ?, is_active = 1 WHERE id = ?",
                    (now, product_id),
                )

        self._upsert_characteristics(conn, product_id, record.characteristics, now, force=data_changed)
        self._upsert_images(
            conn,
            product_id,
            record.images,
            now,
            source=record.source,
            source_id=record.source_id,
        )
        self._upsert_price_history(conn, product_id, record, now)
        conn.commit()
        return product_id

    def _upsert_characteristics(
        self,
        conn: sqlite3.Connection,
        product_id: int,
        characteristics: dict[str, str],
        now: str,
        *,
        force: bool,
    ) -> None:
        payload = _json_dumps(characteristics)
        existing = conn.execute(
            "SELECT characteristics_json FROM product_characteristics WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO product_characteristics (product_id, characteristics_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (product_id, payload, now),
            )
            return
        if force or existing["characteristics_json"] != payload:
            conn.execute(
                """
                UPDATE product_characteristics
                SET characteristics_json = ?, updated_at = ?
                WHERE product_id = ?
                """,
                (payload, now, product_id),
            )

    def _upsert_images(
        self,
        conn: sqlite3.Connection,
        product_id: int,
        images: list[ProductImage],
        now: str,
        *,
        source: str,
        source_id: str,
    ) -> None:
        for image in images:
            path = normalize_image_db_path(
                image.path, default_source=source, default_source_id=source_id
            )
            if not path.startswith("storage/images/"):
                logger.warning("Skip non-canonical image path: %s", image.path)
                continue
            conn.execute(
                """
                INSERT INTO product_images (product_id, path, source_url, kind, position, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, path) DO UPDATE SET
                    source_url = excluded.source_url,
                    position = excluded.position
                """,
                (product_id, path, image.source_url, image.kind, image.position, now),
            )

    def _upsert_price_history(
        self,
        conn: sqlite3.Connection,
        product_id: int,
        record: ProductRecord,
        now: str,
    ) -> None:
        entries: list[PriceHistoryEntry] = list(record.price_history_entries)
        if not entries and record.price is not None:
            entries = [PriceHistoryEntry(price=record.price, old_price=record.old_price, kind="snapshot")]
        for entry in entries:
            if entry.price is None:
                continue
            price = float(entry.price)
            last = conn.execute(
                """
                SELECT price FROM price_history
                WHERE product_id = ?
                ORDER BY recorded_at DESC, id DESC
                LIMIT 1
                """,
                (product_id,),
            ).fetchone()
            if last is not None and float(last["price"]) == price:
                continue
            conn.execute(
                """
                INSERT INTO price_history (product_id, price, old_price, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (product_id, price, entry.old_price, now),
            )


class _FileLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._handle = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "a+b")
        _acquire_file_lock(self._handle)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._handle is not None:
            try:
                _release_file_lock(self._handle)
            finally:
                self._handle.close()
                self._handle = None


def get_repository(db_path: Path | str | None = None) -> ProductRepository:
    global _repository
    with _repository_lock:
        if _repository is None:
            _repository = ProductRepository(db_path)
        return _repository
