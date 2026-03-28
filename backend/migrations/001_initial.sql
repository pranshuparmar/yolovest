-- Migration 001: Initial schema for YoloVest
-- All tables created upfront for forward compatibility.

-- OHLCV candle data (daily + intraday)
CREATE TABLE ohlcv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(symbol, interval, timestamp)
);
CREATE INDEX idx_ohlcv_lookup ON ohlcv(symbol, interval, timestamp DESC);

-- Dynamic watchlist from market-scan
CREATE TABLE watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    composite_score REAL,
    technical_score REAL,
    volume_momentum_score REAL,
    news_sentiment_score REAL,
    fundamental_score REAL,
    sector TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_watchlist_symbol ON watchlist(symbol);

-- Trades (full lifecycle)
CREATE TABLE trades (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    entry_price REAL NOT NULL,
    fill_price REAL NOT NULL DEFAULT 0,
    quantity INTEGER NOT NULL,
    stop_loss_price REAL NOT NULL,
    target_price REAL NOT NULL,
    order_id TEXT,
    sl_order_id TEXT,
    product TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    slippage REAL DEFAULT 0,
    pnl REAL,
    exit_price REAL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);

-- Signals
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    entry_price REAL NOT NULL,
    target_price REAL NOT NULL,
    stop_loss_price REAL NOT NULL,
    position_size INTEGER NOT NULL,
    confidence_score REAL NOT NULL,
    model_version TEXT NOT NULL,
    features_snapshot TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Predictions for self-learning
CREATE TABLE predictions (
    prediction_id TEXT PRIMARY KEY,
    signal_id INTEGER REFERENCES signals(id),
    trade_id TEXT REFERENCES trades(trade_id),
    created_at TEXT NOT NULL,
    prediction_end_time TEXT,
    actual_price REAL,
    direction_correct INTEGER,
    target_hit INTEGER,
    actual_pnl_pct REAL
);

-- Sentiment analysis results
CREATE TABLE sentiment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    confidence REAL NOT NULL,
    key_drivers TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sentiment_lookup ON sentiment(symbol, created_at DESC);

-- Pre-market context
CREATE TABLE premarket (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    gift_nifty_change_pct REAL,
    us_sp500_change_pct REAL,
    market_bias TEXT,
    llm_summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- System state (kill switch, etc.)
CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- LLM review log
CREATE TABLE llm_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    decision TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    adjusted_size INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Audit log
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ist TEXT NOT NULL,
    action_type TEXT NOT NULL,
    skill_name TEXT,
    input_summary TEXT,
    output_summary TEXT,
    duration_ms REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_timestamp ON audit_log(timestamp_ist DESC);
CREATE INDEX idx_audit_action ON audit_log(action_type, timestamp_ist DESC);
