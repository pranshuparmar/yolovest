-- Economic calendar events (RBI/Fed/earnings dates)

CREATE TABLE IF NOT EXISTS economic_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,       -- "monetary_policy", "earnings", "gdp", "trade_data"
    title TEXT NOT NULL,
    country TEXT NOT NULL,          -- "IN", "US", etc.
    impact TEXT NOT NULL DEFAULT 'medium',  -- "high", "medium", "low"
    source TEXT NOT NULL,           -- "rbi_schedule", "fed_schedule", "nse_announcements"
    symbol TEXT,                    -- NULL for macro events, stock symbol for earnings
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(content_hash)
);

CREATE INDEX IF NOT EXISTS idx_econ_events_date ON economic_events(event_date);
CREATE INDEX IF NOT EXISTS idx_econ_events_type ON economic_events(event_type, event_date);
CREATE INDEX IF NOT EXISTS idx_econ_events_symbol ON economic_events(symbol) WHERE symbol IS NOT NULL;
