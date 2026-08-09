#!/usr/bin/env python3
"""Unit tests for M.Video priority category ordering."""

from __future__ import annotations

from mvideo_parser import (
    Category,
    category_discovery_root,
    log_mvideo_priority_categories,
    sort_mvideo_categories_by_priority,
)


def _cat(seo_id: int, path: str) -> Category:
    slug = path.split(" > ")[-1].lower().replace(" ", "-")
    return Category(seo_id=seo_id, name=path.split(" > ")[-1], slug=slug, path=path)


def test_priority_roots_move_to_top_in_configured_order() -> None:
    categories = [
        _cat(1, "Канцелярия > A > A"),
        _cat(2, "Смартфоны и связь > B > B"),
        _cat(3, "Товары для дома > C > C"),
        _cat(4, "Гаджеты > D > D"),
        _cat(5, "Ноутбуки, планшеты, компьютеры > E > E"),
        _cat(6, "Телевизоры и цифровое ТВ > F > F"),
        _cat(7, "Мелкая кухонная техника > G > G"),
        _cat(8, "Климатическая техника > H > H"),
    ]
    ordered = sort_mvideo_categories_by_priority(categories)
    ordered_ids = [c.seo_id for c in ordered]
    assert ordered_ids.index(2) < ordered_ids.index(4)
    assert ordered_ids.index(4) < ordered_ids.index(5)
    assert ordered_ids.index(5) < ordered_ids.index(6)
    assert ordered_ids.index(6) < ordered_ids.index(7)
    assert ordered_ids.index(7) < ordered_ids.index(8)
    assert ordered_ids.index(8) < ordered_ids.index(1)
    assert ordered_ids.index(1) < ordered_ids.index(3)


def test_original_order_preserved_within_same_root() -> None:
    categories = [
        _cat(10, "Гаджеты > Alpha > Alpha"),
        _cat(11, "Гаджеты > Beta > Beta"),
        _cat(12, "Гаджеты > Gamma > Gamma"),
    ]
    ordered = sort_mvideo_categories_by_priority(categories)
    assert [c.seo_id for c in ordered] == [10, 11, 12]


def test_other_categories_remain_after_priority_without_reordering_among_themselves() -> None:
    categories = [
        _cat(20, "Канцелярия > A > A"),
        _cat(21, "Хобби > B > B"),
        _cat(22, "Смартфоны и связь > C > C"),
        _cat(23, "Продукты > D > D"),
    ]
    ordered = sort_mvideo_categories_by_priority(categories)
    assert [c.seo_id for c in ordered] == [22, 20, 21, 23]


def test_no_categories_removed_and_checkpoint_semantics_unchanged() -> None:
    categories = [
        _cat(30, "Гаджеты > X > X"),
        _cat(31, "Канцелярия > Y > Y"),
    ]
    ordered = sort_mvideo_categories_by_priority(categories)
    assert len(ordered) == len(categories)
    assert {c.seo_id for c in ordered} == {c.seo_id for c in categories}
    assert category_discovery_root(ordered[0]) == "Гаджеты"
    completed = {"31"}
    pending = [c for c in ordered if str(c.seo_id) not in completed]
    assert [c.seo_id for c in pending] == [30]


def test_log_priority_categories_runs() -> None:
    categories = [
        _cat(40, "Гаджеты > A > A"),
        _cat(41, "Канцелярия > B > B"),
    ]
    log_mvideo_priority_categories(categories)


if __name__ == "__main__":
    test_priority_roots_move_to_top_in_configured_order()
    test_original_order_preserved_within_same_root()
    test_other_categories_remain_after_priority_without_reordering_among_themselves()
    test_no_categories_removed_and_checkpoint_semantics_unchanged()
    test_log_priority_categories_runs()
    print("OK")
