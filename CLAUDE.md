# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is a fully autonomous AI-driven Indian stock trading platform. It uses OpenClaw for agent orchestration, Google Gemini for LLM reasoning, XGBoost/LightGBM for ML signals, and Zerodha Kite Connect (free tier) for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed).

**Current state:** Phase 0 and Phase 1 complete. Phase 2 (Intelligence Layer) is next. See `plan.md` for implementation plans and `docs/` for review reports.

## What's Built

### Phase 0 — Skill Infrastructure (Complete, TL-approved)

- **Config system** (`config.py`) — Nested Pydantic v2 models, env var expansion, all Section 10 config keys, time-safe validators
- **Schemas** (`models/schemas.py`) — All 9 inter-skill Pydantic contracts + 5 LLM output types
- **Context** (`context.py`) — `AppContext` dataclass with Protocol types, `MarketHoursChecker` with timezone-aware checks (ZoneInfo)
- **Orchestrator** (`orchestrator.py`) — Full heartbeat pipeline with FR-1.3 error propagation, mutex (skip-on-overrun), consecutive skip alerting
- **Event bus** (`events.py`) — Async pub/sub for inter-skill communication
- **Notifier** (`notify.py`) — Console backend + Telegram-ready interface
- **ABCs** — `BrokerBase` (7 methods), `LLMBase` (7 methods), `MarketDataBase` (3 methods)
- **Skill stubs** — All 15 skills registered in `SKILL_REGISTRY` with documented flows

### Phase 1 — Foundation & Data Pipeline (Complete)

- **Database** (`data/db.py`) — SQLite with WAL mode, aiosqlite, migration runner with numbered SQL files (`migrations/`). 11 tables: ohlcv, watchlist, trades, signals, predictions, sentiment, premarket, system_state, llm_reviews, audit_log, schema_version
- **Market data providers** — `JugaadDataProvider` (primary daily/EOD), `YFinanceProvider` (fallback daily), `TVDatafeedProvider` (intraday 5min/15min)
- **Fallback chain** (`data/ingester.py`) — `MarketDataIngester` with automatic failover, staleness validation (FR-10.4), data quality checks, per-provider rate limiting
- **Feature engineering** (`data/features.py`) — All FR-4.1 indicators: RSI, MACD, Bollinger Bands, VWAP, ATR, OBV, SuperTrend, Volume Profile, EMA. Pure functions, toggleable via config
- **Zerodha broker** (`broker/zerodha.py`) — Full `BrokerBase` implementation. Paper mode with simulated slippage (FR-6.2). Rate limiter (FR-6.8), retry with backoff (FR-6.6), Kite auth flow (FR-6.3)
- **Gemini LLM** (`llm/gemini.py`) — All 7 `LLMBase` methods. Structured JSON output. Pro model for complex analysis, Flash for routine checks (NFR-6). Retry with exponential backoff
- **Wiring** — `main.py` creates real implementations when API keys are configured, falls back to stubs otherwise
- **Tests** — 230 passing (89 new: db, ingester, features, broker, LLM)

### Phase 2 — Intelligence Layer (Complete)

- **News module** (`news/`) — `NewsSource` ABC, `NewsAggregator` with SHA256 dedup (FR-2.13). Concrete scrapers: MoneyControl RSS, ET Markets RSS, LiveMint RSS, NSE Official (stub). Google Finance (P1)
- **Strategy module** (`strategy/`) — `MLBase` ABC, `XGBoostSignalModel` with Platt scaling calibration, `Backtester` with walk-forward validation and transaction cost modeling (FR-9.2)
- **Skills implemented** — `ingest-premarket` (GIFT Nifty, US/Asian/commodities via yfinance), `ingest-data` (OHLCV + news + sentiment), `market-scan` (weighted scoring with normalization, sector rotation), `generate-signals` (ML inference, confidence filtering), `model-retrain` (versioning, shadow mode, LLM failure analysis)
- **Database extensions** — Migration 002: news_articles, fundamentals, model_versions, failure_analyses tables + trades.status index. 14 new DB methods
- **Schemas** — `NewsArticle` (auto-hash), `MLPrediction`, `BacktestResult` added
- **Config** — Added `scanning.universe`, `strategy.min_training_samples`
- **SuperTrend** — Upgraded from simplified single-bar to full multi-bar with band carryover
- **Context** — Added `MLProtocol` to `AppContext`
- **Tests** — 322 passing (67 new: news scrapers, strategy/backtester, DB Phase 2 methods)

## Architecture

### Three Abstraction Layers (ABCs)

- **`BrokerBase`** (`broker/base.py`) → `ZerodhaBroker` — execution only (free tier, no market data)
- **`LLMBase`** (`llm/base.py`) → `GeminiLLM` — 7 methods: `ping`, `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`
- **`MarketDataBase`** (`data/base.py`) → `JugaadDataProvider` (primary) → `YFinanceProvider` (fallback) → `TVDatafeedProvider` (intraday)

### Skill System

15 skills extend `SkillBase` (in `src/yolovest/skills/base.py`). Each skill has:
- `async execute(**kwargs) -> SkillResult`
- `should_run() -> bool`
- A trigger type: `HEARTBEAT`, `CRON`, `EVENT`, or `MANUAL`
- Access to shared context via `self.ctx` (config, db, broker, llm, market_data, etc.)

Skills are registered in `SKILL_REGISTRY` dict in `src/yolovest/skills/__init__.py`.

### Heartbeat Pipeline (market hours, every 15min)

```
health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → trade-execute → predict-track
  → position-monitor
```

Error propagation: if `ingest-data` fails, skip scan+signals but always run `position-monitor`. If `health-check` fails, abort entire heartbeat. Full policy table in REQUIREMENTS.md FR-1.3.

### Inter-Skill Data Contracts

All data exchange between skills uses typed Pydantic models in `src/yolovest/models/schemas.py`: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`.

## Key Files

- **`REQUIREMENTS.md`** — Complete specification (950+ lines). Sections 1-11 are functional requirements, Section 13 is the cross-functional review (CEO/PM/BA findings), Section 14 is the glossary. Always consult this before making architectural decisions.
- **`plan.md`** — Phase 2 implementation plan (v2, PM-approved)
- **`docs/tl_phase0_review.md`** — TL review of Phase 0 (all issues resolved)
- **`docs/pm_phase1_review.md`** — PM cross-check of Phase 1 plan
- **`src/yolovest/orchestrator.py`** — Heartbeat pipeline, error propagation, mutex
- **`src/yolovest/context.py`** — `AppContext`, Protocol types (DatabaseProtocol, BrokerProtocol, LLMProtocol, MarketDataProtocol, NotifierProtocol), `MarketHoursChecker`
- **`src/yolovest/config.py`** — All config models with validators
- **`src/yolovest/models/schemas.py`** — All Pydantic data contracts
- **`src/yolovest/data/db.py`** — SQLite database layer + migration runner
- **`src/yolovest/data/ingester.py`** — Fallback chain orchestrator for market data
- **`src/yolovest/data/features.py`** — Technical indicator computation (pure functions)
- **`src/yolovest/broker/zerodha.py`** — Zerodha Kite Connect broker (paper + live)
- **`src/yolovest/llm/gemini.py`** — Google Gemini LLM (all 7 methods)
- **`migrations/001_initial.sql`** — Initial database schema (11 tables)
- **`migrations/002_phase2_extensions.sql`** — Phase 2 tables (news_articles, fundamentals, model_versions, failure_analyses)
- **`src/yolovest/news/`** — News scrapers (MoneyControl, ET Markets, LiveMint RSS) + aggregator with dedup
- **`src/yolovest/strategy/ml_signal.py`** — XGBoost/LightGBM model for signal generation
- **`src/yolovest/strategy/backtest.py`** — Walk-forward backtesting engine with transaction costs

## Implementation Phases

- **Phase 0** (complete): Skill infrastructure — context, schemas, orchestrator, event bus, heartbeat, config, ABCs, Telegram
- **Phase 1** (complete): Database + migrations, market data providers with fallback chain, feature engineering, Zerodha broker, Gemini LLM
- **Phase 2** (complete): Intelligence — news aggregation + dedup, sentiment analysis, dynamic scanner with weighted scoring, ML signal models (XGBoost), backtesting engine, model retraining with shadow mode
- Phase 3 (next): Risk & execution — risk manager, LLM trade review gate, order executor, position tracking
- Phase 4: Self-learning — prediction tracking, model retraining, A/B testing
- Phase 5: Dashboard & reporting

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL-M** = Stop-Loss Market order. **GIFT Nifty** = offshore Nifty futures (pre-market indicator).
- Market hours: 9:15 AM - 3:30 PM IST. Square-off at 3:15 PM. ~15 NSE holidays/year.
- Kite API rate limit: 10 req/s aggregate. Daily re-auth required (user pastes request_token via Telegram).
- All risk parameters are configurable via `config.yaml` under `risk.*` section. See REQUIREMENTS.md Section 10.

## Conventions

- Python 3.12+, async throughout
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`
- Config via YAML + Pydantic validation. Secrets via environment variables (never in config files).
- SQLite with WAL mode. Schema versioned via numbered migration scripts in `migrations/`.
- Paper trading mode by default — live trading requires explicit `mode: live` in config.
- Tests use pytest-asyncio with `asyncio_mode = "auto"`. Run with `python -m pytest tests/ -v`.
