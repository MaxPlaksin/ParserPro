"""Filter review noise from product text — keep full marketing/spec content."""

from __future__ import annotations

import re
from html import unescape

REVIEW_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Отзывы", re.I),
    re.compile(r"^Отзыв покупателя", re.I),
    re.compile(r"^Мнение покупателя", re.I),
    re.compile(r"отзыв(?:ов|ы)?\s*(?:покупател|пользовател)", re.I),
    re.compile(r"^Читать все отзывы", re.I),
    re.compile(r"^Написать отзыв", re.I),
)

UI_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Свернуть(?:\s|$)", re.I),
    re.compile(r"^Развернуть(?:\s|$)", re.I),
)

HTML_REVIEW_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<section[^>]*class=\"[^\"]*review[^\"]*\"[^>]*>.*?</section>", re.I | re.S),
    re.compile(r"<div[^>]*class=\"[^\"]*(?:review|opinion)[^\"]*\"[^>]*>.*?</div>", re.I | re.S),
    re.compile(r"<[^>]*(?:отзыв|opinion)[^>]*>.*?</[^>]+>", re.I | re.S),
)

SKIP_PRODUCT_PATH_PARTS: tuple[str, ...] = (
    "/opinion",
    "/opinion/",
    "/compare/",
    "/reviews/",
    "/review/",
    "/questions/",
    "/question/",
    "/discussion/",
)


def is_skipped_product_path(path: str) -> bool:
    lowered = path.lower()
    if "?" in path:
        return True
    return any(part in lowered for part in SKIP_PRODUCT_PATH_PARTS)


def clean_review_noise(text: str) -> str:
    """Remove review texts and UI chrome; keep price history, specs, reliability blocks."""
    if not text:
        return ""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if any(pattern.search(line) for pattern in REVIEW_LINE_PATTERNS):
            continue
        if any(pattern.search(line) for pattern in UI_LINE_PATTERNS):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def clean_plain_description(text: str) -> str:
    return clean_review_noise(text)


def clean_html_description(html: str) -> str:
    if not html:
        return ""
    text = html
    for pattern in HTML_REVIEW_PATTERNS:
        text = pattern.sub("", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    return clean_review_noise(text)


def format_specs_block(grouped_specs: list[list[str]]) -> str:
    if not grouped_specs:
        return ""
    lines: list[str] = ["Характеристики"]
    current_group = ""
    for item in grouped_specs:
        if len(item) == 3:
            group, name, value = item
        elif len(item) == 2:
            group, name, value = "", item[0], item[1]
        else:
            continue
        group = (group or "").strip()
        name = (name or "").strip()
        value = (value or "").strip()
        if not name or not value:
            continue
        if group and group != current_group:
            lines.append(group)
            current_group = group
        lines.append(f"{name}: {value}")
    return "\n".join(lines) if len(lines) > 1 else ""


def build_full_description(
    detailed: str,
    grouped_specs: list[list[str]] | None,
    *,
    short: str = "",
) -> str:
    parts: list[str] = []
    detailed = (detailed or "").strip()
    short = (short or "").strip()
    if detailed:
        parts.append(detailed)
    elif short:
        parts.append(short)
    spec_block = format_specs_block(grouped_specs or [])
    if spec_block and spec_block not in detailed:
        parts.append(spec_block)
    return clean_review_noise("\n\n".join(parts))


def is_review_media_url(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in ("review", "opinion", "отзыв", "user-review", "video-review"))
