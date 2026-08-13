#!/usr/bin/env python3
"""Static HTML overview: analytics + sample products with inline base64 images (no JS)."""

from __future__ import annotations

import base64
import html
import io
import json
import re
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"
OUTPUT_PATH = PROJECT_ROOT / "output" / "stores_overview.html"
SCREENSHOT_PATH = PROJECT_ROOT / "output" / "stores_overview_smoke.png"

SOURCES = [
    ("dns", "DNS", "output/dns"),
    ("mvideo", "M.Video", "output/mvideo"),
    ("regard", "Regard", "output/regard_full"),
    ("citilink", "Citilink", "output/citilink"),
]

SAMPLE_PER_SOURCE = 3
EXPECTED_PRODUCT_CARDS = SAMPLE_PER_SOURCE * len(SOURCES)
MAX_IMAGES = 5
RANDOM_SEED = 42
MAX_PREVIEW_PX = 900
JPEG_QUALITY = 78

STORE_COLORS = {
    "dns": "#0066cc",
    "mvideo": "#e31235",
    "regard": "#2d8f4e",
    "citilink": "#f7941d",
}


@dataclass
class ImageRow:
    db_path: str
    source_url: str
    local_abs: Path | None
    data_uri: str | None
    missing: bool


@dataclass
class ProductRow:
    source: str
    store_label: str
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
    characteristics: dict[str, str]
    images: list[ImageRow] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(1 for v in self.characteristics.values() if str(v).strip())

    @property
    def best_description(self) -> str | None:
        for value in (self.description_full, self.description_detailed, self.description):
            if value and value.strip():
                return value.strip()
        return None

    @property
    def local_image_count(self) -> int:
        return sum(1 for img in self.images if img.data_uri)


@dataclass
class SourceStats:
    source: str
    label: str
    output_dir: str
    categories_in_db: int = 0
    products: int = 0
    products_with_price: int = 0
    products_with_description: int = 0
    products_with_sku: int = 0
    products_with_characteristics: int = 0
    products_with_image_records: int = 0
    image_records_in_db: int = 0
    products_with_local_images: int = 0
    local_image_files: int = 0
    avg_chars: float = 0.0
    avg_images: float = 0.0
    xlsx_categories: int = 0
    disk_image_files: int = 0
    top_categories: list[tuple[str, int]] = field(default_factory=list)


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


def fmt_int(value: int | float) -> str:
    if isinstance(value, float):
        if value == int(value):
            value = int(value)
        else:
            return f"{value:,.1f}".replace(",", " ")
    return f"{int(value):,}".replace(",", " ")


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


def local_file_exists(db_path: str) -> bool:
    return resolve_local_path(db_path) is not None


def encode_preview_data_uri(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        with Image.open(path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                alpha = img.split()[-1] if img.mode in ("RGBA", "LA") else None
                rgb = img.convert("RGB")
                if alpha is not None:
                    background.paste(rgb, mask=alpha)
                    img = background
                else:
                    img = rgb
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((MAX_PREVIEW_PX, MAX_PREVIEW_PX), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
    except OSError:
        return None


def count_output_folder(output_rel: str) -> tuple[int, int]:
    root = PROJECT_ROOT / output_rel.replace("\\", "/")
    if not root.is_dir():
        return 0, 0
    xlsx = len(list(root.rglob("products.xlsx")))
    images = len([p for p in root.rglob("images/*/*") if p.is_file()])
    return xlsx, images


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
        images.append(
            ImageRow(
                db_path=db_path,
                source_url=(row["source_url"] or "").strip(),
                local_abs=local_abs,
                data_uri=None,
                missing=local_abs is None,
            )
        )
    return images


def load_product(conn: sqlite3.Connection, row: sqlite3.Row, label: str) -> ProductRow:
    return ProductRow(
        source=row["source"],
        store_label=label,
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
        characteristics=parse_characteristics(row["characteristics_json"]),
        images=load_images(conn, row["id"]),
    )


def load_candidates(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            p.id, p.source, p.source_id, p.sku, p.name, p.url, p.price,
            p.description, p.description_detailed, p.description_full,
            c.path AS category_path, c.name AS category_name,
            pc.characteristics_json
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        LEFT JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = ?
          AND p.name IS NOT NULL
          AND TRIM(p.name) != ''
        ORDER BY p.id
        """,
        (source,),
    ).fetchall()


def select_samples(conn: sqlite3.Connection, source: str, label: str, limit: int) -> list[ProductRow]:
    import random

    candidates: list[ProductRow] = []
    for row in load_candidates(conn, source):
        product = load_product(conn, row, label)
        if not any(img.local_abs for img in product.images):
            continue
        if not product.best_description:
            continue
        candidates.append(product)

    candidates.sort(
        key=lambda p: (
            -sum(1 for img in p.images if img.local_abs),
            -bool(p.best_description),
            -p.char_count,
            p.source_id,
        )
    )

    by_category: dict[str, list[ProductRow]] = {}
    for product in candidates:
        by_category.setdefault(product.category_path, []).append(product)

    categories = sorted(by_category.keys())
    rng = random.Random(RANDOM_SEED + hash(source) % 1000)
    rng.shuffle(categories)

    selected: list[ProductRow] = []
    per_cat: dict[str, int] = {c: 0 for c in categories}

    while len(selected) < limit:
        progressed = False
        for category_path in categories:
            if per_cat[category_path] >= 1:
                continue
            pool = by_category[category_path]
            idx = per_cat[category_path]
            if idx >= len(pool):
                continue
            selected.append(pool[idx])
            per_cat[category_path] += 1
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break

    if len(selected) < limit:
        used = {p.product_id for p in selected}
        for product in candidates:
            if product.product_id in used:
                continue
            selected.append(product)
            if len(selected) >= limit:
                break

    return selected[:limit]


def embed_product_images(product: ProductRow) -> None:
    for image in product.images:
        if image.local_abs is None:
            continue
        image.data_uri = encode_preview_data_uri(image.local_abs)
        if image.data_uri:
            image.missing = False


def embed_all_images(samples: dict[str, list[ProductRow]]) -> None:
    for products in samples.values():
        for product in products:
            embed_product_images(product)


def collect_source_stats(conn: sqlite3.Connection, source: str, label: str, output_rel: str) -> SourceStats:
    stats = SourceStats(source=source, label=label, output_dir=output_rel)
    stats.categories_in_db = conn.execute(
        "SELECT COUNT(*) FROM categories WHERE source = ?", (source,)
    ).fetchone()[0]
    stats.products = conn.execute(
        "SELECT COUNT(*) FROM products WHERE source = ?", (source,)
    ).fetchone()[0]
    if stats.products == 0:
        stats.xlsx_categories, stats.disk_image_files = count_output_folder(output_rel)
        return stats

    stats.products_with_price = conn.execute(
        "SELECT COUNT(*) FROM products WHERE source = ? AND price IS NOT NULL", (source,)
    ).fetchone()[0]
    stats.products_with_description = conn.execute(
        """
        SELECT COUNT(*) FROM products
        WHERE source = ?
          AND (
            (description IS NOT NULL AND TRIM(description) != '')
            OR (description_detailed IS NOT NULL AND TRIM(description_detailed) != '')
            OR (description_full IS NOT NULL AND TRIM(description_full) != '')
          )
        """,
        (source,),
    ).fetchone()[0]
    stats.products_with_sku = conn.execute(
        """
        SELECT COUNT(*) FROM products
        WHERE source = ? AND sku IS NOT NULL AND TRIM(sku) != ''
        """,
        (source,),
    ).fetchone()[0]
    stats.products_with_characteristics = conn.execute(
        """
        SELECT COUNT(*) FROM products p
        JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = ?
          AND pc.characteristics_json IS NOT NULL
          AND TRIM(pc.characteristics_json) NOT IN ('', '{}')
        """,
        (source,),
    ).fetchone()[0]
    stats.products_with_image_records = conn.execute(
        """
        SELECT COUNT(DISTINCT p.id) FROM products p
        JOIN product_images pi ON pi.product_id = p.id
        WHERE p.source = ?
        """,
        (source,),
    ).fetchone()[0]
    stats.image_records_in_db = conn.execute(
        """
        SELECT COUNT(*) FROM product_images pi
        JOIN products p ON p.id = pi.product_id
        WHERE p.source = ?
        """,
        (source,),
    ).fetchone()[0]

    products_with_local: set[int] = set()
    local_files = 0
    seen_paths: set[str] = set()
    for row in conn.execute(
        """
        SELECT p.id, pi.path FROM products p
        JOIN product_images pi ON pi.product_id = p.id
        WHERE p.source = ?
        """,
        (source,),
    ).fetchall():
        path = (row["path"] or "").replace("\\", "/")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        if local_file_exists(path):
            local_files += 1
            products_with_local.add(row["id"])
    stats.local_image_files = local_files
    stats.products_with_local_images = len(products_with_local)

    char_counts = []
    img_counts = []
    for row in conn.execute(
        """
        SELECT p.id, pc.characteristics_json,
               (SELECT COUNT(*) FROM product_images pi WHERE pi.product_id = p.id) AS img_cnt
        FROM products p
        LEFT JOIN product_characteristics pc ON pc.product_id = p.id
        WHERE p.source = ?
        """,
        (source,),
    ).fetchall():
        chars = parse_characteristics(row["characteristics_json"])
        char_counts.append(sum(1 for v in chars.values() if str(v).strip()))
        img_counts.append(row["img_cnt"] or 0)
    stats.avg_chars = statistics.mean(char_counts) if char_counts else 0.0
    stats.avg_images = statistics.mean(img_counts) if img_counts else 0.0

    top = conn.execute(
        """
        SELECT COALESCE(c.path, c.name, '—') AS cat, COUNT(*) AS cnt
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.source = ?
        GROUP BY cat
        ORDER BY cnt DESC
        LIMIT 5
        """,
        (source,),
    ).fetchall()
    stats.top_categories = [(r["cat"], r["cnt"]) for r in top]
    stats.xlsx_categories, stats.disk_image_files = count_output_folder(output_rel)
    return stats


def render_images(product: ProductRow) -> str:
    if not product.images:
        return "<p class='missing'>Нет записей об изображениях в БД</p>"
    blocks: list[str] = []
    for image in product.images:
        if image.data_uri:
            blocks.append(
                f'<figure><img src="{image.data_uri}" alt="{esc(product.name)}">'
                f"<figcaption>{esc(image.db_path)}</figcaption></figure>"
            )
            continue
        url_note = ""
        if image.source_url:
            url_note = f"<br><a href=\"{esc(image.source_url)}\">source URL</a>"
        blocks.append(
            f"<figure class='missing'><div>Файл не найден</div>"
            f"<figcaption>{esc(image.db_path or '—')}{url_note}</figcaption></figure>"
        )
    return "<div class='images'>" + "".join(blocks) + "</div>"


def render_characteristics(product: ProductRow) -> str:
    if not product.characteristics:
        return "<p class='muted'>— характеристики отсутствуют —</p>"
    rows = "".join(
        f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>"
        for k, v in sorted(product.characteristics.items(), key=lambda x: x[0].lower())[:20]
    )
    extra = ""
    if len(product.characteristics) > 20:
        extra = f"<p class='muted'>… ещё {len(product.characteristics) - 20} полей</p>"
    return (
        f"<p>Заполнено полей: <strong>{product.char_count}</strong></p>"
        f"<table class='chars'><thead><tr><th>Параметр</th><th>Значение</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>{extra}"
    )


def render_product(product: ProductRow, index: int) -> str:
    color = STORE_COLORS.get(product.source, "#333")
    desc = product.best_description or "—"
    desc_preview = desc[:1200] + ("…" if len(desc) > 1200 else "")
    return f"""
<article class="product" style="border-left: 4px solid {color}">
  <header>
    <span class="store-badge" style="background:{color}">{esc(product.store_label)}</span>
    <h3>#{index}. {esc(product.name)}</h3>
  </header>
  <dl class="meta">
    <dt>Категория</dt><dd>{esc(product.category_path)}</dd>
    <dt>Артикул / SKU</dt><dd>{esc(product.sku or '—')}</dd>
    <dt>ID</dt><dd>{esc(product.source_id)}</dd>
    <dt>Цена</dt><dd>{esc(fmt_price(product.price))}</dd>
    <dt>URL</dt><dd><a href="{esc(product.url)}" target="_blank" rel="noopener">{esc(product.url)}</a></dd>
    <dt>Фото (в отчёте)</dt><dd>{product.local_image_count}</dd>
    <dt>Характеристик</dt><dd>{product.char_count}</dd>
  </dl>
  <h4>Описание</h4>
  <pre class="desc">{esc(desc_preview)}</pre>
  <h4>Характеристики</h4>
  {render_characteristics(product)}
  <h4>Изображения</h4>
  {render_images(product)}
</article>
"""


def render_analytics_table(all_stats: list[SourceStats], table_counts: dict[str, int]) -> str:
    total_products = sum(s.products for s in all_stats)
    total_images_db = sum(s.image_records_in_db for s in all_stats)
    total_local = sum(s.local_image_files for s in all_stats)
    total_cats = sum(s.categories_in_db for s in all_stats)

    header = """
<tr>
  <th>Магазин</th>
  <th>Товаров</th>
  <th>Категорий</th>
  <th>С ценой</th>
  <th>С описанием</th>
  <th>С характеристиками</th>
  <th>Записей фото в БД</th>
  <th>Локальных файлов</th>
  <th>Товаров с фото</th>
  <th>XLSX категорий</th>
  <th>Файлов на диске</th>
  <th>Ø характеристик</th>
  <th>Ø фото/товар</th>
</tr>"""
    rows = []
    for s in all_stats:
        color = STORE_COLORS.get(s.source, "#333")
        rows.append(
            f"<tr>"
            f"<td><span class='dot' style='background:{color}'></span>{esc(s.label)}</td>"
            f"<td><strong>{fmt_int(s.products)}</strong></td>"
            f"<td>{fmt_int(s.categories_in_db)}</td>"
            f"<td>{fmt_int(s.products_with_price)}</td>"
            f"<td>{fmt_int(s.products_with_description)}</td>"
            f"<td>{fmt_int(s.products_with_characteristics)}</td>"
            f"<td>{fmt_int(s.image_records_in_db)}</td>"
            f"<td>{fmt_int(s.local_image_files)}</td>"
            f"<td>{fmt_int(s.products_with_local_images)}</td>"
            f"<td>{fmt_int(s.xlsx_categories)}</td>"
            f"<td>{fmt_int(s.disk_image_files)}</td>"
            f"<td>{s.avg_chars:.1f}</td>"
            f"<td>{s.avg_images:.1f}</td>"
            f"</tr>"
        )
    rows.append(
        f"<tr class='total'>"
        f"<td><strong>ИТОГО</strong></td>"
        f"<td><strong>{fmt_int(total_products)}</strong></td>"
        f"<td>{fmt_int(total_cats)}</td>"
        f"<td colspan='4'></td>"
        f"<td>{fmt_int(total_images_db)}</td>"
        f"<td>{fmt_int(total_local)}</td>"
        f"<td colspan='5'></td>"
        f"</tr>"
    )

    db_info = "".join(
        f"<div class='kpi'><span class='kpi-val'>{fmt_int(v)}</span><span class='kpi-lbl'>{esc(k)}</span></div>"
        for k, v in table_counts.items()
    )

    top_blocks = []
    for s in all_stats:
        if not s.top_categories:
            continue
        items = "".join(
            f"<li>{esc(cat)} — <strong>{fmt_int(cnt)}</strong></li>"
            for cat, cnt in s.top_categories
        )
        color = STORE_COLORS.get(s.source, "#333")
        top_blocks.append(
            f"<div class='top-cat'><h4 style='color:{color}'>{esc(s.label)} — топ категорий</h4><ul>{items}</ul></div>"
        )

    return f"""
<section class="analytics">
  <h2>Аналитика по магазинам</h2>
  <div class="kpi-row">{db_info}</div>
  <div class="table-wrap">
    <table class="analytics-table">
      <thead>{header}</thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
  <div class="top-grid">{"".join(top_blocks)}</div>
</section>
"""


def page_styles() -> str:
    return """
    * { box-sizing: border-box; }
    body { font-family: Segoe UI, system-ui, sans-serif; margin: 0; color: #1a1a2e; background: #eef0f4; }
    .hero { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 60%, #0f3460 100%); color: #fff; padding: 32px 40px 28px; }
    .hero h1 { margin: 0 0 8px; font-size: 28px; }
    .hero p { margin: 0; opacity: 0.85; font-size: 14px; }
    main { max-width: 1200px; margin: 0 auto; padding: 24px 20px 48px; }
    h2 { font-size: 20px; margin: 0 0 16px; }
    h3 { margin: 0 0 8px; font-size: 17px; }
    h4 { margin: 16px 0 8px; font-size: 14px; color: #444; text-transform: uppercase; letter-spacing: 0.04em; }
    .analytics { background: #fff; border-radius: 10px; padding: 24px; margin-bottom: 28px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .kpi-row { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
    .kpi { background: #f4f6fa; border: 1px solid #dde1ea; border-radius: 8px; padding: 12px 16px; min-width: 120px; text-align: center; }
    .kpi-val { display: block; font-size: 22px; font-weight: 700; color: #0f3460; }
    .kpi-lbl { font-size: 11px; color: #666; text-transform: uppercase; }
    .table-wrap { overflow-x: auto; }
    .analytics-table { width: 100%; border-collapse: collapse; font-size: 13px; }
    .analytics-table th, .analytics-table td { border: 1px solid #dde1ea; padding: 8px 10px; text-align: left; white-space: nowrap; }
    .analytics-table th { background: #ececf1; font-weight: 600; }
    .analytics-table tr.total td { background: #f0f4ff; font-weight: 600; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
    .top-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-top: 20px; }
    .top-cat { background: #f8f9fb; border: 1px solid #ececf1; border-radius: 8px; padding: 12px 16px; }
    .top-cat h4 { margin: 0 0 8px; text-transform: none; letter-spacing: 0; font-size: 14px; }
    .top-cat ul { margin: 0; padding-left: 18px; font-size: 12px; color: #444; }
    .store-section { margin-bottom: 32px; }
    .store-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
    .store-header h2 { margin: 0; }
    .store-count { font-size: 13px; color: #666; background: #fff; border: 1px solid #dde1ea; border-radius: 20px; padding: 4px 12px; }
    .product { background: #fff; border-radius: 10px; padding: 20px 24px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.07); }
    .store-badge { display: inline-block; color: #fff; font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 4px; margin-bottom: 8px; letter-spacing: 0.05em; }
    .meta { display: grid; grid-template-columns: 140px 1fr; gap: 5px 12px; font-size: 13px; margin-bottom: 8px; }
    .meta dt { font-weight: 600; color: #555; }
    .meta dd { margin: 0; word-break: break-word; }
    pre.desc { white-space: pre-wrap; word-break: break-word; background: #f8f9fb; border: 1px solid #ececf1; padding: 12px; font-size: 13px; border-radius: 6px; margin: 0; max-height: 280px; overflow-y: auto; }
    table.chars { border-collapse: collapse; width: 100%; font-size: 12px; }
    table.chars th, table.chars td { border: 1px solid #dde1ea; padding: 5px 8px; vertical-align: top; }
    table.chars th { background: #ececf1; }
    .images { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 8px; }
    .images img { max-width: 160px; max-height: 160px; object-fit: contain; border: 1px solid #ddd; background: #fff; border-radius: 4px; }
    figure { margin: 0; max-width: 180px; font-size: 10px; color: #666; }
    figure.missing, .missing { border: 2px dashed #e57373; background: #fff5f5; color: #c62828; padding: 10px; border-radius: 6px; font-size: 12px; }
    .muted { color: #888; font-style: italic; font-size: 13px; }
"""


def render_html(all_stats: list[SourceStats], samples: dict[str, list[ProductRow]], table_counts: dict[str, int]) -> str:
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    analytics = render_analytics_table(all_stats, table_counts)

    store_sections = []
    global_idx = 1
    for source, label, _ in SOURCES:
        products = samples.get(source, [])
        stats = next((s for s in all_stats if s.source == source), None)
        count_note = fmt_int(stats.products) if stats else "0"
        color = STORE_COLORS.get(source, "#333")
        cards = "".join(render_product(p, global_idx + i) for i, p in enumerate(products))
        global_idx += len(products)
        store_sections.append(
            f"""
<section class="store-section">
  <div class="store-header">
    <h2 style="color:{color}">{esc(label)}</h2>
    <span class="store-count">{count_note} товаров в БД · {len(products)} примеров</span>
  </div>
  {cards if cards else "<p class='muted'>Нет данных для отображения</p>"}
</section>
"""
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Обзор парсинга — DNS · M.Video · Regard · Citilink</title>
  <style>{page_styles()}</style>
</head>
<body>
  <div class="hero">
    <h1>Обзор парсинга интернет-магазинов</h1>
    <p>Сгенерировано {esc(generated)} · автономный HTML · inline base64 · по {SAMPLE_PER_SOURCE} примера на магазин</p>
  </div>
  <main>
    {analytics}
    <h2 style="margin-bottom:20px">Примеры товаров</h2>
    {"".join(store_sections)}
  </main>
</body>
</html>
"""


@dataclass
class VerifyResult:
    ok: bool
    html_size_mb: float
    product_cards: int
    inline_images: int
    script_tags: int
    unresolved_placeholders: int
    ends_html: bool
    errors: list[str]


def verify_html(path: Path, expected_cards: int) -> VerifyResult:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    size = path.stat().st_size
    html_size_mb = size / (1024 * 1024)

    if size <= 100 * 1024:
        errors.append("size <= 100 KB")
    if not text.startswith("<!DOCTYPE html>"):
        errors.append("missing DOCTYPE")
    if not text.rstrip().endswith("</html>"):
        errors.append("missing </html>")
    if "<body" not in text:
        errors.append("missing <body")
    if "</body>" not in text:
        errors.append("missing </body>")
    if "Обзор парсинга интернет-магазинов" not in text:
        errors.append("missing title heading")
    for label in ("DNS", "M.Video", "Regard", "Citilink"):
        if label not in text:
            errors.append(f"missing store label {label}")

    product_cards = len(re.findall(r'<article class="product"', text))
    if product_cards != expected_cards:
        errors.append(f"product cards {product_cards} != {expected_cards}")

    inline_images = text.count('src="data:image/')
    if inline_images <= 0:
        errors.append("no inline images")

    script_tags = len(re.findall(r"<script\b", text, flags=re.IGNORECASE))
    if script_tags != 0:
        errors.append(f"script tags = {script_tags}")

    forbidden = [
        "IMAGE_PAYLOADS",
        "data-img",
        "data_img",
        "{esc(",
        "{generated}",
        "{{",
        "traceback",
    ]
    unresolved = 0
    for token in forbidden:
        count = text.count(token)
        unresolved += count
        if count:
            errors.append(f"forbidden token {token!r} x{count}")

    ends_html = text.rstrip().endswith("</html>")
    return VerifyResult(
        ok=not errors,
        html_size_mb=html_size_mb,
        product_cards=product_cards,
        inline_images=inline_images,
        script_tags=script_tags,
        unresolved_placeholders=unresolved,
        ends_html=ends_html,
        errors=errors,
    )


def browser_smoke_test(html_path: Path, screenshot_path: Path, expected_cards: int) -> dict:
    from playwright.sync_api import sync_playwright

    url = html_path.resolve().as_uri()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(2000)

        title = page.title()
        body_text_length = page.evaluate("() => document.body.innerText.length")
        product_cards = page.locator("article.product").count()
        images_total = page.locator("img").count()
        images_complete = page.evaluate(
            """() => {
                const imgs = Array.from(document.querySelectorAll('img'));
                return imgs.filter(img => img.complete && img.naturalWidth > 0).length;
            }"""
        )
        body_visible = page.evaluate(
            """() => {
                const style = window.getComputedStyle(document.body);
                return style.display !== 'none' && style.visibility !== 'hidden';
            }"""
        )
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    ok = (
        body_text_length > 1000
        and product_cards == expected_cards
        and images_total > 0
        and images_complete > 0
        and body_visible
    )
    return {
        "BROWSER_OPEN_OK": ok,
        "TITLE": title,
        "BODY_TEXT_LENGTH": body_text_length,
        "PRODUCT_CARDS": product_cards,
        "IMAGES_TOTAL": images_total,
        "IMAGES_COMPLETE": images_complete,
        "SCREENSHOT_PATH": str(screenshot_path),
        "BODY_VISIBLE": body_visible,
    }


def print_verify_report(result: VerifyResult) -> None:
    print(f"HTML_SIZE_MB={result.html_size_mb:.2f}")
    print(f"PRODUCT_CARDS={result.product_cards}")
    print(f"INLINE_IMAGES={result.inline_images}")
    print(f"SCRIPT_TAGS={result.script_tags}")
    print(f"UNRESOLVED_PLACEHOLDERS={result.unresolved_placeholders}")
    print(f"ENDS_HTML={result.ends_html}")
    if result.errors:
        print("VERIFY_ERRORS=" + "; ".join(result.errors))


def print_browser_report(report: dict) -> None:
    for key in (
        "BROWSER_OPEN_OK",
        "TITLE",
        "BODY_TEXT_LENGTH",
        "PRODUCT_CARDS",
        "IMAGES_TOTAL",
        "IMAGES_COMPLETE",
        "SCREENSHOT_PATH",
    ):
        print(f"{key}={report.get(key)}")


def main() -> int:
    if not DB_PATH.is_file():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = connect_readonly()
    try:
        tables = ["categories", "products", "product_images", "product_characteristics", "price_history"]
        table_counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables
        }

        all_stats: list[SourceStats] = []
        samples: dict[str, list[ProductRow]] = {}
        for source, label, output_rel in SOURCES:
            all_stats.append(collect_source_stats(conn, source, label, output_rel))
            samples[source] = select_samples(conn, source, label, SAMPLE_PER_SOURCE)
    finally:
        conn.close()

    embed_all_images(samples)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render_html(all_stats, samples, table_counts), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")

    verify = verify_html(OUTPUT_PATH, EXPECTED_PRODUCT_CARDS)
    print_verify_report(verify)
    if not verify.ok:
        return 1

    browser = browser_smoke_test(OUTPUT_PATH, SCREENSHOT_PATH, EXPECTED_PRODUCT_CARDS)
    print_browser_report(browser)
    if not browser["BROWSER_OPEN_OK"]:
        return 1

    for s in all_stats:
        print(f"  {s.label}: {s.products} products, {len(samples[s.source])} samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
