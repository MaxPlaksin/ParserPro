#!/usr/bin/env python3
"""Unit tests for M.Video fast defer (403) — без обращений к mvideo.ru."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mvideo_parser import (
    CategoryBlockHandler,
    CheckpointState,
    CategoryState,
    MvideoFastDeferConfig,
    Session403Tracker,
    category_403_next_retry_at,
    defer_category_403,
    is_403_recheck_ready,
)


def _config(**overrides) -> MvideoFastDeferConfig:
    base = MvideoFastDeferConfig(
        fast_defer=True,
        defer_after_retry=1,
        recheck_delay_minutes=120,
        consecutive_403_cooldown_after=3,
        session_cooldown_seconds=300,
        short_retry_seconds=30.0,
    )
    return MvideoFastDeferConfig(**{**base.__dict__, **overrides})


async def _run_block_handler_until_defer(handler: CategoryBlockHandler) -> list[float]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with patch("mvideo_parser.asyncio.sleep", side_effect=fake_sleep):
        assert await handler.on_blocked() == "retry"
        assert await handler.on_blocked() == "defer"
    return sleeps


def test_a_first_403_short_retry_second_403_defer_no_long_backoff() -> None:
    sleeps = asyncio.run(_run_block_handler_until_defer(CategoryBlockHandler(_config())))
    assert len(sleeps) == 1
    assert 25.0 <= sleeps[0] <= 35.0
    for long_step in (60, 120, 240):
        assert all(abs(s - long_step) > 5 for s in sleeps)


def test_b_pending_queue_preserved_after_defer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cp_path = Path(tmp) / "checkpoint.json"
        checkpoint = CheckpointState(
            output=str(cp_path.with_suffix(".xlsx")),
            category=CategoryState(
                category_id="100",
                category_name="Test",
                category_path="A > B",
                category_slug="test",
                list_cursor="cursor-2",
                list_complete=True,
            ),
            completed_ids=["1", "2"],
            pending=[
                {"product_id": "3", "name": "P3", "slug": "p3"},
                {"product_id": "4", "name": "P4", "slug": "p4"},
            ],
        )
        checkpoint.save(cp_path)

        global_cp: dict = {"completed_categories": [], "needs_recheck_categories": []}
        defer_category_403(
            global_cp,
            "100",
            retry_count=1,
            pending_count=len(checkpoint.pending),
            recheck_delay_minutes=120,
            now=datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc),
        )

        reloaded = CheckpointState.load(cp_path)
        assert reloaded is not None
        assert reloaded.category is not None
        assert reloaded.category.list_complete is True
        assert reloaded.category.list_cursor == "cursor-2"
        assert reloaded.completed_ids == ["1", "2"]
        assert len(reloaded.pending) == 2
        meta = global_cp["blocked_categories"]["100"]
        assert meta["reason"] == "403"
        assert meta["pending_count"] == 2
        assert "100" in global_cp["needs_recheck_categories"]


def test_c_future_next_retry_at_not_ready_for_recheck() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)
    global_cp = {
        "blocked_categories": {
            "100": {
                "reason": "403",
                "next_retry_at": (now + timedelta(hours=2)).isoformat(),
            }
        }
    }
    assert is_403_recheck_ready(global_cp, "100", now=now) is False


def test_d_past_next_retry_at_ready_for_recheck() -> None:
    now = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)
    global_cp = {
        "blocked_categories": {
            "100": {
                "reason": "403",
                "next_retry_at": (now - timedelta(minutes=5)).isoformat(),
            }
        }
    }
    assert is_403_recheck_ready(global_cp, "100", now=now) is True
    assert category_403_next_retry_at(global_cp, "100") is not None


def test_e_three_consecutive_defers_trigger_session_cooldown() -> None:
    tracker = Session403Tracker(_config(consecutive_403_cooldown_after=3))
    assert tracker.on_defer() is False
    assert tracker.on_defer() is False
    assert tracker.on_defer() is True
    assert tracker.consecutive_403_deferrals == 3


def test_f_success_resets_consecutive_counter() -> None:
    tracker = Session403Tracker(_config(consecutive_403_cooldown_after=3))
    tracker.on_defer()
    tracker.on_defer()
    assert tracker.consecutive_403_deferrals == 2
    tracker.on_success()
    assert tracker.consecutive_403_deferrals == 0
    assert tracker.on_defer() is False


def test_deferred_empty_not_gated_by_403_recheck() -> None:
    global_cp = {"blocked_categories": {}}
    assert is_403_recheck_ready(global_cp, "999") is True


if __name__ == "__main__":
    test_a_first_403_short_retry_second_403_defer_no_long_backoff()
    test_b_pending_queue_preserved_after_defer()
    test_c_future_next_retry_at_not_ready_for_recheck()
    test_d_past_next_retry_at_ready_for_recheck()
    test_e_three_consecutive_defers_trigger_session_cooldown()
    test_f_success_resets_consecutive_counter()
    test_deferred_empty_not_gated_by_403_recheck()
    print("OK")
