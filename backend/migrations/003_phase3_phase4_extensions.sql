-- Prediction scoreboard, reports, and LLM review tracking

-- Prediction scoreboard (aggregated accuracy stats)
CREATE TABLE IF NOT EXISTS prediction_scoreboard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key TEXT NOT NULL,      -- e.g. "symbol:RELIANCE", "model:xgb-v1.0", "timeframe:intraday"
    group_type TEXT NOT NULL,     -- "symbol", "model", "timeframe", "overall"
    total_predictions INTEGER NOT NULL DEFAULT 0,
    correct_predictions INTEGER NOT NULL DEFAULT 0,
    accuracy REAL,
    avg_confidence REAL,
    target_hit_rate REAL,
    avg_pnl_pct REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(group_key, group_type)
);

CREATE INDEX IF NOT EXISTS idx_scoreboard_type ON prediction_scoreboard(group_type);

-- Reports archive
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type TEXT NOT NULL,    -- "daily", "weekly"
    report_date TEXT NOT NULL,
    content TEXT NOT NULL,        -- JSON blob
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reports_type_date ON reports(report_type, report_date DESC);

-- Add entry_price to predictions for scoring (if not exists via migration guard)
-- Note: predictions table already has signal_id which links to signals table for entry_price
