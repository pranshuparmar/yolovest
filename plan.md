# Phase 1: Foundation & Data Pipeline — Implementation Plan

## Scope (from REQUIREMENTS.md Section 6, Phase 1)
1. Database schema design + migration setup (FR-10.1)
2. Market data abstraction — concrete providers (jugaad-data, yfinance, tvDatafeed) with fallback chain
3. Feature engineering (technical indicators)
4. DatabaseConfig.path addition (TL review C2)

**Deferred to Phase 2:** News scraping (FR-2.3-2.5), sentiment (FR-2.7), premarket (FR-2.10-2.11). These are intelligence layer, not data pipeline.

---

## Step 1: Database Layer (`data/db.py`)

SQLite with WAL mode, aiosqlite. Implements `DatabaseProtocol` from context.py.

### Tables (from C13, FR-2.8, FR-10.1):
```sql
-- OHLCV candle data (daily + intraday)
CREATE TABLE ohlcv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,       -- 'daily', '5minute', '15minute'
    timestamp TEXT NOT NULL,      -- ISO 8601
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,         -- 'jugaad', 'yfinance', 'tvdatafeed'
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(symbol, interval, timestamp)
);
CREATE INDEX idx_ohlcv_symbol_interval ON ohlcv(symbol, interval, timestamp DESC);

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

-- Sentiment analysis results
CREATE TABLE sentiment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    sentiment TEXT NOT NULL,      -- 'bullish', 'bearish', 'neutral'
    confidence REAL NOT NULL,
    key_drivers TEXT,             -- JSON array
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_sentiment_symbol ON sentiment(symbol, created_at DESC);

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
    features_snapshot TEXT,       -- JSON
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
    direction_correct INTEGER,   -- 0/1/NULL
    target_hit INTEGER,
    actual_pnl_pct REAL
);

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

-- LLM review audit log
CREATE TABLE llm_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    decision TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    adjusted_size INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Schema version tracking
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Database class methods:
- `async initialize()` — create tables, enable WAL mode
- `async health_check() -> bool`
- `async is_kill_switch_active() -> bool`
- `async get_open_positions() -> list[dict]`
- `async upsert_ohlcv(symbol, interval, bars: list[OHLCVBar], source: str)`
- `async get_ohlcv(symbol, interval, days) -> list[OHLCVBar]`
- `async upsert_watchlist(stocks: list[dict])`
- `async get_watchlist() -> list[dict]`
- `async upsert_sentiment(symbol, result: SentimentResult)`
- `async upsert_premarket(context: dict)`
- `async get_latest_premarket() -> dict | None`
- `async set_system_state(key, value)`
- `async get_system_state(key) -> str | None`

### Config change:
Add `path: str = "./data/yolovest.db"` to `DatabaseConfig` (TL review C2).

---

## Step 2: Market Data Providers

### 2a: Fallback chain orchestrator (`data/ingester.py`)

`MarketDataIngester` wraps multiple providers with automatic fallback:
- Implements `MarketDataProtocol` (so it can be passed as `ctx.market_data`)
- `get_ohlcv()` tries providers in order: primary → fallback → error
- `get_quote()` same pattern
- `health_check()` returns True if at least one provider is up
- Data staleness validation (FR-10.4): reject data older than `stale_threshold_minutes`

### 2b: JugaadDataProvider (`data/jugaad.py`)
- Primary for daily/EOD data
- `get_ohlcv(symbol, "daily", days)` → fetch via jugaad-data NSE scraper
- `get_quote(symbol)` → latest price from jugaad-data
- `health_check()` → try fetching Nifty 50 LTP
- Rate limiting: respect NSE limits (built into jugaad-data)

### 2c: YFinanceProvider (`data/yfinance_provider.py`)
- Fallback for daily/EOD data
- Append `.NS` suffix for NSE symbols
- `get_ohlcv()` → yfinance download
- `get_quote()` → yfinance fast_info
- `health_check()` → try small download

### 2d: TVDatafeedProvider (`data/tvfeed.py`)
- Intraday data (5min, 15min candles)
- `get_ohlcv(symbol, "5minute"|"15minute", days)` → tvDatafeed API
- Free tier: 5min bars, last 15 days
- `health_check()` → try fetching NIFTY intraday

---

## Step 3: Feature Engineering (`data/features.py`)

Technical indicator computation using the OHLCV data. All indicators from FR-4.1 config:
- RSI (14-period default)
- MACD (12, 26, 9)
- Bollinger Bands (20, 2σ)
- VWAP (intraday)
- ATR (14-period)
- Volume Profile (relative volume)
- OBV
- SuperTrend (10, 3)
- EMA (configurable periods: 9, 21, 50, 200)

Input: `list[OHLCVBar]` → Output: `dict[str, float]` features snapshot.

Implement as pure functions (no DB or network calls) — easy to test.

Each indicator is toggleable via `strategy.indicators.*` config.

---

## Step 4: Update DatabaseProtocol + AppContext

Expand `DatabaseProtocol` in `context.py` to include all DB methods needed by skills.
Update `main.py` to create `Database` instance and inject into `AppContext`.

---

## Step 5: Tests

- `tests/test_db.py` — Database init, WAL mode, CRUD for all tables, upsert idempotency
- `tests/test_ingester.py` — Fallback chain (mock providers), staleness rejection
- `tests/test_features.py` — Each indicator against known values
- `tests/test_jugaad.py` / `test_yfinance.py` / `test_tvfeed.py` — Unit tests with mocked HTTP (no real API calls)

---

## Step 6: Config + pyproject.toml updates

- Add `database.path` to `DatabaseConfig`
- Fix Q2: `requires-python = ">=3.12"` in pyproject.toml
- Add dependencies: `jugaad-data`, `yfinance`, `tvdatafeed`, `ta` (technical analysis library)

---

## Deliverables (files to create/modify)

### New files:
1. `src/yolovest/data/db.py` — SQLite database layer
2. `src/yolovest/data/ingester.py` — Fallback chain orchestrator
3. `src/yolovest/data/jugaad.py` — jugaad-data provider
4. `src/yolovest/data/yfinance_provider.py` — yfinance provider
5. `src/yolovest/data/tvfeed.py` — tvDatafeed provider
6. `src/yolovest/data/features.py` — Feature engineering
7. `tests/test_db.py` — Database tests
8. `tests/test_ingester.py` — Ingester/fallback tests
9. `tests/test_features.py` — Feature engineering tests

### Modified files:
1. `src/yolovest/config.py` — Add `database.path`
2. `src/yolovest/context.py` — Expand `DatabaseProtocol`, `MarketDataProtocol`
3. `src/yolovest/main.py` — Wire up Database + Ingester
4. `pyproject.toml` — Fix python version, add deps

---

## Implementation Order

1. `config.py` changes (DatabaseConfig.path, pyproject.toml)
2. `data/db.py` (Database class) + `test_db.py`
3. `data/jugaad.py` + `data/yfinance_provider.py` + `data/tvfeed.py` (providers)
4. `data/ingester.py` (fallback chain) + `test_ingester.py`
5. `data/features.py` (indicators) + `test_features.py`
6. `context.py` (expand protocols) + `main.py` (wiring)
7. Run all tests, commit, push
