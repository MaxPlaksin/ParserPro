"""Canonical paths for parsed assets (single source of truth)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
STORAGE_IMAGES_ROOT = PROJECT_ROOT / "storage" / "images"

# storage/images/{source}/{source_id}/001.jpg
IMAGE_PATH_RE = re.compile(
    r"^storage/images/(?P<source>[^/]+)/(?P<source_id>[^/]+)/(?P<filename>[^/]+)$"
)
# legacy: storage/dns/{category_id}/{source_id}/001.jpg
LEGACY_STORAGE_RE = re.compile(
    r"^storage/(?P<source>[^/]+)/(?P<category>[^/]+)/(?P<source_id>[^/]+)/(?P<filename>[^/]+)$"
)
# legacy: output/.../images/{source_id}/001.jpg
LEGACY_OUTPUT_RE = re.compile(
    r"^output/(?P<rest>.+)/images/(?P<source_id>[^/]+)/(?P<filename>[^/]+)$"
)


def safe_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    return cleaned or "unknown"


def canonical_image_dir(source: str, source_id: str) -> Path:
    return STORAGE_IMAGES_ROOT / source / safe_part(source_id)


def image_storage_relpath(source: str, source_id: str, filename: str) -> str:
    return f"storage/images/{source}/{safe_part(source_id)}/{filename}".replace("\\", "/")


def normalize_image_db_path(path: str, *, default_source: str = "", default_source_id: str = "") -> str:
    """Ensure relative canonical path storage/images/{source}/{id}/file."""
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return ""

    if raw.startswith(str(PROJECT_ROOT)):
        raw = Path(raw).relative_to(PROJECT_ROOT).as_posix()

    match = IMAGE_PATH_RE.match(raw)
    if match:
        return image_storage_relpath(match.group("source"), match.group("source_id"), match.group("filename"))

    match = LEGACY_STORAGE_RE.match(raw)
    if match:
        return image_storage_relpath(match.group("source"), match.group("source_id"), match.group("filename"))

    match = LEGACY_OUTPUT_RE.match(raw)
    if match:
        sid = match.group("source_id")
        # guess source from path
        source = "mvideo" if "mvideo" in match.group("rest") else "dns"
        return image_storage_relpath(source, sid, match.group("filename"))

    if raw.startswith("images/") and default_source and default_source_id:
        return image_storage_relpath(default_source, default_source_id, Path(raw).name)

    filename = Path(raw).name
    if default_source and default_source_id and filename:
        return image_storage_relpath(default_source, default_source_id, filename)

    return raw


def materialize_file_to_storage(source: str, source_id: str, src: Path) -> str:
    """Copy file into canonical storage if missing; return relative DB path."""
    dest_dir = canonical_image_dir(source, source_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if src.is_file() and src.exists():
        if not dest.exists() or dest.stat().st_size == 0:
            shutil.copy2(src, dest)
    return image_storage_relpath(source, source_id, src.name)


def migrate_legacy_file(source: str, source_id: str, legacy_relpath: str) -> str | None:
    """Move/copy legacy path into canonical storage. Returns new relpath or None."""
    canonical = normalize_image_db_path(legacy_relpath, default_source=source, default_source_id=source_id)
    if not canonical:
        return None
    legacy_abs = PROJECT_ROOT / legacy_relpath.replace("\\", "/")
    canonical_abs = PROJECT_ROOT / canonical
    if legacy_abs.is_file() and legacy_abs.resolve() != canonical_abs.resolve():
        canonical_abs.parent.mkdir(parents=True, exist_ok=True)
        if not canonical_abs.exists() or canonical_abs.stat().st_size == 0:
            shutil.copy2(legacy_abs, canonical_abs)
    elif canonical_abs.is_file():
        return canonical
    else:
        return None
    return canonical if canonical_abs.is_file() else None
