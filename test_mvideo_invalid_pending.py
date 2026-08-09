#!/usr/bin/env python3
"""Unit tests for invalid/empty ProductListItem handling — без mvideo.ru."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from mvideo_parser import (
    BlockedError,
    CategoryState,
    CheckpointState,
    MvideoScraper,
    ProductListItem,
    Session403Tracker,
    MvideoFastDeferConfig,
    is_valid_product_id,
    parse_list_product_item,
)


def test_a_empty_product_id_from_list_response_not_in_pending() -> None:
    assert parse_list_product_item({"productId": "", "name": "X"}, "100") is None
    assert parse_list_product_item({"name": "No id"}, "100") is None
    assert parse_list_product_item({"productId": None, "name": "Y"}, "100") is None

    valid = parse_list_product_item(
        {"productId": "12345", "name": "Good", "slug": "good", "price": {}, "rating": {}},
        "100",
    )
    assert valid is not None
    assert valid.product_id == "12345"


def _make_scraper(pending: list[dict]) -> tuple[MvideoScraper, list[str], MagicMock]:
    checkpoint = CheckpointState(
        category=CategoryState(
            category_id="100",
            category_name="Test",
            category_path="Test",
            category_slug="test",
            list_complete=True,
        ),
        pending=pending,
    )
    list_client = MagicMock()
    worker_pool = MagicMock()
    worker_pool._workers = 2
    worker_pool.get_product_details = AsyncMock(return_value={"properties": []})
    scraper = MvideoScraper(
        list_client,
        worker_pool,
        checkpoint,
        fetch_details=True,
        download_images_flag=False,
    )
    fetch_calls: list[str] = []

    async def fake_fetch(item: ProductListItem, stagger_index: int = 0) -> dict:
        fetch_calls.append(item.product_id)
        return {"product_id": item.product_id, "name": item.name, "url": "http://x"}

    scraper._fetch_and_build_row = fake_fetch  # type: ignore[method-assign]
    scraper._finalize_row = AsyncMock()  # type: ignore[method-assign]
    return scraper, fetch_calls, worker_pool


def test_b_existing_empty_pending_skipped_without_http() -> None:
    scraper, fetch_calls, worker_pool = _make_scraper(
        [
            {
                "product_id": "",
                "name": "",
                "slug": "",
                "sale_price": None,
                "base_price": None,
                "discount": None,
                "brand": None,
                "rating_count": None,
                "rating_star": None,
                "status": None,
                "images": [],
                "category_id": "100",
            }
        ]
    )
    asyncio.run(scraper._process_pending_details(None, None, None))
    assert fetch_calls == []
    worker_pool.get_product_details.assert_not_called()
    assert scraper.checkpoint.pending == []


def test_c_after_empty_next_valid_pending_processed() -> None:
    scraper, fetch_calls, _ = _make_scraper(
        [
            {
                "product_id": "",
                "name": "",
                "slug": "",
                "sale_price": None,
                "base_price": None,
                "discount": None,
                "brand": None,
                "rating_count": None,
                "rating_star": None,
                "status": None,
                "images": [],
                "category_id": "100",
            },
            {
                "product_id": "555",
                "name": "Valid",
                "slug": "valid",
                "sale_price": None,
                "base_price": None,
                "discount": None,
                "brand": None,
                "rating_count": None,
                "rating_star": None,
                "status": None,
                "images": [],
                "category_id": "100",
            },
        ]
    )
    asyncio.run(scraper._process_pending_details(None, None, None))
    assert fetch_calls == ["555"]
    assert scraper.checkpoint.pending == []
    assert scraper.processed == 1


def test_d_empty_item_no_blocked_error_no_403_counter() -> None:
    scraper, fetch_calls, _ = _make_scraper(
        [
            {
                "product_id": "",
                "name": "",
                "slug": "",
                "sale_price": None,
                "base_price": None,
                "discount": None,
                "brand": None,
                "rating_count": None,
                "rating_star": None,
                "status": None,
                "images": [],
                "category_id": "100",
            }
        ]
    )
    tracker = Session403Tracker(MvideoFastDeferConfig())
    asyncio.run(scraper._process_pending_details(None, None, None))
    assert fetch_calls == []
    assert tracker.on_defer() is False


async def _fetch_raises_blocked(item: ProductListItem, stagger_index: int = 0) -> dict:
    raise BlockedError(f"GET /bff/product-details?productId={item.product_id} -> HTTP 403")


def test_e_valid_pending_keeps_fetch_behavior() -> None:
    scraper, fetch_calls, worker_pool = _make_scraper(
        [
            {
                "product_id": "777",
                "name": "Valid",
                "slug": "valid",
                "sale_price": None,
                "base_price": None,
                "discount": None,
                "brand": None,
                "rating_count": None,
                "rating_star": None,
                "status": None,
                "images": [],
                "category_id": "100",
            }
        ]
    )
    asyncio.run(scraper._process_pending_details(None, None, None))
    assert fetch_calls == ["777"]
    assert scraper.processed == 1


def test_valid_product_id_helper() -> None:
    assert is_valid_product_id("123") is True
    assert is_valid_product_id("  ") is False
    assert is_valid_product_id("") is False
    assert is_valid_product_id(None) is False


if __name__ == "__main__":
    test_a_empty_product_id_from_list_response_not_in_pending()
    test_b_existing_empty_pending_skipped_without_http()
    test_c_after_empty_next_valid_pending_processed()
    test_d_empty_item_no_blocked_error_no_403_counter()
    test_e_valid_pending_keeps_fetch_behavior()
    test_valid_product_id_helper()
    print("OK")
