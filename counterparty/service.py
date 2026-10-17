"""Counterparty check orchestration — queue, worker, API response formats."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from counterparty.providers.base import CounterpartyCheckProvider
from counterparty.providers.checko_web import CheckoWebProvider
from counterparty.version import PARSER_VERSION
from db.counterparty_repository import get_counterparty_repository
from models.company_check_record import CompanyCheckRecord

logger = logging.getLogger(__name__)

DEFAULT_TTL_HOURS = int(os.getenv("COMPANY_CHECK_TTL_HOURS", "24"))
WORKER_POLL_SEC = float(os.getenv("COUNTERPARTY_WORKER_POLL_SEC", "2"))
MSK = ZoneInfo("Europe/Moscow")

NOT_FOUND_RE = re.compile(r"not found|не найден", re.I)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_checked_at(ts: str | None) -> str | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return dt.astimezone(MSK).isoformat()


def is_stale(checked_at: str | None, *, ttl_hours: int = DEFAULT_TTL_HOURS) -> bool:
    dt = _parse_iso(checked_at)
    if dt is None:
        return True
    age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    return age.total_seconds() > ttl_hours * 3600


def _year_map(row: dict[str, Any], prefix: str) -> dict[str, Any]:
    y1 = row.get(f"{prefix}_year_1")
    y2 = row.get(f"{prefix}_year_2")
    out: dict[str, Any] = {}
    if y1 is not None:
        out[str(y1)] = row.get(f"{prefix}_value_1")
    if y2 is not None:
        out[str(y2)] = row.get(f"{prefix}_value_2")
    cp = row.get(f"{prefix}_change_percent")
    if cp is not None:
        out["change_percent"] = cp
    return out


def format_post_response(inn: str, status: str) -> dict[str, str]:
    """POST /api/v1/checks — только inn + status."""
    return {"inn": inn, "status": status}


def format_get_response(
    inn: str,
    *,
    row: dict[str, Any] | None = None,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    """GET /api/v1/checks/{inn} — контракт для основной платформы."""
    if status == "queued":
        return {"inn": inn, "status": "queued"}

    if status in ("not_found", "error"):
        out: dict[str, Any] = {
            "inn": inn,
            "status": status,
            "error": error or ("company not found" if status == "not_found" else "parse failed"),
            "checked_at": format_checked_at(row.get("checked_at") if row else None) or format_checked_at(
                datetime.now(timezone.utc).isoformat()
            ),
            "source": (row or {}).get("source") or "checko_web",
        }
        if row and row.get("company_id"):
            out["company_id"] = row["company_id"]
        return out

    # completed
    assert row is not None
    return {
        "inn": inn,
        "company_id": row.get("company_id"),
        "status": "completed",
        "source": row.get("source") or "checko_web",
        "name": row.get("name"),
        "ogrn": row.get("ogrn") or None,
        "checked_at": format_checked_at(row.get("checked_at")),
        "financials": {
            "revenue": _year_map(row, "revenue"),
            "profit": _year_map(row, "profit"),
        },
        "enforcements": {
            "total_count": row.get("enforcement_total_count"),
            "active_count": row.get("enforcement_active_count"),
            "total_amount": row.get("enforcement_total_amount"),
        },
        "arbitration": {
            "defendant_total_count": row.get("arbitration_defendant_total_count"),
            "defendant_12m_count": None,
            "defendant_24m_count": None,
            "total_amount": row.get("arbitration_defendant_total_amount"),
        },
        "error": None,
    }


def _is_fresh_completed(row: dict[str, Any] | None, *, ttl_hours: int) -> bool:
    return bool(
        row
        and row.get("status") in ("ok", "completed")
        and not is_stale(row.get("checked_at"), ttl_hours=ttl_hours)
    )


class CounterpartyCheckService:
    def __init__(self, providers: list[CounterpartyCheckProvider] | None = None):
        self.repo = get_counterparty_repository()
        self.providers: list[CounterpartyCheckProvider] = providers or [CheckoWebProvider()]
        self.ttl_hours = DEFAULT_TTL_HOURS
        self._worker_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start_worker(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._worker_task is None or self._worker_task.done():
            self._stop.clear()
            self._worker_task = loop.create_task(self._worker_loop())
            logger.info("Worker started (poll=%ss, ttl=%sh)", WORKER_POLL_SEC, self.ttl_hours)

    async def stop_worker(self) -> None:
        self._stop.set()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job = self.repo.claim_next_job()
            if job is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=WORKER_POLL_SEC)
                except asyncio.TimeoutError:
                    pass
                continue

            inn = job["inn"]
            company_id = job.get("company_id")
            job_id = job["id"]
            logger.info("Worker: INN %s job #%s", inn, job_id)
            try:
                record = await self._fetch_with_fallback(inn)
                record.company_id = company_id or record.company_id or ""
                if job.get("name") and not record.name:
                    record.name = job["name"]
                record.status = "ok"
                self.repo.upsert_check(record, company_id=company_id)
                self.repo.finish_job(job_id, status="done")
            except Exception as exc:
                logger.exception("Worker failed INN %s: %s", inn, exc)
                msg = str(exc)
                st = "not_found" if NOT_FOUND_RE.search(msg) else "error"
                err_text = "company not found" if st == "not_found" else msg
                self.repo.save_error(
                    inn,
                    company_id=company_id,
                    source=self.providers[0].name if self.providers else "checko_web",
                    error=err_text,
                    parser_version=PARSER_VERSION,
                    status=st,
                )
                self.repo.finish_job(job_id, status="error", error=err_text)

    async def _fetch_with_fallback(self, inn: str) -> CompanyCheckRecord:
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                return await provider.fetch(inn)
            except Exception as exc:
                last_error = exc
                logger.warning("Provider %s failed INN %s: %s", provider.name, inn, exc)
        raise RuntimeError(str(last_error or "No providers configured"))

    def enqueue_check(
        self,
        inn: str,
        *,
        company_id: str | None = None,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, str]:
        if not inn or not inn.strip().isdigit():
            raise ValueError("invalid inn")

        inn = inn.strip()
        cached = self.repo.get_by_inn(inn)

        if not force and _is_fresh_completed(cached, ttl_hours=self.ttl_hours):
            if company_id and cached:
                self.repo.update_company_id(inn, company_id)
            return format_post_response(inn, "completed")

        if self.repo.has_active_job(inn):
            return format_post_response(inn, "queued")

        if not force and cached and _is_fresh_completed(cached, ttl_hours=self.ttl_hours):
            return format_post_response(inn, "completed")

        need_job = force or cached is None or is_stale(cached.get("checked_at"), ttl_hours=self.ttl_hours)
        if need_job or cached is None or cached.get("status") in ("error", "not_found"):
            self.repo.enqueue_job(inn, company_id=company_id, name=name, force_refresh=force)
            self.start_worker()
            return format_post_response(inn, "queued")

        if cached.get("status") in ("ok", "completed"):
            return format_post_response(inn, "completed")
        return format_post_response(inn, "queued")

    def get_result(self, inn: str) -> dict[str, Any]:
        inn = inn.strip()
        if self.repo.has_active_job(inn):
            return format_get_response(inn, status="queued")

        row = self.repo.get_by_inn(inn)
        if row is None:
            return format_get_response(
                inn,
                status="not_found",
                error="company not found",
            )

        db_status = row.get("status") or "ok"
        if db_status == "not_found":
            return format_get_response(inn, row=row, status="not_found", error=row.get("error"))
        if db_status == "error":
            return format_get_response(inn, row=row, status="error", error=row.get("error"))
        return format_get_response(inn, row=row, status="completed")


_service: CounterpartyCheckService | None = None


def get_service() -> CounterpartyCheckService:
    global _service
    if _service is None:
        _service = CounterpartyCheckService()
    return _service
