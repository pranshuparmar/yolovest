# Phase 2: Intelligence Layer — Implementation Plan

**v2 — Updated after PM review (docs/pm_phase2_review.md)**

## Scope (from REQUIREMENTS.md)

Phase 2 implements the intelligence pipeline that feeds signals into the trading engine:
1. Pre-market data ingestion — GIFT Nifty, US/Asian markets, commodities (FR-2.10, FR-2.11)
2. News aggregation + deduplication — MoneyControl, ET Markets, LiveMint, NSE official (FR-2.2–2.5, FR-2.12–2.13)
3. Dynamic stock scanning + ranking — weighted scoring, sector rotation, LLM cross-validation (FR-3.1–3.6)
4. ML signal generation — XGBoost/LightGBM models for intraday + swing (FR-4.1–4.5)
5. Backtesting engine — walk-forward validation, Sharpe/drawdown metrics (FR-4.6–4.8)
6. Model retraining — versioning, shadow mode, A/B testing, LLM failure analysis (FR-7.4–7.7)

7. Economic calendar events — RBI, Fed, GDP, earnings dates (FR-2.6)
8. Bhavcopy seed data import for deep historical backtesting (FR-2.1d)

**Deferred to Phase 3:** Risk management (FR-5), order execution (FR-6), position monitoring. These consume signals produced by Phase 2.

**Deferred to Phase 4:** Prediction tracking outcome scoring (FR-7.2–7.3), full self-learning loop.

## PM Review Changes (v2)

Incorporating PM review feedback (docs/pm_phase2_review.md):
- G1: Added FR-2.6 (economic calendar) as P1 step
- G2: Added FR-2.1d (Bhavcopy seed data import) as sub-task
- G3: Clarified FR-3.4 — market-scan re-scores on every heartbeat
- G4: Added FR-9.2 transaction cost modeling to backtester
- G5: Added minimum training data guard (200 samples)
- G6: Added confidence score calibration (Platt scaling)
- G7: Added sub-score normalization to [0,1] before weighting
- Structural: ML code in `strategy/` not `ml/` per spec Section 9
- Config: Added `scanning.universe`, `strategy.min_training_samples`
- TL rec #7: Use Gemini native `google_search` tool for web grounding

## TL Phase 1 Review Items Addressed First

All TL Phase 1 review issues (M1, M2, m1–m6) have been resolved. Phase 2 benefits from:
- Atomic migrations (M1 fix) — safe for Phase 2's new migration files
- JSON error handling in GeminiLLM (m6 fix) — robust for heavy LLM use in sentiment/scanning
- IST-aware timestamps (m1, m5 fixes) — consistent across all providers
- Correct volume data (m4 fix) — accurate volume-based indicators

---

## Implementation Order

### Step 1: News & Data Source Abstractions

**New module: `src/yolovest/news/`**

#### 1a. News scraper base class (`news/base.py`)

```python
class NewsSource(ABC):
    """Base class for all news/data scrapers."""

    @abstractmethod
    async def fetch_headlines(self, symbols: list[str]) -> list[NewsArticle]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...
```

**Data contract — add to `models/schemas.py`:**
```python
class NewsArticle(BaseModel):
    headline: str
    source: str                    # "moneycontrol", "et_markets", "livemint"
    url: str | None = None
    symbols: list[str] = []        # stocks mentioned
    published_at: datetime | None = None
    content_hash: str              # SHA256 of headline for dedup (FR-2.13)
```

#### 1b. Concrete scrapers

| File | Source | Data | Priority |
|------|--------|------|----------|
| `news/moneycontrol.py` | MoneyControl RSS/web | Stock news, analyst ratings | P0 |
| `news/et_markets.py` | Economic Times | Market headlines, bulk deals | P0 |
| `news/livemint.py` | LiveMint | Stock news, market commentary | P1 |
| `news/nse_official.py` | NSE website | Corp announcements, bulk/block deals, FII/DII, delivery % | P0 |
| `news/google_finance.py` | Google Finance | Global cues, broader sentiment | P1 |

Each scraper:
- Uses `aiohttp` or `asyncio.to_thread(requests.get, ...)` with rate limiting
- Returns `list[NewsArticle]`
- Handles failures gracefully (log + return empty)
- Has a semaphore for concurrent request limiting

#### 1c. News aggregator (`news/aggregator.py`)

```python
class NewsAggregator:
    """Fetches from all configured sources, deduplicates, returns merged list."""

    def __init__(self, sources: list[NewsSource]): ...
    async def fetch_all(self, symbols: list[str]) -> list[NewsArticle]: ...
    def deduplicate(self, articles: list[NewsArticle]) -> list[NewsArticle]: ...
```

Dedup strategy (FR-2.13): hash headlines → merge by `content_hash` → keep earliest, track all sources.

**Tests:** `tests/test_news_scrapers.py` — mocked HTTP responses per scraper, dedup logic.

**Dependencies:** `aiohttp`, `beautifulsoup4`, `feedparser`

---

### Step 2: Pre-Market Ingestion (`ingest-premarket` skill)

Implement the 4 stubbed helper methods in `skills/ingest_premarket.py`:

#### 2a. `_fetch_gift_nifty()`
- Use yfinance: `^NSEI` or Singapore Nifty futures
- Return: `{"value": float, "change_pct": float, "previous_close": float}`

#### 2b. `_fetch_us_markets()`
- Use yfinance: `^GSPC` (S&P 500), `^IXIC` (NASDAQ), `^DJI` (Dow)
- Return: `{"sp500_change_pct": float, "nasdaq_change_pct": float, "dow_change_pct": float}`

#### 2c. `_fetch_asian_markets()`
- Use yfinance: `^N225` (Nikkei), `^HSI` (Hang Seng), `000001.SS` (Shanghai)
- Return: `{"nikkei_change_pct": float, "hang_seng_change_pct": float, "shanghai_change_pct": float}`

#### 2d. `_fetch_commodities()`
- Use yfinance: `CL=F` (crude oil), `GC=F` (gold), `USDINR=X` (rupee)
- Return: `{"crude_oil_usd": float, "gold_usd": float, "usdinr": float}`

All fetchers: `asyncio.to_thread(yfinance.Ticker(...).fast_info)`, with error handling.

#### 2e. Fix `execute()` wiring
- The skill calls `self.ctx.llm.summarize_with_web_grounding(...)` — this already works.
- Need `db.upsert_premarket()` method (see Step 5).

**Tests:** `tests/test_ingest_premarket.py` — mocked yfinance, mocked LLM, verify DB persistence.

---

### Step 3: Data Ingestion Skill (`ingest-data` skill)

Implement the 5 stubbed methods in `skills/ingest_data.py`:

#### 3a. `_fetch_nse_data()`
- Use `news/nse_official.py` scraper for corp announcements, bulk/block deals
- Fetch FII/DII daily activity from NSE website
- Fetch delivery % per symbol
- Return: `{"corp_actions": [...], "bulk_deals": [...], "fii_dii": {...}, "delivery": {symbol: pct}}`

#### 3b. `_fetch_all_news(symbols)`
- Use `NewsAggregator` (from Step 1c) to fetch from all configured sources
- Return: `list[NewsArticle]`

#### 3c. `_deduplicate_news(articles)`
- Delegate to `NewsAggregator.deduplicate()`
- Group by `content_hash`, merge sources

#### 3d. `_fetch_fundamentals(symbols)`
- P1 priority — can start with empty/stub and backfill
- Eventually: scrape Screener.in for PE, PB, debt ratio, promoter holding
- Persist to DB for use by market-scan scoring

#### 3e. `_fetch_technicals(symbols)`
- P1 priority — can start with empty/stub and backfill
- Eventually: Trendlyne momentum scores, volume breakout alerts
- Persist to DB

#### 3f. Fix `execute()` wiring
- Replace `self.ctx.market_data.fetch_daily/fetch_intraday` with actual `MarketDataProtocol.get_ohlcv()` calls
- Wire news aggregator via AppContext

**Tests:** `tests/test_ingest_data.py` — mocked providers, mocked news, verify DB round-trip.

---

### Step 4: Market Scanner (`market-scan` skill)

Implement the 2 stubbed methods + refine scoring:

#### 4a. `_apply_exclusion_filters(stocks)`
- Filter stocks in F&O ban list (fetch from NSE, cache daily)
- Filter stocks with pending corporate actions (from NSE data ingested in Step 3)
- Filter stocks below minimum price threshold (penny stock filter)
- Return filtered list

#### 4b. `_analyze_sector_rotation(scored_stocks)`
- Group stocks by `sector` field
- Compute per-sector: avg composite score, count of stocks, top stock
- Classify sectors as "strong" (avg score > P75) or "weak" (avg score < P25)
- Return: `{"strong": ["IT", "Banks"], "weak": ["Metals"], "rotation": {...}}`

#### 4c. Scoring refinement
- The scoring formula in `execute()` already works: `technical * 0.4 + volume_momentum * 0.25 + news_sentiment * 0.20 + fundamental * 0.15`
- Need: individual sub-score computation from raw data
- Technical score: compute from RSI, MACD signal, EMA alignment, SuperTrend direction
- Volume score: relative volume vs 20-day avg, delivery % trend
- Sentiment score: map `SentimentResult.sentiment` → 0.8 (bullish), 0.5 (neutral), 0.2 (bearish), weight by confidence
- Fundamental score: inverse PE rank, low debt, high promoter holding

#### 4d. DB method: `get_nse_universe()`
- Query OHLCV + sentiment + watchlist tables
- Return list of dicts with all sub-scores pre-computed
- Must have: symbol, sector, avg_daily_volume, technical_score, volume_momentum_score, news_sentiment_score, fundamental_score

**Tests:** `tests/test_market_scan.py` — scoring logic, sector rotation, exclusion filters, LLM cross-validation.

---

### Step 5: Database Extensions

**New migration: `migrations/002_phase2_extensions.sql`**

```sql
-- News articles table for dedup tracking
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

-- Fundamental data cache
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

-- Model versions tracking
CREATE TABLE IF NOT EXISTS model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT NOT NULL,  -- 'intraday' or 'swing'
    version TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sharpe_ratio REAL,
    max_drawdown_pct REAL,
    win_rate REAL,
    profit_factor REAL,
    status TEXT NOT NULL DEFAULT 'shadow',  -- 'shadow', 'production', 'retired'
    shadow_start_date TEXT,
    promoted_date TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_model_type_status ON model_versions(model_type, status);

-- Failure analysis from LLM
CREATE TABLE IF NOT EXISTS failure_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type TEXT,
    patterns TEXT,       -- JSON
    recommendations TEXT, -- JSON
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Add index on trades.status for get_open_positions (TL nit n1)
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
```

**New DB methods in `data/db.py`:**
- `upsert_premarket(data: dict) -> None`
- `get_latest_premarket() -> dict`
- `upsert_sentiment(symbol: str, result: SentimentResult) -> None`
- `get_sentiment(symbol: str) -> SentimentResult | None`
- `upsert_news_articles(articles: list[NewsArticle]) -> int`
- `get_nse_universe() -> list[dict]`
- `insert_signal(signal: dict) -> None`
- `upsert_fundamentals(symbol: str, data: dict) -> None`
- `get_training_dataset() -> dict`
- `get_prediction_outcomes() -> list[dict]`
- `store_failure_analysis(analysis) -> None`
- `save_model_version(model_type, version, path, metrics) -> None`
- `get_production_model(model_type) -> dict | None`
- `promote_model(model_type, version) -> None`

---

### Step 6: ML Protocol & Model System

**New module: `src/yolovest/strategy/` (per spec Section 9)**

#### 6a. ML base class (`strategy/ml_base.py`)

```python
class MLBase(ABC):
    """Abstract ML model interface for signal generation."""

    @abstractmethod
    async def predict_intraday(self, symbol: str, features: dict) -> MLPrediction: ...

    @abstractmethod
    async def predict_swing(self, symbol: str, features: dict) -> MLPrediction: ...

    @abstractmethod
    async def train(self, model_type: str, X, y, params: dict) -> dict: ...

    @abstractmethod
    async def save_model(self, model_type: str, metrics: dict) -> str: ...

    @abstractmethod
    async def load_model(self, model_type: str, version: str | None = None) -> None: ...

    @abstractmethod
    async def get_production_metrics(self, model_type: str) -> dict: ...

    @abstractmethod
    async def deploy_shadow(self, model_type: str, version: str, days: int) -> None: ...
```

**Data contract — add to `models/schemas.py`:**
```python
class MLPrediction(BaseModel):
    signal_type: Literal["BUY", "SELL", "HOLD"]
    entry_price: float
    target_price: float
    stop_loss_price: float
    position_size: int
    holding_period: str
    confidence: float  # 0.0 to 1.0
    model_version: str
```

#### 6b. XGBoost/LightGBM implementation (`strategy/ml_signal.py`)

- Load/save models via joblib
- Feature vector construction from `features.py` output
- Separate intraday vs swing models
- Walk-forward train/test split for validation
- ATR-based target/SL computation
- Confidence calibration via Platt scaling (PM G6)

#### 6c. Backtesting engine (`strategy/backtest.py`)

```python
class Backtester:
    """Walk-forward backtesting with simulated execution."""

    def run(self, model, data, config) -> BacktestResult: ...
```

**BacktestResult schema:**
```python
class BacktestResult(BaseModel):
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    total_trades: int
    total_return_pct: float
    trade_log: list[dict] = []
```

Validation gates (FR-4.6): only deploy if `sharpe >= config.strategy.backtest_min_sharpe` and `drawdown <= config.strategy.backtest_max_drawdown_pct`.

Transaction cost modeling (PM G4 / FR-9.2): include brokerage (₹20 or 0.03%), STT, stamp duty, GST, exchange fees in all PnL calculations. ~0.1% round-trip intraday, ~0.15% delivery.

#### 6d. Add `MLProtocol` to `context.py` and wire in `main.py`

**Tests:** `tests/test_ml_model.py`, `tests/test_backtester.py`

---

### Step 7: Signal Generation Skill (`generate-signals`)

#### 7a. Implement `_should_use_intraday_model()`
- Before 14:00 IST → intraday model (MIS trades need time to play out)
- After 14:00 IST or `config.strategy.default_trade_type == "swing"` → swing model
- Return bool

#### 7b. Fix `execute()` wiring
- Replace `self.ctx.features.compute(...)` with actual feature computation using `data/features.py`
- Replace `self.ctx.ml.predict_intraday/predict_swing` with `MLProtocol` calls
- Replace `self.ctx.db.insert_signal(...)` with actual DB method

#### 7c. Add `FeaturesProtocol` to context (or use features directly)
- Wrap `data/features.py` pure functions into a service class
- Load OHLCV from DB, compute indicators, return feature dict

**Tests:** `tests/test_generate_signals.py` — mocked model, verify confidence filtering, signal schema.

---

### Step 8: Model Retraining Skill (`model-retrain`)

#### 8a. Implement `_retrain_model(model_type, data)`
- Split data with walk-forward validation
- Train XGBoost on train set, evaluate on test set
- Compute metrics: Sharpe, drawdown, win rate, profit factor
- Return metrics dict

#### 8b. Implement `_check_shadow_promotions()`
- Query `model_versions` table for shadow models past `shadow_mode_days`
- Compare shadow period performance vs production
- Promote if better, retire if worse
- Return list of promotion/rollback actions

**Tests:** `tests/test_model_retrain.py` — mocked models, versioning, shadow promotion logic.

---

### Step 9: SuperTrend Full Implementation (TL rec #6)

Upgrade `data/features.py` `compute_supertrend()` from simplified single-bar to full multi-bar implementation with band carryover across the bar series.

**Tests:** Update existing SuperTrend tests in `tests/test_features.py`.

---

## New Dependencies

Add to `pyproject.toml`:
```toml
dependencies = [
    # ... existing ...
    "xgboost>=2.0",
    "scikit-learn>=1.4",
    "aiohttp>=3.9",
    "beautifulsoup4>=4.12",
    "feedparser>=6.0",
    "joblib>=1.3",
]
```

## File Summary

**New files (18):**
- `src/yolovest/news/__init__.py`
- `src/yolovest/news/base.py` — NewsSource ABC
- `src/yolovest/news/aggregator.py` — dedup + merge
- `src/yolovest/news/moneycontrol.py`
- `src/yolovest/news/et_markets.py`
- `src/yolovest/news/livemint.py`
- `src/yolovest/news/nse_official.py`
- `src/yolovest/strategy/__init__.py`
- `src/yolovest/strategy/ml_base.py` — MLBase ABC
- `src/yolovest/strategy/ml_signal.py` — XGBoost/LightGBM impl
- `src/yolovest/strategy/backtest.py` — walk-forward backtesting
- `src/yolovest/data/bhavcopy.py` — NSE Bhavcopy CSV importer (FR-2.1d)
- `migrations/002_phase2_extensions.sql`
- `tests/test_news_scrapers.py`
- `tests/test_ingest_premarket.py`
- `tests/test_ml_model.py`
- `tests/test_backtester.py`

**Modified files (10):**
- `src/yolovest/models/schemas.py` — add NewsArticle, MLPrediction, BacktestResult
- `src/yolovest/context.py` — add MLProtocol
- `src/yolovest/config.py` — add ScanningConfig.universe, StrategyConfig.min_training_samples
- `src/yolovest/data/db.py` — add Phase 2 DB methods
- `src/yolovest/data/features.py` — full SuperTrend
- `src/yolovest/main.py` — wire ML provider, news aggregator
- `src/yolovest/skills/ingest_data.py` — implement stubbed methods
- `src/yolovest/skills/ingest_premarket.py` — implement stubbed methods
- `src/yolovest/skills/market_scan.py` — implement stubbed methods
- `src/yolovest/skills/generate_signals.py` — implement stubbed methods
- `src/yolovest/skills/model_retrain.py` — implement stubbed methods
- `pyproject.toml` — new dependencies

## Implementation Priority

**Phase 2a (P0 — minimum for first signal):**
Steps 1a-1c, 2, 5 (migration + DB methods), 6a-6b, 7, 9

**Phase 2b (P0 — full scanning):**
Steps 3, 4

**Phase 2c (P1 — backtesting + retraining):**
Steps 6c, 8

**Phase 2d (P1 — additional data sources):**
Steps 3d, 3e (fundamentals, technicals scrapers)

## Test Target

Each step includes tests. Target: 80+ new tests covering all Phase 2 components. All existing 255 tests must continue passing.
