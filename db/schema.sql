PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_category_id TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, source_category_id)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    url TEXT NOT NULL,
    brand TEXT,
    name TEXT NOT NULL,
    sku TEXT,
    price REAL,
    old_price REAL,
    price_min_observed REAL,
    price_max_observed REAL,
    currency TEXT DEFAULT 'RUB',
    available INTEGER,
    rating REAL,
    reviews_count INTEGER,
    description TEXT,
    description_short TEXT,
    description_detailed TEXT,
    description_full TEXT,
    raw_full_text TEXT,
    reliability_percent REAL,
    reliability_text TEXT,
    display TEXT,
    dimensions TEXT,
    equipment TEXT,
    price_history_text TEXT,
    extra_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_products_source ON products(source);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
CREATE INDEX IF NOT EXISTS idx_products_last_seen ON products(last_seen_at);

CREATE TABLE IF NOT EXISTS product_characteristics (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    characteristics_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    source_url TEXT,
    kind TEXT NOT NULL DEFAULT 'image',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, path)
);

CREATE INDEX IF NOT EXISTS idx_product_images_product ON product_images(product_id);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price REAL NOT NULL,
    old_price REAL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_history_product ON price_history(product_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS catalog_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'petrovich',
    section_code TEXT NOT NULL,
    parent_section_code TEXT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'section',
    depth INTEGER NOT NULL DEFAULT 0,
    product_qty INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, section_code)
);

CREATE INDEX IF NOT EXISTS idx_catalog_sections_parent ON catalog_sections(source, parent_section_code);
CREATE INDEX IF NOT EXISTS idx_catalog_sections_kind ON catalog_sections(source, kind);
