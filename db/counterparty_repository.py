"""SQLite persistence for counterparty checks and job queue."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.company_check_record import CompanyCheckRecord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "counterparty.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema_counterparty.sql"

EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("company_id", "TEXT"),
    ("source_url", "TEXT"),
    ("status", "TEXT DEFAULT 'ok'"),
    ("error", "TEXT"),
    ("parser_version", "TEXT"),
)
JOB_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (("name", "TEXT"),)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    raw = data.get("raw_data")
    if isinstance(raw, str) and raw:
        try:
            data["raw_data"] = json.loads(raw)
        except json.JSONDecodeError:
            data["raw_data"] = {}
    return data


class CounterpartyRepository:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(company_checks)")}
        for name, col_type in EXTRA_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE company_checks ADD COLUMN {name} {col_type}")
        job_cols = {row[1] for row in conn.execute("PRAGMA table_info(check_jobs)")}
        for name, col_type in JOB_EXTRA_COLUMNS:
            if name not in job_cols:
                conn.execute(f"ALTER TABLE check_jobs ADD COLUMN {name} {col_type}")

    def _init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._connect() as conn:
            conn.executescript(sql)
            self._migrate_columns(conn)
            conn.execute(
                "UPDATE check_jobs SET status = 'pending', updated_at = ? WHERE status = 'running'",
                (utc_now_iso(),),
            )
            conn.commit()

    def upsert_check(self, record: CompanyCheckRecord, *, company_id: str | None = None) -> int:
        now = utc_now_iso()
        cid = company_id or record.company_id or None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO company_checks (
                    company_id, inn, ogrn, source, name, url, source_url,
                    revenue_year_1, revenue_value_1, revenue_year_2, revenue_value_2,
                    revenue_change_percent,
                    profit_year_1, profit_value_1, profit_year_2, profit_value_2,
                    profit_change_percent,
                    enforcement_total_count, enforcement_active_count, enforcement_total_amount,
                    arbitration_defendant_total_count, arbitration_defendant_active_count,
                    arbitration_defendant_total_amount,
                    status, error, parser_version, raw_data,
                    checked_at, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?
                )
                ON CONFLICT(inn, source) DO UPDATE SET
                    company_id=COALESCE(excluded.company_id, company_checks.company_id),
                    ogrn=excluded.ogrn,
                    name=excluded.name,
                    url=excluded.url,
                    source_url=excluded.source_url,
                    revenue_year_1=excluded.revenue_year_1,
                    revenue_value_1=excluded.revenue_value_1,
                    revenue_year_2=excluded.revenue_year_2,
                    revenue_value_2=excluded.revenue_value_2,
                    revenue_change_percent=excluded.revenue_change_percent,
                    profit_year_1=excluded.profit_year_1,
                    profit_value_1=excluded.profit_value_1,
                    profit_year_2=excluded.profit_year_2,
                    profit_value_2=excluded.profit_value_2,
                    profit_change_percent=excluded.profit_change_percent,
                    enforcement_total_count=excluded.enforcement_total_count,
                    enforcement_active_count=excluded.enforcement_active_count,
                    enforcement_total_amount=excluded.enforcement_total_amount,
                    arbitration_defendant_total_count=excluded.arbitration_defendant_total_count,
                    arbitration_defendant_active_count=excluded.arbitration_defendant_active_count,
                    arbitration_defendant_total_amount=excluded.arbitration_defendant_total_amount,
                    status=excluded.status,
                    error=excluded.error,
                    parser_version=excluded.parser_version,
                    raw_data=excluded.raw_data,
                    checked_at=excluded.checked_at,
                    updated_at=excluded.updated_at
                """,
                (
                    cid,
                    record.inn,
                    record.ogrn,
                    record.source,
                    record.name,
                    record.url,
                    record.source_url or record.url,
                    record.revenue_year_1,
                    record.revenue_value_1,
                    record.revenue_year_2,
                    record.revenue_value_2,
                    record.revenue_change_percent,
                    record.profit_year_1,
                    record.profit_value_1,
                    record.profit_year_2,
                    record.profit_value_2,
                    record.profit_change_percent,
                    record.enforcement_total_count,
                    record.enforcement_active_count,
                    record.enforcement_total_amount,
                    record.arbitration_defendant_total_count,
                    record.arbitration_defendant_active_count,
                    record.arbitration_defendant_total_amount,
                    record.status,
                    record.error or None,
                    record.parser_version,
                    json.dumps(record.raw_data, ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM company_checks WHERE inn = ? AND source = ?",
                (record.inn, record.source),
            ).fetchone()
            conn.commit()
            return int(row["id"])

    def save_error(
        self,
        inn: str,
        *,
        company_id: str | None,
        source: str,
        error: str,
        parser_version: str,
        status: str = "error",
    ) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO company_checks (
                    company_id, inn, source, status, error, parser_version,
                    checked_at, created_at, updated_at, raw_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
                ON CONFLICT(inn, source) DO UPDATE SET
                    company_id=COALESCE(excluded.company_id, company_checks.company_id),
                    status=excluded.status,
                    error=excluded.error,
                    parser_version=excluded.parser_version,
                    checked_at=excluded.checked_at,
                    updated_at=excluded.updated_at
                """,
                (company_id, inn, source, status, error, parser_version, now, now, now),
            )
            conn.commit()

    def update_company_id(self, inn: str, company_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE company_checks SET company_id = ?, updated_at = ? WHERE inn = ?",
                (company_id, utc_now_iso(), inn),
            )
            conn.commit()

    def get_by_inn(self, inn: str, *, source: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if source:
                row = conn.execute(
                    "SELECT * FROM company_checks WHERE inn = ? AND source = ? ORDER BY checked_at DESC LIMIT 1",
                    (inn, source),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM company_checks WHERE inn = ? ORDER BY checked_at DESC LIMIT 1",
                    (inn,),
                ).fetchone()
        return _row_to_dict(row)

    def get_by_company_id(self, company_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM company_checks WHERE company_id = ? ORDER BY checked_at DESC LIMIT 1",
                (company_id,),
            ).fetchone()
        return _row_to_dict(row)

    def enqueue_job(
        self,
        inn: str,
        *,
        company_id: str | None = None,
        name: str | None = None,
        force_refresh: bool = False,
    ) -> int | None:
        now = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM check_jobs WHERE inn = ? AND status IN ('pending', 'running') LIMIT 1",
                (inn,),
            ).fetchone()
            if existing:
                return None
            cur = conn.execute(
                """
                INSERT INTO check_jobs (inn, company_id, name, force_refresh, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (inn, company_id, name, int(force_refresh), now, now),
            )
            conn.commit()
            return int(cur.lastrowid)

    def claim_next_job(self) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM check_jobs WHERE status = 'pending' ORDER BY id ASC LIMIT 1",
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE check_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            conn.commit()
            return dict(row)

    def finish_job(self, job_id: int, *, status: str = "done", error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE check_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error or None, utc_now_iso(), job_id),
            )
            conn.commit()

    def has_active_job(self, inn: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM check_jobs WHERE inn = ? AND status IN ('pending', 'running') LIMIT 1",
                (inn,),
            ).fetchone()
        return row is not None


_repository: CounterpartyRepository | None = None


def get_counterparty_repository(db_path: Path | str | None = None) -> CounterpartyRepository:
    global _repository
    if _repository is None or (db_path and Path(db_path) != _repository.db_path):
        _repository = CounterpartyRepository(db_path)
    return _repository
