# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is a fully autonomous AI-driven Indian stock trading platform. It uses Google Gemini for LLM reasoning, XGBoost for ML signals, and Zerodha Kite Connect for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed) with optional Kite data plan support.

**Current state:** Fully implemented and production-ready. All features complete. 671+ tests passing.

## Architecture

### Abstraction Layers (ABCs)

- **`BrokerBase`** (`broker/base.py`) → `ZerodhaBroker` — execution only (free tier), or with optional data plan
- **`LLMBase`** (`llm/base.py`) → `GeminiLLM` — 7 methods: `ping`, `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`
- **`MarketDataBase`** (`data/base.py`) → `JugaadDataProvider` (primary) → `YFinanceProvider` (fallback) → `TVDatafeedProvider` (intraday) → `KiteDataProvider` (optional paid plan)
- **`NewsSource`** (`news/base.py`) → MoneyControl, ET Markets, LiveMint (RSS), NSE Official (API), Google Finance (scraper)
- **`MLBase`** (`strategy/ml_base.py`) → `XGBoostSignalModel` with Platt scaling calibration

### Skill System

16 skills extend `SkillBase` (in `src/yolovest/skills/base.py`). Each skill has:
- `async execute(**kwargs) -> SkillResult`
- `should_run() -> bool`
- A trigger type: `HEARTBEAT`, `CRON`, `EVENT`, or `MANUAL`
- Access to shared context via `self.ctx` (config, db, broker, llm, market_data, news_aggregator, memory, etc.)

Skills are registered in `SKILL_REGISTRY` dict in `src/yolovest/skills/__init__.py`.

### Heartbeat Pipeline (market hours, every 15min)

```
health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → trade-execute → predict-track
  → position-monitor
```

Error propagation: if `ingest-data` fails, skip scan+signals but always run `position-monitor`. If `health-check` fails, abort entire heartbeat. Full policy table in REQUIREMENTS.md FR-1.3.

### AppContext

`AppContext` dataclass (`context.py`) holds references to all subsystems via Protocol types:
- `config`, `db`, `broker`, `llm`, `market_data`, `notify`, `market_hours`, `event_bus`
- `ml` (optional ML provider), `news_aggregator` (built at startup), `memory` (agent persistence)

### Inter-Skill Data Contracts

All data exchange between skills uses typed Pydantic models in `src/yolovest/models/schemas.py`: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`, `NewsArticle`, `MLPrediction`, `BacktestResult`.

## What's Implemented

### Data Pipeline
- **Market data**: JugaadDataProvider (NSE daily), YFinanceProvider (fallback), TVDatafeedProvider (intraday), KiteDataProvider (optional paid plan). Fallback chain with staleness validation and quality checks.
- **News**: NewsAggregator with 5 sources (MoneyControl, ET Markets, LiveMint RSS, NSE Official API, Google Finance scraper). SHA256 dedup. Wired into AppContext at startup.
- **Fundamentals**: Screener.in scraper (PE, PB, debt ratios, promoter holdings)
- **Technicals**: Trendlyne scraper (momentum scores, volume breakouts, DMA signals)
- **Economic calendar**: RBI MPC (primary, high impact), FOMC (secondary context, medium impact), NSE earnings dates. Dynamic year handling.
- **Feature engineering**: RSI, MACD, Bollinger Bands, VWAP, ATR, OBV, SuperTrend, Volume Profile, EMA. Pure functions, toggleable via config.
- **Bhavcopy importer**: NSE historical CSV import for backtesting seed data

### Intelligence Layer
- **ML signals**: XGBoost with Platt scaling, walk-forward backtesting, model versioning, shadow A/B testing, automatic promotion/retirement
- **Sentiment**: Gemini-powered sentiment analysis per symbol from aggregated news
- **Market scanning**: Weighted composite scoring (technical, volume, sentiment, fundamental) with sector rotation analysis and Gemini cross-validation

### Risk & Execution
- **Risk check**: Kill switch, market hours enforcement, daily/weekly circuit breakers, max positions, portfolio/single-stock exposure caps, sector correlation limits, mandatory SL validation, ATR-based position sizing, slippage feedback loop (FR-6.7), early close day handling (FR-11.2)
- **LLM review**: Gemini trade approval gate with full context. APPROVE/REJECT/RESIZE. Fallback to rules-only.
- **Trade execution**: Paper mode (simulated slippage) + live mode (Kite API). Returns trade_id for prediction linkage.
- **Position monitor**: Broker reconciliation, trailing SL, target/SL hit detection, unrealized PnL
- **Square-off**: Auto close MIS at EOD with transaction cost modeling. Respects early close days.

### Self-Learning & Reporting
- **Prediction tracking**: Log predictions with trade linkage, score against actuals, maintain scoreboard
- **Failure analysis**: Gemini analyzes prediction failures during scoring (5+ failures) and weekly retraining
- **LLM review accuracy**: Compares APPROVE/REJECT decisions vs actual trade PnL outcomes
- **Slippage stats**: Per-symbol slippage aggregation fed back into position sizing
- **Reports**: Daily (trades, PnL, win rate, slippage, predictions) and weekly (cumulative PnL, LLM accuracy, slippage trends, best/worst trades)

### Dashboard & Deployment
- **FastAPI dashboard**: 14 REST endpoints + WebSocket. Basic auth. Endpoints include `/api/slippage`, `/api/llm-accuracy`.
- **Telegram bot**: `/start`, `/status`, `/pnl`, `/positions`, `/stop`, `/kill`, `/resume`, `/auth`
- **Agent memory**: Cross-restart state persistence via `agent_memory` DB table with TTL support
- **Database maintenance**: CRON skill for daily backups, data retention cleanup (OHLCV, audit logs, predictions), old backup pruning
- **Docker**: Multi-stage Dockerfile, docker-compose with volume mounts, IST timezone

## Key Files

- **`REQUIREMENTS.md`** — Complete specification (950+ lines). Always consult before architectural decisions.
- **`src/yolovest/main.py`** — Entry point. Builds context, starts orchestrator, Telegram, dashboard.
- **`src/yolovest/orchestrator.py`** — Heartbeat pipeline, error propagation, mutex, memory persistence
- **`src/yolovest/context.py`** — `AppContext`, all Protocol types, `MarketHoursChecker` (early close aware)
- **`src/yolovest/config.py`** — All config models with validators
- **`src/yolovest/models/schemas.py`** — All Pydantic data contracts
- **`src/yolovest/data/db.py`** — SQLite database layer + migration runner + agent memory methods
- **`src/yolovest/data/ingester.py`** — Fallback chain orchestrator for market data
- **`src/yolovest/data/features.py`** — Technical indicator computation (pure functions)
- **`src/yolovest/data/google_finance.py`** — Google Finance scraper (Indian indices primary, global context secondary)
- **`src/yolovest/data/kite_data.py`** — Optional Kite Connect data provider (FR-2.1e)
- **`src/yolovest/data/economic_calendar.py`** — RBI MPC (primary) + FOMC (secondary context) + NSE earnings
- **`src/yolovest/broker/zerodha.py`** — Zerodha Kite Connect broker (paper + live)
- **`src/yolovest/llm/gemini.py`** — Google Gemini LLM (all 7 methods)
- **`src/yolovest/memory.py`** — Agent memory persistence (FR-1.5)
- **`src/yolovest/news/`** — News scrapers + aggregator with dedup
- **`src/yolovest/strategy/ml_signal.py`** — XGBoost model for signal generation
- **`src/yolovest/strategy/backtest.py`** — Walk-forward backtesting engine
- **`src/yolovest/dashboard/app.py`** — FastAPI dashboard (14 endpoints + WebSocket)
- **`migrations/`** — 5 numbered SQL migration files (001-005)
- **`config.example.yaml`** — Sample config with all keys documented

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL-M** = Stop-Loss Market order. **GIFT Nifty** = offshore Nifty futures (pre-market indicator).
- Market hours: 9:15 AM - 3:30 PM IST. Square-off at 3:15 PM. ~15 NSE holidays/year.
- Early close days configurable via `market_hours.early_close_days` in config.
- Kite API rate limit: 10 req/s aggregate. Daily re-auth required (user pastes request_token via Telegram).
- All risk parameters are configurable via `config.yaml` under `risk.*` section. See REQUIREMENTS.md Section 10.

## Conventions

- Python 3.11+, async throughout
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`
- Config via YAML + Pydantic validation. Secrets via environment variables (never in config files).
- SQLite with WAL mode. Schema versioned via numbered migration scripts in `migrations/`.
- Paper trading mode by default — live trading requires explicit `mode: live` in config.
- India-first design: RBI MPC is primary economic event; global indices tracked as secondary sentiment context only.
- Tests use pytest-asyncio with `asyncio_mode = "auto"`. Run with `PYTHONPATH=src python -m pytest tests/ -v`.
