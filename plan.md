# Phase 1: Foundation & Data Pipeline — Implementation Plan

**v2 — Updated after PM review (docs/pm_phase1_review.md)**

## Scope (from REQUIREMENTS.md Section 6, Phase 1)
1. Database schema design + migration system (FR-10.1)
2. Market data abstraction — concrete providers (jugaad-data, yfinance, tvDatafeed) with fallback chain
3. Feature engineering (technical indicators)
4. Broker abstraction — Zerodha Kite Connect implementation (FR-6.1, FR-6.3, FR-6.8)
5. LLM abstraction — Gemini implementation (all 7 methods)
6. Config fixes (DatabaseConfig.path, requires-python)

**Deferred to Phase 2:** News scraping (FR-2.3-2.5), sentiment analysis invocation (FR-2.7), premarket ingestion (FR-2.10-2.11), NSE/BSE official data (FR-2.2). These are intelligence layer, not data pipeline. H19 (scraping fragility) and H20 (FR-2.7/FR-2.3 dependency) are noted for Phase 2 planning.

**Pre-filter strategy (H10):** The data pipeline targets Nifty 500 (not full NSE ~2000), configurable via `scanning.universe` config. This is feasible within free API rate limits on a 15-minute heartbeat.

---

## Step 1: Database Layer (`data/db.py`) + Migration System

SQLite with WAL mode, aiosqlite. Implements `DatabaseProtocol` from context.py.

### Migration System (FR-10.1)

Sequential numbered SQL files in `migrations/` directory:
```
migrations/
  001_initial.sql      -- Phase 1 tables
  002_add_audit.sql    -- audit_log table (future migrations follow this pattern)
```

Migration runner:
- `async run_migrations(db_path)` — reads `schema_version` table, applies unapplied `.sql` files in order
- Each migration is atomic (wrapped in transaction)
- `schema_version` table tracks which migrations have been applied
- `initialize()` calls `run_migrations()` then enables WAL mode

### Tables — Migration 001 (Phase 1 core):
```sql
-- Schema version tracking (created before migrations run)
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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

-- Audit log (NFR-5: every decision point must be logged)
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ist TEXT NOT NULL,
    action_type TEXT NOT NULL,     -- 'skill_run', 'order_placed', 'signal_generated', etc.
    skill_name TEXT,
    input_summary TEXT,            -- JSON: abbreviated input context
    output_summary TEXT,           -- JSON: abbreviated output/result
    duration_ms REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp_ist DESC);
CREATE INDEX idx_audit_log_action ON audit_log(action_type, timestamp_ist DESC);
```

Note: All tables are created in migration 001 (forward-compatible). Phase 2+ tables will be added via new migration files. DB methods for Phase 2+ tables (upsert_sentiment, upsert_premarket, get_latest_premarket) are deferred until their consuming skills are built.

### Database class methods (Phase 1 only):
- `async initialize()` — run migrations, enable WAL mode
- `async close()` — close connection
- `async health_check() -> bool`
- `async is_kill_switch_active() -> bool`
- `async set_system_state(key, value)` / `async get_system_state(key) -> str | None`
- `async get_open_positions() -> list[dict]`
- `async upsert_ohlcv(symbol, interval, bars: list[OHLCVBar], source: str)`
- `async get_ohlcv(symbol, interval, days) -> list[OHLCVBar]`
- `async upsert_watchlist(stocks: list[dict])`
- `async get_watchlist() -> list[dict]`
- `async log_audit(action_type, skill_name, input_summary, output_summary, duration_ms)`

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
- Data quality validation: `high >= low`, `close` within `[low, high]` range
- Per-provider rate limiting via `asyncio.Semaphore` (configurable)

### 2b: JugaadDataProvider (`data/jugaad.py`)
- Primary for daily/EOD data
- `get_ohlcv(symbol, "daily", days)` → fetch via jugaad-data NSE scraper
- `get_quote(symbol)` → latest price from jugaad-data
- `health_check()` → try fetching Nifty 50 LTP
- Rate limiting: built-in caching + respect NSE limits

### 2c: YFinanceProvider (`data/yfinance_provider.py`)
- Fallback for daily/EOD data
- Append `.NS` suffix for NSE symbols
- `get_ohlcv()` → yfinance download
- `get_quote()` → yfinance fast_info
- `health_check()` → try small download
- Rate limiting: `asyncio.Semaphore(2)` — max 2 concurrent requests, 0.5s delay between

### 2d: TVDatafeedProvider (`data/tvfeed.py`)
- Intraday data (5min, 15min candles)
- `get_ohlcv(symbol, "5minute"|"15minute", days)` → tvDatafeed API
- Free tier: 5min bars, last 15 days
- `health_check()` → try fetching NIFTY intraday
- Rate limiting: `asyncio.Semaphore(1)` — sequential requests

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

## Step 4: Zerodha Broker (`broker/zerodha.py`)

Concrete implementation of `BrokerBase` ABC using Kite Connect API.

### Methods:
- `__init__(api_key, api_secret)` — initialize Kite client
- `async authenticate(request_token)` — exchange request_token for access_token (FR-6.3)
- `async is_authenticated() -> bool` — check if access_token is valid
- `async place_order(symbol, side, quantity, order_type, product, price, trigger_price) -> str` — place order via Kite API (FR-6.1)
- `async cancel_order(order_id) -> bool`
- `async get_order_status(order_id) -> dict`
- `async get_positions() -> list[dict]`
- `async get_pending_orders() -> list[dict]`
- `async get_margins() -> dict`

### Key design:
- Rate limiter: `asyncio.Semaphore(8)` — stay under Kite's 10 req/s limit (FR-6.8)
- All API calls wrapped with exponential backoff retry (FR-6.6: max 3 retries, base 2s)
- Paper mode: if `config.mode == "paper"`, skip actual API calls, simulate fills at LTP + slippage (FR-6.2)
- Kite Connect SDK (`kiteconnect`) as dependency

### Dependencies:
- `kiteconnect>=5.0,<6` added to pyproject.toml

---

## Step 5: Gemini LLM (`llm/gemini.py`)

Concrete implementation of `LLMBase` ABC using Google Gemini API.

### All 7 methods:
- `__init__(api_key, model)` — initialize Gemini client
- `async ping() -> bool` — small test request
- `async review_trade(context: TradeContext) -> TradeReview` — structured prompt → JSON parse → TradeReview
- `async analyze_sentiment(symbol, headlines) -> SentimentResult` — headlines → sentiment classification
- `async summarize_with_web_grounding(prompt) -> WebGroundingResult` — Gemini search grounding
- `async validate_watchlist(shortlist, sector_analysis, premarket_context) -> WatchlistValidation`
- `async summarize_market_day() -> MarketDaySummary`
- `async analyze_prediction_failures(failures) -> FailureAnalysis`

### Key design:
- Use `google-genai` SDK (official Python client)
- Structured output via Gemini's JSON mode — prompt includes Pydantic schema, response parsed into schema
- Exponential backoff on rate limit errors
- Model configurable via `llm.model` config (default: `gemini-2.5-pro`)
- NFR-6: Use Flash model for routine checks (ping, sentiment), Pro for complex analysis (trade review, failure analysis)

### Dependencies:
- `google-genai>=1.0,<2` added to pyproject.toml

---

## Step 6: Update Protocols + Wiring

### DatabaseProtocol expansion (context.py):
Keep protocol minimal — only methods used across multiple skills:
- `health_check()`, `is_kill_switch_active()`, `get_open_positions()`
- `upsert_ohlcv()`, `get_ohlcv()`
- `set_system_state()`, `get_system_state()`
- `log_audit()`

Skills needing specialized DB methods (e.g., `upsert_sentiment`) will access the `Database` class directly via `ctx.db` with type narrowing where needed.

### main.py wiring:
- Replace `_StubDB` with `Database` instance
- Replace `_StubMarketData` with `MarketDataIngester` instance
- Replace `_StubBroker` with `ZerodhaBroker` instance (falls back to paper mode)
- Replace `_StubLLM` with `GeminiLLM` instance
- Stubs remain as fallbacks when API keys are not configured

---

## Step 7: Tests

- `tests/test_db.py` — Database init, WAL mode, CRUD for ohlcv/watchlist, upsert idempotency, migration runner (apply v1, verify tables, apply v2, verify upgrade)
- `tests/test_ingester.py` — Fallback chain (mock providers), staleness rejection, data quality validation, rate limiting. Include one integration test: primary fails → fallback → DB write → read back.
- `tests/test_features.py` — Each indicator against known computed values
- `tests/test_jugaad.py` / `test_yfinance.py` / `test_tvfeed.py` — Unit tests with mocked HTTP (no real API calls)
- `tests/test_zerodha.py` — Mocked Kite API: auth flow, place_order, rate limiting, paper mode simulation
- `tests/test_gemini.py` — Mocked Gemini API: all 7 methods, JSON parsing, error handling, retry on rate limit

---

## Step 8: Config + pyproject.toml updates

- Add `database.path` to `DatabaseConfig`
- Fix Q2: `requires-python = ">=3.12"` in pyproject.toml
- Add dependencies (with version ranges):
  - `jugaad-data>=0.3,<1`
  - `yfinance>=0.2,<1`
  - `tvdatafeed>=2.1,<3`
  - `pandas>=2.0,<3` (required by ta/yfinance)
  - `ta>=0.11,<1` (technical analysis library)
  - `kiteconnect>=5.0,<6`
  - `google-genai>=1.0,<2`

---

## Deliverables (files to create/modify)

### New files:
1. `src/yolovest/data/db.py` — SQLite database layer + migration runner
2. `src/yolovest/data/ingester.py` — Fallback chain orchestrator
3. `src/yolovest/data/jugaad.py` — jugaad-data provider
4. `src/yolovest/data/yfinance_provider.py` — yfinance provider
5. `src/yolovest/data/tvfeed.py` — tvDatafeed provider
6. `src/yolovest/data/features.py` — Feature engineering
7. `src/yolovest/broker/zerodha.py` — Zerodha Kite Connect broker
8. `src/yolovest/llm/gemini.py` — Google Gemini LLM
9. `migrations/001_initial.sql` — Initial database schema
10. `tests/test_db.py` — Database + migration tests
11. `tests/test_ingester.py` — Ingester/fallback tests
12. `tests/test_features.py` — Feature engineering tests
13. `tests/test_zerodha.py` — Broker tests
14. `tests/test_gemini.py` — LLM tests

### Modified files:
1. `src/yolovest/config.py` — Add `database.path`
2. `src/yolovest/context.py` — Expand `DatabaseProtocol`
3. `src/yolovest/main.py` — Wire up all concrete implementations
4. `pyproject.toml` — Fix python version, add deps
5. `src/yolovest/data/__init__.py` — Public exports

---

## Implementation Order

1. `pyproject.toml` + `config.py` changes (deps, DatabaseConfig.path)
2. `migrations/001_initial.sql` + `data/db.py` (migration runner + Database class) + `test_db.py`
3. `data/jugaad.py` + `data/yfinance_provider.py` + `data/tvfeed.py` (providers)
4. `data/ingester.py` (fallback chain + rate limiting) + `test_ingester.py`
5. `data/features.py` (indicators) + `test_features.py`
6. `broker/zerodha.py` + `test_zerodha.py`
7. `llm/gemini.py` + `test_gemini.py`
8. `context.py` (expand protocols) + `main.py` (wiring) + `data/__init__.py`
9. Run all tests, commit, push
