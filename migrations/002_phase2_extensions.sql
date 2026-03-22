-- Phase 2: Intelligence Layer schema extensions

-- News articles table for dedup tracking (FR-2.13)
CREATE TABLE IF NOT EXISTS news_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL,
    headline TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT,
    symbols TEXT,  -- JSON array
    published_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(content_hash, source)
);

CREATE INDEX IF NOT EXISTS idx_news_symbols ON news_articles(symbols);
CREATE INDEX IF NOT EXISTS idx_news_created ON news_articles(created_at);

-- Fundamental data cache (FR-2.4)
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol TEXT NOT NULL,
    pe_ratio REAL,
    pb_ratio REAL,
    debt_to_equity REAL,
    promoter_holding_pct REAL,
    quarterly_revenue_growth_pct REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol)
);

-- Model versions tracking (FR-7.7)
CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT NOT NULL,
    version TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sharpe_ratio REAL,
    max_drawdown_pct REAL,
    win_rate REAL,
    profit_factor REAL,
    status TEXT NOT NULL DEFAULT 'shadow',
    shadow_start_date TEXT,
    promoted_date TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_model_type_status ON model_versions(model_type, status);

-- Failure analysis from LLM (FR-7.6)
CREATE TABLE IF NOT EXISTS failure_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT,
    patterns TEXT,
    recommendations TEXT,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Add index on trades.status for get_open_positions (TL nit n1)
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
