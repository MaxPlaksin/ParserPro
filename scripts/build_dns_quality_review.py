#!/usr/bin/env python3
"""Read-only DNS product quality QA report (HTML)."""

from __future__ import annotations

import html
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
OUTPUT_PATH = PROJECT_ROOT / "output" / "dns_quality_review.html"

SAMPLE_SIZE = 30
MAX_PER_CATEGORY = 2
RANDOM_SEED = 42
MAX_IMAGES = 6

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass
class ImageRow:
    db_path: str
    local_abs: Path | None
    html_src: str | None
    missing: bool


@dataclass
class ProductRow:
    product_id: int
    source_id: str
    sku: str | None
    name: str
    url: str
    category_path: str
    price: float | None
    description: str | None
    description_detailed: str | None
    description_full: str | None
    raw_full_text: str | None
    characteristics: dict[str, str]
    images: list[ImageRow] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(1 for v in self.characteristics.values() if str(v).strip())

    @property
    def has_price(self) -> bool:
        return self.price is not None

    @property
    def has_description(self) -> bool:
        for value in (self.description, self.description_detailed, self.description_full):
            if value and value.strip():
                return True
        return False

    @property
    def local_image_count(self) -> int:
        return sum(1 for img in self.images if not img.missing)

    @property
    def has_raw(self) -> bool:
        return bool(self.raw_full_text and self.raw_full_text.strip())


def connect_readonly() -> sqlite3.Connection:
    uri = f"file:{DB_PATH.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ") + " ₽"
    return f"{value:,.2f}".replace(",", " ") + " ₽"


def parse_characteristics(raw: str | None) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in payload.items():
        name = str(key).strip()
        if not name:
            continue
        if value is None:
            text = ""
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value).strip()
        result[name] = text
    return result


def resolve_local_path(db_path: str) -> Path | None:
    if not db_path:
        return None
    candidate = PROJECT_ROOT / db_path.replace("\\", "/")
    return candidate if candidate.is_file() else None


def image_html_src(local_abs: Path | None) -> str | None:
    if local_abs is None:
        return None
    try:
        rel = local_abs.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return "../" + rel.as_posix()


def load_images(conn: sqlite3.Connection, product_id: int) -> list[ImageRow]:
    rows = conn.execute(
        """
        SELECT path, source_url, position
        FROM product_images
        WHERE product_id = ?
        ORDER BY position, id
        LIMIT ?
        """,
        (product_id, MAX_IMAGES),
    ).fetchall()

    images: list[ImageRow] = []
    for row in rows:
        db_path = (row["path"] or "").replace("\\", "/")
        local_abs = resolve_local_path(db_path)
        missing = local_abs is None
        images.append(
            ImageRow(
                db_path=db_path,
                local_abs=local_abs,
                html_src=image_html_src(local_abs),
                missing=missing,
            )
        )
    return images


def load_product(conn: sqlite3.Connection, row: sqlite3.Row) -> ProductRow:
    characteristics = parse_characteristics(row["characteristics_json"])
    return ProductRow(
        product_id=row["id"],
        source_id=row["source_id"],
        sku=row["sku"],
        name=row["name"],
        url=row["url"] or "",
        category_path=row["category_path"] or row["category_name"] or "—",
        price=row["price"],
        description=row["description"],
        description_detailed=row["description_detailed"],
        description_full=row["description_full"],
        raw_full_text=row["raw_full_text"],
        characteristics=characteristics,
        images=load_images(conn, row["id"]),
    )


def load_dns_candidates(conn: sqlite3.Connection) -> list[ProductRow]:
    rows = conn.execute(
        """
        SELECT
            p.id,
            p.source_id,
            p.sku,
            p.name,
            p.url,
            p.price,
            p.description,
            p.description_detailed,
            p.description_full,
            p.raw_full_text,
            c.path AS category_path,
            c.name AS category_name,
            pc.characteristics_json
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = 'dns'
          AND p.name IS NOT NULL
          AND TRIM(p.name) != ''
        ORDER BY p.id
        """
    ).fetchall()
    return [load_product(conn, row) for row in rows]


def select_products(candidates: list[ProductRow]) -> list[ProductRow]:
    import random

    by_category: dict[str, list[ProductRow]] = {}
    for product in candidates:
        by_category.setdefault(product.category_path, []).append(product)

    for category_path in by_category:
        pool = by_category[category_path]
        pool.sort(
            key=lambda p: (
                -p.local_image_count,
                -p.char_count,
                p.source_id,
            )
        )
        by_category[category_path] = pool

    categories = sorted(by_category.keys())
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(categories)

    selected: list[ProductRow] = []
    per_category: dict[str, int] = {cat: 0 for cat in categories}

    while len(selected) < SAMPLE_SIZE:
        progressed = False
        for category_path in categories:
            if per_category[category_path] >= MAX_PER_CATEGORY:
                continue
            idx = per_category[category_path]
            pool = by_category[category_path]
            if idx >= len(pool):
                continue
            selected.append(pool[idx])
            per_category[category_path] += 1
            progressed = True
            if len(selected) >= SAMPLE_SIZE:
                break
        if not progressed:
            break

    selected.sort(key=lambda p: (p.category_path.lower(), p.source_id))
    return selected


def describe_field(label: str, value: str | None) -> str:
    if value and value.strip():
        return (
            f"<section class='desc-block'>"
            f"<h4>{esc(label)} <span class='ok'>present</span></h4>"
            f"<pre>{esc(value.strip())}</pre>"
            f"</section>"
        )
    return (
        f"<section class='desc-block missing'>"
        f"<h4>{esc(label)} <span class='empty'>empty</span></h4>"
        f"</section>"
    )


def render_indicators(product: ProductRow) -> str:
    price = "PRICE OK" if product.has_price else "PRICE EMPTY"
    desc = "DESCRIPTION OK" if product.has_description else "DESCRIPTION EMPTY"
    raw = "RAW OK" if product.has_raw else "RAW EMPTY"
    price_cls = "ok" if product.has_price else "empty"
    desc_cls = "ok" if product.has_description else "empty"
    raw_cls = "ok" if product.has_raw else "empty"
    return (
        "<div class='indicators'>"
        f"<span class='badge {price_cls}'>{price}</span>"
        f"<span class='badge {desc_cls}'>{desc}</span>"
        f"<span class='badge neutral'>CHARACTERISTICS {product.char_count}</span>"
        f"<span class='badge neutral'>IMAGES {product.local_image_count}</span>"
        f"<span class='badge {raw_cls}'>{raw}</span>"
        "</div>"
    )


def render_images(product: ProductRow) -> str:
    if not product.images:
        return "<p class='missing-image'>MISSING IMAGE — no image records in DB</p>"
    blocks: list[str] = []
    for image in product.images:
        if image.missing or not image.html_src:
            blocks.append(
                "<figure class='missing-image'>"
                f"<div>MISSING IMAGE</div>"
                f"<figcaption>{esc(image.db_path or '—')}</figcaption>"
                "</figure>"
            )
        else:
            blocks.append(
                "<figure>"
                f"<img src='{esc(image.html_src)}' alt='{esc(product.name)}'>"
                f"<figcaption>{esc(image.db_path)}</figcaption>"
                "</figure>"
            )
    return "<div class='images'>" + "".join(blocks) + "</div>"


def render_characteristics(product: ProductRow) -> str:
    if not product.characteristics:
        return "<p class='empty-note'>— no characteristics —</p>"
    rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{esc(value)}</td></tr>"
        for name, value in sorted(product.characteristics.items(), key=lambda x: x[0].lower())
    )
    return (
        f"<p>Non-empty fields: <strong>{product.char_count}</strong></p>"
        f"<table class='chars'><thead><tr><th>Key</th><th>Value</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def render_product(product: ProductRow, index: int) -> str:
    raw_block = ""
    if product.has_raw:
        raw_block = (
            "<details class='raw-details'>"
            "<summary>raw_full_text (collapsible)</summary>"
            f"<pre>{esc(product.raw_full_text or '')}</pre>"
            "</details>"
        )
    else:
        raw_block = "<p class='empty-note'>raw_full_text: empty</p>"

    return f"""
<article class="product" id="product-{index}">
  <header>
    <h2>#{index}. {esc(product.name)}</h2>
    {render_indicators(product)}
  </header>
  <dl class="meta">
    <dt>category_path</dt><dd>{esc(product.category_path)}</dd>
    <dt>source_id</dt><dd>{esc(product.source_id)}</dd>
    <dt>SKU</dt><dd>{esc(product.sku or '—')}</dd>
    <dt>URL</dt><dd><a href="{esc(product.url)}">{esc(product.url)}</a></dd>
    <dt>price</dt><dd>{esc(fmt_price(product.price))}</dd>
  </dl>
  <h3>Descriptions</h3>
  {describe_field('description', product.description)}
  {describe_field('description_detailed', product.description_detailed)}
  {describe_field('description_full', product.description_full)}
  <h3>Structured characteristics</h3>
  {render_characteristics(product)}
  <h3>Local images (first {MAX_IMAGES})</h3>
  {render_images(product)}
  <h3>raw_full_text</h3>
  {raw_block}
</article>
"""


def build_summary(products: list[ProductRow]) -> str:
    char_counts = [p.char_count for p in products]
    image_counts = [p.local_image_count for p in products]
    with_desc = sum(1 for p in products if p.has_description)
    with_photos = sum(1 for p in products if p.local_image_count > 0)
    with_price = sum(1 for p in products if p.has_price)
    categories = sorted({p.category_path for p in products})

    def stat(values: list[int], fn_name: str) -> str:
        if not values:
            return "—"
        if fn_name == "mean":
            return f"{statistics.mean(values):.1f}"
        if fn_name == "median":
            return str(int(statistics.median(values)))
        if fn_name == "min":
            return str(min(values))
        if fn_name == "max":
            return str(max(values))
        return "—"

    cat_items = "".join(f"<li>{esc(c)}</li>" for c in categories)
    return f"""
<section class="summary">
  <h2>Summary — {len(products)} DNS products (seed={RANDOM_SEED})</h2>
  <div class="summary-grid">
    <div><strong>Characteristics</strong><br>
      mean {stat(char_counts, 'mean')} ·
      median {stat(char_counts, 'median')} ·
      min {stat(char_counts, 'min')} ·
      max {stat(char_counts, 'max')}
    </div>
    <div><strong>With description</strong><br>{with_desc} / {len(products)}</div>
    <div><strong>With local photos</strong><br>{with_photos} / {len(products)}</div>
    <div><strong>Avg photos</strong><br>{stat(image_counts, 'mean')}</div>
    <div><strong>With price</strong><br>{with_price} / {len(products)}</div>
    <div><strong>Categories represented</strong><br>{len(categories)}</div>
  </div>
  <h3>Category list</h3>
  <ul class="category-list">{cat_items}</ul>
</section>
"""


def page_styles() -> str:
    return """
    body { font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1a1a1a; background: #f4f5f7; }
    h1 { margin-bottom: 0.2em; }
    h2, h3, h4 { margin-top: 1em; }
    .note { color: #555; font-size: 14px; }
    .summary { background: #fff; border: 1px solid #d5d7de; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 12px 0; }
    .summary-grid div { background: #f8f9fb; border: 1px solid #ececf1; padding: 10px 12px; border-radius: 6px; }
    .category-list { columns: 2; margin: 0; padding-left: 20px; font-size: 13px; }
    .product { background: #fff; border: 1px solid #d5d7de; border-radius: 8px; padding: 16px 20px; margin: 24px 0; }
    .meta { display: grid; grid-template-columns: 140px 1fr; gap: 6px 12px; font-size: 14px; }
    .meta dt { font-weight: 600; color: #444; }
    .meta dd { margin: 0; word-break: break-word; }
    .indicators { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
    .badge { font-size: 12px; font-weight: 600; padding: 4px 8px; border-radius: 4px; border: 1px solid #ccc; }
    .badge.ok { background: #e8f7ea; border-color: #8ecf98; color: #1f6b2c; }
    .badge.empty { background: #fff0f0; border-color: #e59a9a; color: #8a2020; }
    .badge.neutral { background: #eef2ff; border-color: #a8b6ef; color: #2a3d8f; }
    .desc-block pre, .raw-details pre { white-space: pre-wrap; word-break: break-word; background: #fafafa; padding: 10px; border: 1px solid #ececf1; font-size: 13px; }
    .desc-block.missing, .empty-note { color: #888; font-style: italic; }
    .ok { color: #1f6b2c; font-size: 12px; font-weight: 600; }
    .empty { color: #8a2020; font-size: 12px; font-weight: 600; }
    table.chars { border-collapse: collapse; width: 100%; font-size: 13px; }
    table.chars th, table.chars td { border: 1px solid #d9d9de; padding: 6px 8px; vertical-align: top; }
    table.chars th { background: #ececf1; text-align: left; }
    .images { display: flex; flex-wrap: wrap; gap: 12px; }
    .images img { max-width: 180px; max-height: 180px; object-fit: contain; border: 1px solid #ddd; background: #fff; }
    figure { margin: 0; max-width: 200px; font-size: 11px; color: #555; }
    .missing-image { border: 2px dashed #c44; background: #fff5f5; color: #922; padding: 12px; min-width: 160px; min-height: 80px; display: flex; flex-direction: column; justify-content: center; }
    .raw-details summary { cursor: pointer; font-weight: 600; margin-bottom: 8px; }
"""


def render_html(products: list[ProductRow]) -> str:
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    cards = "".join(render_product(p, i + 1) for i, p in enumerate(products))
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DNS Quality Review — {len(products)} products</title>
  <style>{page_styles()}</style>
</head>
<body>
  <h1>DNS Quality Review (read-only QA)</h1>
  <p class="note">Generated {esc(generated)} · DB: {esc(DB_PATH)} · mode=ro · seed={RANDOM_SEED}</p>
  {build_summary(products)}
  {cards}
</body>
</html>
"""


def main() -> int:
    if not DB_PATH.is_file():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = connect_readonly()
    try:
        candidates = load_dns_candidates(conn)
        products = select_products(candidates)
    finally:
        conn.close()

    if len(products) < SAMPLE_SIZE:
        print(
            f"Warning: selected {len(products)} products (target {SAMPLE_SIZE})",
            file=sys.stderr,
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render_html(products), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Products: {len(products)}")
    print(f"Categories: {len({p.category_path for p in products})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
