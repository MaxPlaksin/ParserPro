#!/usr/bin/env python3
"""Unit checks for DNS image URL normalization."""

from dns_parser import image_dedupe_key, normalize_image

HASH = "a" * 64
THUMB_URL = f"https://c.dns-shop.ru/thumb/st1/fit/500/500/{HASH}.webp"
FIT_URL = f"https://c.dns-shop.ru/fit/1200/1200/{HASH}.webp"


def test_normalize_image_preserves_thumb_url() -> None:
    assert normalize_image(THUMB_URL) == THUMB_URL


def test_normalize_image_preserves_fit_url() -> None:
    assert normalize_image(FIT_URL) == FIT_URL


def test_image_dedupe_key_uses_hash() -> None:
    assert image_dedupe_key(THUMB_URL) == HASH
    assert image_dedupe_key(FIT_URL) == HASH


if __name__ == "__main__":
    test_normalize_image_preserves_thumb_url()
    test_normalize_image_preserves_fit_url()
    test_image_dedupe_key_uses_hash()
    print("OK")
