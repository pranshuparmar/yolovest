# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is a fully autonomous AI-driven Indian stock trading platform. It uses OpenClaw for agent orchestration, Google Gemini for LLM reasoning, XGBoost/LightGBM for ML signals, and Zerodha Kite Connect (free tier) for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed).

**Current state:** All 6 phases (0–5) complete. The platform is fully implemented. See `plan.md` for implementation plans and `docs/` for review reports.

## What's Built

### Phase 0 — Skill Infrastructure (Complete, TL-approved)

- **Config system** (`config.py`) — Nested Pydantic v2 models, env var expansion, all Section 10 config keys, time-safe validators
- **Schemas** (`models/schemas.py`) — All 9 inter-skill Pydantic contracts + 5 LLM output types
- **Context** (`context.py`) — `AppContext` dataclass with Protocol types, `MarketHoursChecker` with timezone-aware checks (ZoneInfo)
- **Orchestrator** (`orchestrator.py`) — Full heartbeat pipeline with FR-1.3 error propagation, mutex (skip-on-overrun), consecutive skip alerting
- **Event bus** (`events.py`) — Async pub/sub for inter-skill communication
- **Notifier** (`notify.py`) — Console backend + Telegram-ready interface
- **ABCs** — `BrokerBase` (8 methods incl. `modify_sl_order`), `LLMBase` (7 methods), `MarketDataBase` (3 methods + `get_ltp`)
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

### Phase 3 — Risk & Execution (Complete)

- **Risk check** (`skills/risk_check.py`) — Full FR-5.1 to FR-5.18: kill switch, market hours enforcement, daily/weekly circuit breakers with sizing reduction, max positions, portfolio/single-stock exposure caps, sector correlation limits, mandatory stop-loss validation, ATR-based position sizing
- **LLM review** (`skills/llm_review.py`) — Gemini trade approval gate (FR-4.4, FR-5.11). Full context assembly (signal + sentiment + portfolio + premarket + sector rotation). APPROVE/REJECT/RESIZE decisions. Fallback to rules-only when LLM unavailable (FR-5.12)
- **Trade execution** (`skills/trade_execute.py`) — Paper mode with configurable simulated slippage (FR-6.2). Live mode with primary + SL order placement, retry with exponential backoff (FR-6.6), slippage tracking (FR-6.7)
- **Position monitor** (`skills/position_monitor.py`) — Broker-local position reconciliation (FR-6.5). Target/SL hit detection. Trailing stop-loss with configurable trigger multiple and step size (FR-5.8). Unrealized PnL tracking
- **Square-off** (`skills/square_off.py`) — Auto close MIS positions at EOD (FR-5.10). Force mode for kill switch. SL order cancellation before exit. PnL computation and Telegram notification
- **Database** — 10 new methods: `get_portfolio_state`, `get_stock_sector`, `log_llm_review`, `get_sector_rotation`, `get_todays_trades`, `get_latest_sentiment`, `insert_trade`, `update_position_sl`, `update_unrealized_pnl`, `close_position`
- **Protocols** — Extended `DatabaseProtocol` (10 new methods), `BrokerProtocol` (`modify_sl_order`), `MarketDataProtocol` (`get_ltp`), `NotifierProtocol` (`send_trade_alert`)
- **MarketHoursChecker** — Added `is_square_off_window()` with extension support
- **Notifier** — Added `send_trade_alert()` for trade entry/exit alerts
- **MarketDataIngester** — Added `get_ltp()` for last traded price via quote fallback chain
- **ZerodhaBroker** — Added `modify_sl_order()` for trailing SL updates (paper + live)
- **Tests** — 393 passing (67 new: risk-check, llm-review, trade-execute, position-monitor, square-off, DB Phase 3 methods)

### Phase 4 — Self-Learning & Reporting (Complete)

- **Predict-track** (`skills/predict_track.py`) — Dual-mode skill: "log" mode records predictions with trade linkage (FR-7.1), "score" mode evaluates elapsed predictions against actual prices (FR-7.2). Graceful fallback from LTP to OHLCV close for price lookups
- **Prediction scoreboard** — Aggregated accuracy stats by symbol, model version, and overall (FR-7.3). Auto-refreshed after scoring. Stored in `prediction_scoreboard` table
- **Shadow model promotion** (`skills/model_retrain.py`) — Full A/B testing lifecycle (FR-7.5): shadow models that complete trial period are compared on Sharpe ratio, promoted if improved, retired if not. Auto-loads promoted model
- **Report generation** (`skills/report_generate.py`) — Daily reports (FR-8.4): trades, PnL, win rate, slippage, prediction accuracy, Gemini market summary. Weekly reports (FR-8.5): cumulative PnL, best/worst trades, LLM review accuracy (FR-7.8), prediction trends
- **Database** — Migration 003: prediction_scoreboard and reports tables. 12 new methods: `insert_prediction`, `get_unscored_predictions`, `score_prediction`, `refresh_prediction_scoreboard`, `get_prediction_scoreboard`, `get_todays_predictions`, `get_weekly_trades/predictions/llm_reviews`, `store_report`, `get_shadow_models_ready`, `retire_model`
- **Orchestrator** — Fixed predict-track invocation to pass `mode="log"` and `trade_id` linkage
- **Tests** — 432 passing (39 new: predict-track, report-generate, model-retrain shadow promotion, DB Phase 4 methods)

### Phase 5 — Dashboard & Reporting UI (Complete)

- **FastAPI dashboard** (`dashboard/app.py`) — Full REST API with basic password auth (FR-8.9). Endpoints: portfolio overview, open positions, today's trades, trade history with filters, equity curve, trade detail with reasoning chain, prediction scoreboard, historical reports, watchlist, sector rotation, audit log, health check
- **Trade detail view** (FR-8.3) — Full reasoning chain: signal → LLM review → prediction → audit trail, all linked by trade_id
- **WebSocket** (FR-8.2) — Real-time event broadcasting via `/ws` endpoint with connection manager
- **Historical reports** (FR-8.7) — Query reports by type, date range with parsed JSON content
- **Equity curve** (FR-8.1) — Daily cumulative PnL chart data computed from closed trades
- **Audit log API** (FR-8.8) — Filterable audit log access with action type filter
- **Database** — 5 new methods: `get_trades_history`, `get_equity_curve`, `get_trade_detail`, `get_reports_history`, `get_audit_log`
- **Config** — Added `dashboard.password` for basic auth
- **Wiring** — Dashboard starts as background task in `main.py`, disabled with `--no-dashboard`
- **Dependencies** — Added `fastapi`, `uvicorn`, `websockets` to pyproject.toml
- **Tests** — 459 passing (27 new: dashboard API endpoints with auth, DB Phase 5 methods)

### Telegram Bot & Docker Deployment

- **Telegram bot** (`telegram_bot.py`) — Full bot with python-telegram-bot: `/start`, `/status`, `/pnl`, `/positions`, `/stop`, `/kill`, `/resume` (FR-5.14), `/auth <token>` (FR-6.3). Runs as background long-polling task
- **Notifier integration** — `Notifier.set_telegram_bot()` wires bot for real message delivery. Per-alert-type toggle (FR-8.6). Graceful failure handling
- **Kill switch fix** — Updated to use `set_system_state("kill_switch", ...)` matching actual DB API
- **Broker login URL** — Added `get_login_url()` to `BrokerBase` and `ZerodhaBroker` for auth flow
- **Docker** — Multi-stage `Dockerfile` with non-root user, health check, volume mounts. `docker-compose.yml` with env var passthrough, named volumes for data/backups/models/logs, IST timezone
- **Tests** — 472 passing (13 new: Telegram bot, notifier integration, kill switch, broker login URL)

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
- **`src/yolovest/dashboard/app.py`** — FastAPI dashboard with 12 REST endpoints + WebSocket

## Implementation Phases

- **Phase 0** (complete): Skill infrastructure — context, schemas, orchestrator, event bus, heartbeat, config, ABCs, Telegram
- **Phase 1** (complete): Database + migrations, market data providers with fallback chain, feature engineering, Zerodha broker, Gemini LLM
- **Phase 2** (complete): Intelligence — news aggregation + dedup, sentiment analysis, dynamic scanner with weighted scoring, ML signal models (XGBoost), backtesting engine, model retraining with shadow mode
- **Phase 3** (complete): Risk & execution — risk manager (all FR-5 rules), LLM trade review gate, order executor (paper + live), position monitor with trailing SL, square-off
- **Phase 4** (complete): Self-learning & reporting — prediction tracking + scoring, prediction scoreboard, shadow model promotion/retirement, daily/weekly report generation with LLM review accuracy tracking
- **Phase 5** (complete): Dashboard & reporting UI — FastAPI web dashboard with basic auth, REST API (12 endpoints), WebSocket live updates, trade detail with reasoning chain, equity curve, historical reports, audit log

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL-M** = Stop-Loss Market order. **GIFT Nifty** = offshore Nifty futures (pre-market indicator).
- Market hours: 9:15 AM - 3:30 PM IST. Square-off at 3:15 PM. ~15 NSE holidays/year.
- Kite API rate limit: 10 req/s aggregate. Daily re-auth required (user pastes request_token via Telegram).
- All risk parameters are configurable via `config.yaml` under `risk.*` section. See REQUIREMENTS.md Section 10.

## Conventions

- Python 3.11+, async throughout
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`
- Config via YAML + Pydantic validation. Secrets via environment variables (never in config files).
- SQLite with WAL mode. Schema versioned via numbered migration scripts in `migrations/`.
- Paper trading mode by default — live trading requires explicit `mode: live` in config.
- Tests use pytest-asyncio with `asyncio_mode = "auto"`. Run with `python -m pytest tests/ -v`.
