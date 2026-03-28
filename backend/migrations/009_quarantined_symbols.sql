-- Auto-quarantine for symbols with repeated data fetch failures
CREATE TABLE IF NOT EXISTS quarantined_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    quarantined_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_quarantined_symbol ON quarantined_symbols(symbol);
