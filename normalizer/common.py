"""Shared normalizer helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from models.product_record import ProductImage
from storage_paths import (
    image_storage_relpath,
    materialize_file_to_storage,
    normalize_image_db_path,
)

import re

GIF_EXT_RE = re.compile(r"\.gif(?:$|\?)", re.I)


def extract_spec_fields(row: dict[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    for key, value in row.items():
        if not key.startswith("spec__"):
            continue
        name = key.removeprefix("spec__").strip()
        if name and value not in (None, ""):
            specs[name] = str(value)
    if not specs and row.get("characteristics"):
        for part in str(row["characteristics"]).split(";"):
            if ":" in part:
                name, val = part.split(":", 1)
                specs[name.strip()] = val.strip()
    return specs


def split_pipe(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split("|") if part.strip()]


def media_kind(url: str, path: str = "") -> str:
    candidate = (url or path).lower()
    if GIF_EXT_RE.search(candidate):
        return "gif"
    if any(x in candidate for x in (".mp4", ".webm", ".mov")):
        return "video"
    return "image"


def resolve_local_image_path(
    raw_path: str,
    *,
    source: str,
    source_id: str,
    category_dir: Path | None = None,
) -> ProductImage | None:
    raw_path = raw_path.strip()
    if not raw_path:
        return None

    canonical = normalize_image_db_path(raw_path, default_source=source, default_source_id=source_id)
    if canonical.startswith("storage/images/"):
        from storage_paths import PROJECT_ROOT

        abs_canonical = PROJECT_ROOT / canonical
        if not abs_canonical.is_file():
            candidates: list[Path] = []
            if raw_path.startswith("output/") or raw_path.startswith("storage/"):
                candidates.append(PROJECT_ROOT / raw_path.replace("\\", "/"))
            if category_dir is not None and not raw_path.startswith(("output/", "storage/")):
                candidates.append(category_dir / raw_path)
            for candidate in candidates:
                if candidate.is_file():
                    canonical = materialize_file_to_storage(source, source_id, candidate)
                    break

    if not canonical.startswith("storage/images/"):
        return None

    return ProductImage(
        path=canonical,
        source_url="",
        kind=media_kind("", canonical),
        position=0,
    )


def build_images_from_row(
    row: dict[str, Any],
    *,
    source: str,
    source_id: str,
    category_source_id: str = "",
    category_dir: Path | None = None,
) -> list[ProductImage]:
    del category_source_id  # images keyed by source + source_id only
    images: list[ProductImage] = []
    local_paths = split_pipe(row.get("local_images"))
    remote_urls = split_pipe(row.get("photo_urls") or row.get("photos"))
    gif_urls = split_pipe(row.get("gif_urls"))

    if not local_paths and remote_urls:
        for index, url in enumerate(remote_urls, 1):
            ext = Path(urlparse(url).path).suffix or ".jpg"
            local_paths.append(image_storage_relpath(source, source_id, f"{index:03d}{ext}"))

    for index, local_path in enumerate(local_paths, 1):
        image = resolve_local_image_path(
            local_path,
            source=source,
            source_id=source_id,
            category_dir=category_dir,
        )
        if image is None:
            continue
        image.position = index
        if index - 1 < len(remote_urls):
            image.source_url = remote_urls[index - 1]
            image.kind = media_kind(remote_urls[index - 1], image.path)
        images.append(image)

    start = len(images)
    for offset, url in enumerate(gif_urls, 1):
        parsed = urlparse(url)
        filename = Path(parsed.path).name or f"gif_{start + offset:03d}.gif"
        rel_path = image_storage_relpath(source, source_id, filename)
        images.append(
            ProductImage(
                path=rel_path,
                source_url=url,
                kind="gif",
                position=start + offset,
            )
        )
    return images
