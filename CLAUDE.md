# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is a fully autonomous AI-driven Indian stock trading platform. It uses Google Gemini for LLM reasoning, XGBoost for ML signals, and Zerodha Kite Connect for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed) with optional Kite data plan support.

**Current state:** Fully implemented and production-ready. All features complete.

## Project Structure

```
/
├── backend/          — Python backend (FastAPI + trading engine)
│   ├── src/yolovest/ — Main package
│   ├── tests/        — Tests (mirrors src/ structure)
│   ├── migrations/   — SQL migration files (001-009)
│   ├── pyproject.toml
│   ├── Dockerfile
│   └── config.example.yaml
├── frontend/         — React SPA (Vite + TypeScript + Tailwind)
│   ├── src/
│   ├── Dockerfile
│   ├── nginx.conf
│   └── package.json
├── docker-compose.yml
└── CLAUDE.md
```

## Architecture

### Abstraction Layers (ABCs)

- **`BrokerBase`** (`broker/base.py`) → `ZerodhaBroker` — execution only (free tier), or with optional data plan
- **`LLMBase`** (`llm/base.py`) → `GeminiLLM` — 7 methods: `ping`, `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`
- **`MarketDataBase`** (`data/base.py`) → `JugaadDataProvider` (primary) → `YFinanceProvider` (fallback) → `TVDatafeedProvider` (intraday) → `KiteDataProvider` (optional paid plan)
- **`NewsSource`** (`news/base.py`) → MoneyControl, ET Markets, LiveMint (RSS), NSE Official (API), Google Finance (scraper)
- **`MLBase`** (`strategy/ml_base.py`) → `XGBoostSignalModel` with Platt scaling calibration

### Skill System

16 skills extend `SkillBase` (in `backend/src/yolovest/skills/base.py`). Each skill has:
- `async execute(**kwargs) -> SkillResult`
- `should_run() -> bool`
- A trigger type: `HEARTBEAT`, `CRON`, `EVENT`, or `MANUAL`
- Access to shared context via `self.ctx` (config, db, broker, llm, market_data, news_aggregator, memory, etc.)

Skills are registered in `SKILL_REGISTRY` dict in `backend/src/yolovest/skills/__init__.py`.

### Heartbeat Pipeline (market hours, every 15min)

```
health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → trade-execute → predict-track
  → position-monitor
```

Error propagation: if `ingest-data` fails, skip scan+signals but always run `position-monitor`. If `health-check` fails, abort entire heartbeat.

### AppContext

`AppContext` dataclass (`context.py`) holds references to all subsystems via Protocol types:
- `config`, `db`, `broker`, `llm`, `market_data`, `notify`, `market_hours`, `event_bus`
- `ml` (optional ML provider), `news_aggregator` (built at startup), `memory` (agent persistence)

### Inter-Skill Data Contracts

All data exchange between skills uses typed Pydantic models in `backend/src/yolovest/models/schemas.py`: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`, `NewsArticle`, `MLPrediction`, `BacktestResult`.

## What's Implemented

### Data Pipeline
- **Market data**: JugaadDataProvider (NSE daily), YFinanceProvider (fallback), TVDatafeedProvider (intraday), KiteDataProvider (optional paid plan). Fallback chain with staleness validation and quality checks.
- **News**: NewsAggregator with 5 sources (MoneyControl, ET Markets, LiveMint RSS, NSE Official API, Google Finance scraper). SHA256 dedup. Toggleable via `market_data.news_enabled`.
- **Fundamentals**: Screener.in scraper (PE, PB, debt ratios, promoter holdings). Toggleable via `market_data.scrapers_enabled`.
- **Technicals**: Trendlyne scraper (momentum scores, volume breakouts, DMA signals). Toggleable via `market_data.scrapers_enabled`.
- **Economic calendar**: RBI MPC (primary, high impact), FOMC (secondary context, medium impact), NSE earnings dates. Dynamic year handling.
- **Feature engineering**: RSI, MACD, Bollinger Bands, VWAP, ATR, OBV, SuperTrend, Volume Profile, EMA. Pure functions, toggleable via config.
- **Bhavcopy importer**: NSE historical CSV import for backtesting seed data
- **Symbol quarantine**: Auto-blocks symbols after 3 consecutive fetch failures. Excluded from all pipelines. Unblock via API.

### Intelligence Layer
- **ML signals**: XGBoost with Platt scaling, walk-forward backtesting, model versioning, shadow A/B testing, automatic promotion/retirement. Fresh LTP used for entry/target/SL pricing.
- **Sentiment**: Gemini-powered sentiment analysis per symbol from aggregated news. Toggleable via `llm.enabled`.
- **Market scanning**: Weighted composite scoring (technical, volume, sentiment, fundamental) with sector rotation analysis and Gemini cross-validation. Volume-based tiebreaking prevents alphabetical bias.

### Risk & Execution
- **Risk check**: Kill switch, market hours enforcement, daily/weekly circuit breakers, max positions, portfolio/single-stock exposure caps, sector correlation limits, mandatory SL validation, ATR-based position sizing, slippage feedback loop, early close day handling, price drift validation (configurable via `execution.price_drift_max_pct`)
- **LLM review**: Gemini trade approval gate with full context. APPROVE/REJECT/RESIZE. Fallback to rules-only. Toggleable via `risk.llm_review_enabled`.
- **Trade execution**: Paper mode (simulated slippage with fresh LTP) + live mode (Kite API with price drift rejection). Returns trade_id for prediction linkage.
- **Position monitor**: Broker reconciliation, trailing SL, target/SL hit detection, unrealized PnL
- **Square-off**: Auto close MIS at EOD with transaction cost modeling. Respects early close days.
- **Signal dedup**: Symbols with existing signals or open positions today are skipped.
- **Symbol cooldown**: Hard block for `symbol_cooldown_days` after last trade, elevated confidence threshold (`symbol_repeat_min_confidence`) for `symbol_repeat_lookback_days`.

### Self-Learning & Reporting
- **Prediction tracking**: Log predictions with trade linkage, score against actuals, maintain scoreboard
- **Failure analysis**: Gemini analyzes prediction failures during scoring (5+ failures) and weekly retraining
- **LLM review accuracy**: Compares APPROVE/REJECT decisions vs actual trade PnL outcomes
- **Slippage stats**: Per-symbol slippage aggregation fed back into position sizing
- **Reports**: Daily (trades, PnL, win rate, slippage, predictions) and weekly (cumulative PnL, LLM accuracy, slippage trends, best/worst trades)

### Dashboard & Deployment
- **React frontend**: SPA in `frontend/` built with Vite + TypeScript + Tailwind CSS + Recharts. All timestamps localized to IST (`timeZone: "Asia/Kolkata"`). Dev server: `cd frontend && npm run dev`.
- **FastAPI backend**: REST endpoints + WebSocket. Basic auth. CORS middleware. Dry-run signal preview with diagnostics.
- **Telegram bot**: `/start`, `/status`, `/pnl`, `/positions`, `/stop`, `/kill`, `/resume`, `/auth`
- **Agent memory**: Cross-restart state persistence via `agent_memory` DB table with TTL support
- **Database maintenance**: CRON skill for daily backups, data retention cleanup (OHLCV, audit logs, predictions), old backup pruning
- **Docker**: Separate backend and frontend containers via docker-compose. Backend (Python), frontend (nginx + React build).

## Key Files

- **`backend/src/yolovest/main.py`** — Entry point. Builds context, starts orchestrator, Telegram, dashboard.
- **`backend/src/yolovest/orchestrator.py`** — Heartbeat pipeline, error propagation, mutex, memory persistence
- **`backend/src/yolovest/context.py`** — `AppContext`, all Protocol types, `MarketHoursChecker` (early close aware)
- **`backend/src/yolovest/config.py`** — All config models with validators. Service toggles: `llm.enabled`, `market_data.news_enabled`, `market_data.scrapers_enabled`.
- **`backend/src/yolovest/models/schemas.py`** — All Pydantic data contracts
- **`backend/src/yolovest/data/db.py`** — SQLite database layer + migration runner + agent memory + quarantine methods
- **`backend/src/yolovest/data/ingester.py`** — Fallback chain orchestrator for market data
- **`backend/src/yolovest/data/features.py`** — Technical indicator computation (pure functions)
- **`backend/src/yolovest/data/google_finance.py`** — Google Finance scraper (Indian indices primary, global context secondary)
- **`backend/src/yolovest/data/kite_data.py`** — Optional Kite Connect data provider
- **`backend/src/yolovest/data/economic_calendar.py`** — RBI MPC (primary) + FOMC (secondary context) + NSE earnings
- **`backend/src/yolovest/broker/zerodha.py`** — Zerodha Kite Connect broker (paper + live)
- **`backend/src/yolovest/llm/gemini.py`** — Google Gemini LLM (all 7 methods)
- **`backend/src/yolovest/memory.py`** — Agent memory persistence
- **`backend/src/yolovest/news/`** — News scrapers + aggregator with dedup
- **`backend/src/yolovest/strategy/ml_signal.py`** — XGBoost model for signal generation
- **`backend/src/yolovest/strategy/backtest.py`** — Walk-forward backtesting engine
- **`backend/src/yolovest/dashboard/app.py`** — FastAPI backend (REST endpoints + WebSocket + CORS)
- **`frontend/`** — React SPA (Vite + TypeScript + Tailwind CSS + Recharts)
- **`frontend/src/api/`** — API client with Basic Auth and endpoint definitions
- **`frontend/src/pages/`** — Dashboard, Positions, Trades, TradeDetail, Watchlist, Analytics, Reports, Audit, Integrations, DryRun, Login, etc.
- **`frontend/src/components/`** — Reusable UI: EquityChart, PortfolioCards, TradesTable, PositionsTable, SlippageChart, LLMAccuracyCard, etc.
- **`frontend/src/hooks/`** — React Query hooks, WebSocket hook, auth hook
- **`backend/migrations/`** — Numbered SQL migration files (001-017)
- **`backend/config.example.yaml`** — File-only config keys (secrets, paths, server binding)

## Configuration

Config is split between a YAML file (file-only keys) and a SQLite `config` table (everything else, editable via UI).

### File-only keys (config.yaml)
These require secrets, filesystem paths, or a restart to change:
- `mode` (paper/live), `broker.*` (API keys), `llm.api_key`
- `database.path`, `database.backup_dir`, `market_data.bhavcopy_dir`
- `dashboard.host`, `dashboard.port`, `dashboard.password`
- `log.*` (all logging config)
- `notifications.telegram.enabled`, `notifications.telegram.bot_token`, `notifications.telegram.chat_id`

### DB-editable keys (~129 keys, managed via Settings page)
On first start, code defaults are populated into the `config` table. Thereafter, changes are made via:
- **UI**: Settings page (`/settings`) with grouped, type-aware form inputs
- **API**: `GET /api/config`, `PUT /api/config` (validates via Pydantic before persisting)
- Changes are hot-applied to the running config immediately.

### Service toggles

| Service | Config key | Default | What it controls |
|---------|-----------|---------|-----------------|
| Gemini LLM | `llm.enabled` | `false` | Sentiment analysis, trade review, failure analysis |
| News sources | `market_data.news_enabled` | `true` | MoneyControl, ET Markets, LiveMint RSS feeds |
| Scrapers | `market_data.scrapers_enabled` | `true` | Screener.in, Trendlyne, Google Finance, NSE, Economic Calendar |
| Kite data | `market_data.kite_data_enabled` | `false` | Paid Kite Connect historical data API |
| Telegram | `notifications.telegram.enabled` | `false` | Telegram bot and notifications (file-only, requires restart) |
| LLM review gate | `risk.llm_review_enabled` | `true` | Gemini trade approval (falls back to rules-only if LLM disabled) |

### Holidays & early close days
Stored in DB config (`market_hours.holidays`, `market_hours.early_close_days`). Managed via:
- **UI**: Calendar page (`/calendar`) — Outlook-style weekly/monthly view with inline add/remove
- **API**: `GET/POST/DELETE /api/holidays`
- **Telegram**: `/holiday`, `/holiday add YYYY-MM-DD|today|tomorrow`, `/holiday rm YYYY-MM-DD|today|tomorrow`

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL-M** = Stop-Loss Market order. **GIFT Nifty** = offshore Nifty futures (pre-market indicator).
- Market hours: 9:15 AM - 3:30 PM IST. Square-off at 3:15 PM. ~15 NSE holidays/year.
- Kite API rate limit: 10 req/s aggregate. Daily re-auth required (user pastes request_token via Telegram).

## Conventions

- Python 3.11+, async throughout
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`
- Every skill logs a completion summary at INFO level with key metrics
- Config via YAML (file-only keys) + DB `config` table (everything else) + Pydantic validation. Secrets via environment variables (never in config files or DB).
- SQLite with WAL mode. Schema versioned via numbered migration scripts in `backend/migrations/`.
- Paper trading mode by default — live trading requires explicit `mode: live` in config file.
- India-first design: RBI MPC is primary economic event; global indices tracked as secondary sentiment context only.
- All UI timestamps use `timeZone: "Asia/Kolkata"` for consistent IST display.
- Destructive actions (delete dry run, unquarantine symbol) require user confirmation.
- Tests use pytest-asyncio with `asyncio_mode = "auto"`. Run with `cd backend && PYTHONPATH=src python -m pytest tests/ -v`.
- Frontend uses Vite + TypeScript + Tailwind CSS. Run with `cd frontend && npm run dev`. Build with `npm run build`.
