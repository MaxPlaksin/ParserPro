PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=10000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS company_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT,
    inn TEXT NOT NULL,
    ogrn TEXT,
    source TEXT NOT NULL DEFAULT 'checko_web',
    name TEXT,
    url TEXT,
    source_url TEXT,

    revenue_year_1 INTEGER,
    revenue_value_1 REAL,
    revenue_year_2 INTEGER,
    revenue_value_2 REAL,
    revenue_change_percent REAL,

    profit_year_1 INTEGER,
    profit_value_1 REAL,
    profit_year_2 INTEGER,
    profit_value_2 REAL,
    profit_change_percent REAL,

    enforcement_total_count INTEGER,
    enforcement_active_count INTEGER,
    enforcement_total_amount REAL,

    arbitration_defendant_total_count INTEGER,
    arbitration_defendant_active_count INTEGER,
    arbitration_defendant_total_amount REAL,

    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT,
    parser_version TEXT,
    raw_data TEXT,
    checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    UNIQUE(inn, source)
);

CREATE TABLE IF NOT EXISTS check_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inn TEXT NOT NULL,
    company_id TEXT,
    name TEXT,
    force_refresh INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_checks_inn ON company_checks(inn);
CREATE INDEX IF NOT EXISTS idx_company_checks_company_id ON company_checks(company_id);
CREATE INDEX IF NOT EXISTS idx_company_checks_checked_at ON company_checks(checked_at);
CREATE INDEX IF NOT EXISTS idx_check_jobs_status ON check_jobs(status);
CREATE INDEX IF NOT EXISTS idx_check_jobs_inn ON check_jobs(inn);
