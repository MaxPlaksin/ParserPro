#!/usr/bin/env python3
"""Build a static HTML sample review of collected products (read-only DB)."""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
OUTPUT_DIR = PROJECT_ROOT / "output" / "sample_review_20"
ASSETS_DIR = OUTPUT_DIR / "assets"
INDEX_PATH = OUTPUT_DIR / "index.html"
DEMO_PATH = OUTPUT_DIR / "parser_demo.html"

SAMPLE_PER_SOURCE = 10
MAX_IMAGES_DISPLAY = 5

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass
class ImageInfo:
    db_path: str
    source_url: str
    local_abs: Path | None
    asset_relpath: str | None
    status: str


@dataclass
class ProductSample:
    source: str
    product_id: int
    source_id: str
    sku: str | None
    name: str
    category_name: str | None
    category_path: str | None
    price: float | None
    description: str | None
    raw_full_text_len: int
    characteristics: dict[str, str]
    char_fields: int
    image_urls: list[str]
    local_images: list[str]
    images: list[ImageInfo] = field(default_factory=list)

    @property
    def description_len(self) -> int:
        return len(self.description or "")

    @property
    def local_image_count(self) -> int:
        return sum(1 for img in self.images if img.local_abs is not None)

    @property
    def image_url_count(self) -> int:
        return len(self.image_urls)


def connect_readonly() -> sqlite3.Connection:
    uri = f"file:{DB_PATH.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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


def load_candidates(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            p.id,
            p.source,
            p.source_id,
            p.sku,
            p.name,
            p.price,
            p.description,
            p.raw_full_text,
            p.category_id,
            c.name AS category_name,
            c.path AS category_path,
            pc.characteristics_json
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        INNER JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = ?
          AND p.name IS NOT NULL
          AND TRIM(p.name) != ''
          AND pc.characteristics_json IS NOT NULL
          AND TRIM(pc.characteristics_json) != ''
          AND pc.characteristics_json != '{}'
          AND EXISTS (
              SELECT 1 FROM product_images pi WHERE pi.product_id = p.id
          )
        ORDER BY p.id
        """,
        (source,),
    ).fetchall()


def load_images(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT path, source_url, kind, position
        FROM product_images
        WHERE product_id = ?
        ORDER BY position, id
        """,
        (product_id,),
    ).fetchall()


def score_candidate(row: sqlite3.Row, local_image_count: int) -> tuple[int, int, int]:
    chars = parse_characteristics(row["characteristics_json"])
    desc_len = len(row["description"] or "")
    return (local_image_count, len(chars), desc_len)


def select_products(conn: sqlite3.Connection, source: str, limit: int) -> list[ProductSample]:
    candidates: list[tuple[tuple[int, int, int], sqlite3.Row, int]] = []

    for row in load_candidates(conn, source):
        image_rows = load_images(conn, row["id"])
        local_count = 0
        for image_row in image_rows:
            rel = (image_row["path"] or "").replace("\\", "/")
            if rel and (PROJECT_ROOT / rel).is_file():
                local_count += 1
        if local_count == 0:
            continue
        candidates.append((score_candidate(row, local_count), row, local_count))

    candidates.sort(key=lambda item: item[0], reverse=True)

    by_category: dict[int | None, list[tuple[tuple[int, int, int], sqlite3.Row, int]]] = {}
    for scored in candidates:
        category_id = scored[1]["category_id"]
        by_category.setdefault(category_id, []).append(scored)

    selected_rows: list[sqlite3.Row] = []
    used_ids: set[int] = set()

    category_keys = sorted(
        by_category.keys(),
        key=lambda cid: by_category[cid][0][0],
        reverse=True,
    )
    for category_id in category_keys:
        if len(selected_rows) >= limit:
            break
        for scored in by_category[category_id]:
            row = scored[1]
            if row["id"] in used_ids:
                continue
            selected_rows.append(row)
            used_ids.add(row["id"])
            break

    if len(selected_rows) < limit:
        for scored in candidates:
            row = scored[1]
            if row["id"] in used_ids:
                continue
            selected_rows.append(row)
            used_ids.add(row["id"])
            if len(selected_rows) >= limit:
                break

    products: list[ProductSample] = []
    for row in selected_rows[:limit]:
        products.append(build_product_sample(conn, row))
    return products


def build_product_sample(conn: sqlite3.Connection, row: sqlite3.Row) -> ProductSample:
    characteristics = parse_characteristics(row["characteristics_json"])
    image_rows = load_images(conn, row["id"])

    image_urls: list[str] = []
    local_images: list[str] = []
    images: list[ImageInfo] = []

    asset_dir = ASSETS_DIR / f"{row['source']}_{row['source_id']}"

    for image_row in image_rows[:MAX_IMAGES_DISPLAY]:
        db_path = (image_row["path"] or "").replace("\\", "/")
        source_url = (image_row["source_url"] or "").strip()
        if source_url:
            image_urls.append(source_url)

        local_abs: Path | None = None
        asset_relpath: str | None = None
        status = "no path in DB"

        if db_path:
            candidate = PROJECT_ROOT / db_path
            if candidate.is_file():
                local_abs = candidate
                local_images.append(db_path)
                asset_dir.mkdir(parents=True, exist_ok=True)
                dest = asset_dir / candidate.name
                if not dest.exists():
                    shutil.copy2(candidate, dest)
                asset_relpath = dest.relative_to(OUTPUT_DIR).as_posix()
                status = "local file ok"
            elif source_url:
                status = "URL найден, локальный файл отсутствует"
            else:
                status = "path in DB, local file missing"
        elif source_url:
            status = "URL найден, локальный файл отсутствует"

        images.append(
            ImageInfo(
                db_path=db_path,
                source_url=source_url,
                local_abs=local_abs,
                asset_relpath=asset_relpath,
                status=status,
            )
        )

    return ProductSample(
        source=row["source"],
        product_id=row["id"],
        source_id=row["source_id"],
        sku=row["sku"],
        name=row["name"],
        category_name=row["category_name"],
        category_path=row["category_path"],
        price=row["price"],
        description=row["description"],
        raw_full_text_len=len(row["raw_full_text"] or ""),
        characteristics=characteristics,
        char_fields=len(characteristics),
        image_urls=image_urls,
        local_images=local_images,
        images=images,
    )


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ") + " ₽"
    return f"{value:,.2f}".replace(",", " ") + " ₽"


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def image_data_uri(path: Path) -> str | None:
    if not path.is_file():
        return None
    mime = MIME_BY_SUFFIX.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def render_product_images(product: ProductSample, *, embed: bool) -> str:
    image_blocks: list[str] = []
    for image in product.images:
        if embed and image.local_abs is not None:
            data_uri = image_data_uri(image.local_abs)
            if data_uri:
                image_blocks.append(
                    f"<figure><img src='{data_uri}' alt='{esc(product.name)}'>"
                    f"<figcaption>{esc(image.db_path)}</figcaption></figure>"
                )
                continue
        if not embed and image.asset_relpath:
            image_blocks.append(
                f"<figure><img src='{esc(image.asset_relpath)}' alt='{esc(product.name)}'>"
                f"<figcaption>{esc(image.db_path)}</figcaption></figure>"
            )
            continue

        note = esc(image.status)
        if image.source_url:
            if embed:
                note += f"<br>{esc(image.source_url)}"
            else:
                note += f"<br><a href='{esc(image.source_url)}'>{esc(image.source_url)}</a>"
        image_blocks.append(f"<div class='missing-image'>{note}</div>")

    if not image_blocks:
        image_blocks.append("<div class='missing-image'>нет изображений в выборке</div>")
    return "".join(image_blocks)


def render_summary_table(products: list[ProductSample]) -> str:
    rows = []
    for product in products:
        rows.append(
            "<tr>"
            f"<td>{esc(product.source)}</td>"
            f"<td>{esc(product.name)}</td>"
            f"<td>{product.char_fields}</td>"
            f"<td>{product.image_url_count}</td>"
            f"<td>{product.local_image_count}</td>"
            f"<td>{product.description_len}</td>"
            f"<td>{product.raw_full_text_len}</td>"
            "</tr>"
        )
    return (
        "<table class='summary'>"
        "<thead><tr>"
        "<th>SOURCE</th><th>NAME</th><th>CHAR_FIELDS</th>"
        "<th>IMAGE_URLS</th><th>LOCAL_IMAGES</th>"
        "<th>DESCRIPTION_LEN</th><th>RAW_TEXT_LEN</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_stats(products: list[ProductSample]) -> str:
    dns = [p for p in products if p.source == "dns"]
    mvideo = [p for p in products if p.source == "mvideo"]
    with_local = sum(1 for p in products if p.local_image_count > 0)
    without_local = len(products) - with_local

    def avg_chars(items: list[ProductSample]) -> str:
        if not items:
            return "0"
        return f"{sum(p.char_fields for p in items) / len(items):.1f}"

    return (
        "<ul class='stats'>"
        f"<li>DNS selected = {len(dns)}</li>"
        f"<li>MVIDEO selected = {len(mvideo)}</li>"
        f"<li>avg characteristics DNS = {avg_chars(dns)}</li>"
        f"<li>avg characteristics MVideo = {avg_chars(mvideo)}</li>"
        f"<li>products with local images = {with_local}</li>"
        f"<li>products without local images = {without_local}</li>"
        "</ul>"
    )


def render_product_card(product: ProductSample, *, embed: bool = False) -> str:
    category = product.category_path or product.category_name or "—"
    char_rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{esc(value)}</td></tr>"
        for name, value in sorted(product.characteristics.items(), key=lambda item: item[0].lower())
    )
    if not char_rows:
        char_rows = "<tr><td colspan='2'>—</td></tr>"

    return f"""
<section class="product" id="product-{esc(product.source)}-{esc(product.source_id)}">
  <h2>{esc(product.name)}</h2>
  <dl class="meta">
    <dt>source</dt><dd>{esc(product.source)}</dd>
    <dt>product_id</dt><dd>{product.product_id}</dd>
    <dt>source_id</dt><dd>{esc(product.source_id)}</dd>
    <dt>SKU</dt><dd>{esc(product.sku or '—')}</dd>
    <dt>category</dt><dd>{esc(category)}</dd>
    <dt>price</dt><dd>{esc(fmt_price(product.price))}</dd>
    <dt>CHAR_FIELDS</dt><dd>{product.char_fields}</dd>
    <dt>IMAGE_URLS</dt><dd>{product.image_url_count}</dd>
    <dt>LOCAL_IMAGES</dt><dd>{product.local_image_count}</dd>
    <dt>description length</dt><dd>{product.description_len}</dd>
    <dt>raw_full_text length</dt><dd>{product.raw_full_text_len}</dd>
  </dl>
  <h3>description</h3>
  <pre class="description">{esc(product.description or '—')}</pre>
  <h3>characteristics ({product.char_fields})</h3>
  <table class="chars"><thead><tr><th>Name</th><th>Value</th></tr></thead><tbody>{char_rows}</tbody></table>
  <h3>images (up to {MAX_IMAGES_DISPLAY})</h3>
  <div class="images">{render_product_images(product, embed=embed)}</div>
</section>
"""


def _page_styles() -> str:
    return """
    body { font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1a1a1a; background: #f7f7f9; }
    h1, h2, h3 { margin-top: 1.2em; }
    table { border-collapse: collapse; width: 100%; background: #fff; }
    th, td { border: 1px solid #d9d9de; padding: 8px 10px; vertical-align: top; text-align: left; }
    th { background: #ececf1; }
    .summary { margin: 16px 0 24px; font-size: 14px; }
    .stats { background: #fff; border: 1px solid #d9d9de; padding: 12px 18px; }
    .product { background: #fff; border: 1px solid #d9d9de; border-radius: 8px; padding: 16px 20px; margin: 24px 0; }
    .meta { display: grid; grid-template-columns: 180px 1fr; gap: 6px 12px; }
    .meta dt { font-weight: 600; color: #444; }
    .meta dd { margin: 0; }
    pre.description { white-space: pre-wrap; word-break: break-word; background: #fafafa; padding: 12px; border: 1px solid #ececf1; }
    .images { display: flex; flex-wrap: wrap; gap: 12px; }
    .images img { max-width: 220px; max-height: 220px; object-fit: contain; border: 1px solid #ddd; background: #fff; }
    figure { margin: 0; max-width: 240px; font-size: 12px; color: #555; }
    .missing-image { border: 1px dashed #c77; background: #fff7f7; padding: 10px; max-width: 420px; font-size: 13px; }
    .note { color: #666; font-size: 13px; }
"""


def render_html(products: list[ProductSample], *, embed: bool = False) -> str:
    generated = __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")
    if embed:
        title = "Демонстрация сбора товарных данных — DNS + M.Video"
        subtitle = "Автономный single-file HTML · изображения встроены (base64)"
    else:
        title = "Sample Review — 20 products"
        subtitle = f"Generated {esc(generated)} · DB read-only · {esc(DB_PATH)}"
    cards = "".join(render_product_card(product, embed=embed) for product in products)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>{_page_styles()}</style>
</head>
<body>
  <h1>{esc(title)}</h1>
  <p class="note">{subtitle}</p>
  {render_stats(products)}
  {render_summary_table(products)}
  {cards}
</body>
</html>
"""


def main() -> int:
    if not DB_PATH.is_file():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect_readonly()
    try:
        dns_products = select_products(conn, "dns", SAMPLE_PER_SOURCE)
        mvideo_products = select_products(conn, "mvideo", SAMPLE_PER_SOURCE)
    finally:
        conn.close()

    products = dns_products + mvideo_products
    if len(products) < SAMPLE_PER_SOURCE * 2:
        print(
            f"Warning: selected only {len(products)} products "
            f"(DNS={len(dns_products)}, M.Video={len(mvideo_products)})",
            file=sys.stderr,
        )

    INDEX_PATH.write_text(render_html(products), encoding="utf-8")
    DEMO_PATH.write_text(render_html(products, embed=True), encoding="utf-8")
    demo_size = DEMO_PATH.stat().st_size
    print(f"Wrote {INDEX_PATH}")
    print(f"Wrote {DEMO_PATH} ({demo_size:,} bytes)")
    print(f"DNS selected: {len(dns_products)}")
    print(f"M.Video selected: {len(mvideo_products)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
