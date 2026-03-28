-- Manual approval queue for trade signals
CREATE TABLE IF NOT EXISTS pending_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    entry_price REAL NOT NULL,
    target_price REAL NOT NULL,
    stop_loss_price REAL NOT NULL,
    position_size INTEGER NOT NULL,
    confidence_score REAL,
    model_version TEXT,
    product TEXT DEFAULT 'MIS',
    signal_data TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_trades_status ON pending_trades(status, created_at);
