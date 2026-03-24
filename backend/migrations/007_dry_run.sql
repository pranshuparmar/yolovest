-- Dry-run signal preview results (stored for next-day comparison)
CREATE TABLE IF NOT EXISTS dry_run_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    entry_price REAL NOT NULL,
    target_price REAL NOT NULL,
    stop_loss_price REAL NOT NULL,
    confidence_score REAL NOT NULL,
    position_size INTEGER,
    model_version TEXT,
    composite_score REAL,
    technical_score REAL,
    volume_momentum_score REAL,
    news_sentiment_score REAL,
    fundamental_score REAL,
    -- Filled next day during comparison
    actual_open REAL,
    actual_close REAL,
    actual_high REAL,
    actual_low REAL,
    direction_correct INTEGER,
    target_hit INTEGER,
    actual_move_pct REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    scored_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dry_run_run_id ON dry_run_results(run_id);
CREATE INDEX IF NOT EXISTS idx_dry_run_created ON dry_run_results(created_at);
