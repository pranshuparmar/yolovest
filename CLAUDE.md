# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is a fully autonomous AI-driven Indian stock trading platform. It uses Google Gemini for LLM reasoning, XGBoost for ML signals, and Zerodha Kite Connect for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed) with optional paid Kite data plan support.

**Current state:** Fully implemented and production-ready. Running in live trading mode with manual approval.

## Project Structure

```
/
├── backend/          — Python backend (FastAPI + trading engine)
│   ├── src/yolovest/ — Main package
│   │   ├── broker/        — Broker integration (base.py, zerodha.py)
│   │   ├── dashboard/     — FastAPI REST API + WebSocket (app.py, 65+ endpoints)
│   │   ├── data/          — Market data, DB layer, features, scrapers
│   │   ├── llm/           — LLM integration (base.py, gemini.py)
│   │   ├── models/        — Pydantic data contracts (schemas.py)
│   │   ├── news/          — News scrapers + aggregator (5 sources)
│   │   ├── skills/        — 18 skills extending SkillBase
│   │   ├── strategy/      — ML signal generation, holding period logic, backtesting
│   │   ├── main.py        — Entry point, context builder
│   │   ├── orchestrator.py — Heartbeat pipeline coordinator
│   │   ├── config.py      — 34 nested Pydantic config classes
│   │   ├── context.py     — AppContext + Protocol types
│   │   ├── telegram_bot.py — 19 Telegram commands
│   │   ├── cron_scheduler.py — CRON skill scheduler
│   │   └── ...            — timezone, memory, costs, events, notify, watchdog
│   ├── tests/        — Tests (mirrors src/ structure)
│   ├── migrations/   — SQL migration files (001-022)
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/         — React SPA (Vite + TypeScript + Tailwind)
│   ├── src/
│   │   ├── pages/         — 26 page components
│   │   ├── components/    — 20 reusable UI components
│   │   ├── api/           — API client + endpoint definitions
│   │   ├── hooks/         — React Query hooks, WebSocket, auth
│   │   ├── types/         — TypeScript type definitions
│   │   └── utils/         — datetime, csvExport helpers
│   ├── Dockerfile
│   └── package.json
├── docker-compose.yml
└── CLAUDE.md
```

## Architecture

### Abstraction Layers (ABCs)

- **`BrokerBase`** (`broker/base.py`) → `ZerodhaBroker` — execution + paper simulation. Shared rate limiter (`Semaphore(8)`) across broker and KiteDataProvider to respect Kite's 10 req/s aggregate limit. MARKET orders auto-converted to LIMIT with 1% protection buffer (Zerodha API requirement). SL-M converted to SL with limit price.
- **`LLMBase`** (`llm/base.py`) → `GeminiLLM` — 7 methods: `ping`, `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`
- **`MarketDataBase`** (`data/base.py`) → `JugaadDataProvider` (primary) → `YFinanceProvider` (fallback) → `TVDatafeedProvider` (intraday) → `KiteDataProvider` (optional paid plan). Fallback chain with staleness validation and quality checks.
- **`NewsSource`** (`news/base.py`) → MoneyControl, ET Markets, LiveMint (RSS), NSE Official (API), Google Finance (scraper)
- **`MLBase`** (`strategy/ml_base.py`) → `XGBoostSignalModel` with Platt scaling calibration

### Skill System

18 skills extend `SkillBase` (in `backend/src/yolovest/skills/base.py`). Each skill has:
- `async execute(**kwargs) -> SkillResult`
- `should_run() -> bool`
- `async safe_execute()` — wraps execute with exception logging (logger.exception)
- A trigger type: `HEARTBEAT`, `CRON`, `EVENT`, or `MANUAL`
- Access to shared context via `self.ctx` (config, db, broker, llm, market_data, news_aggregator, memory, etc.)

Skills are registered in `SKILL_REGISTRY` dict in `backend/src/yolovest/skills/__init__.py`.

### Heartbeat Pipeline (market hours, every 15min)

```
health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → [manual: queue pending] OR [auto: trade-execute] → predict-track
  → position-monitor (always runs)
```

Error propagation: if `ingest-data` fails, skip scan+signals but always run `position-monitor`. If `health-check` fails, abort entire heartbeat. If `trade-execute` fails, signal is removed from DB so it can be retried next heartbeat. In manual mode, failed executions revert the pending trade back to `status='pending'`.

### Balanced Mode Dual-Model

Strategy mode `"balanced"` runs **both** ML models concurrently per stock:
1. `predict_intraday()` — same-day model (MIS)
2. `predict_swing()` — multi-day model (CNC)
3. Picks the prediction with higher confidence (ignoring HOLD)
4. After `intraday_cutoff` (default 14:30 IST), only swing model runs

Other modes (`intraday`, `short_term`, `long_term`) run only the corresponding model.

### Mode Filtering (Paper vs Live)

All trade/position/prediction queries filter by `ctx.config.mode`. Paper and live data never mix:
- `get_open_positions(mode=)`, `get_portfolio_state(mode=)`, `get_todays_trades(mode=)`
- `get_trades_history(mode=)`, `get_equity_curve(mode=)`, `get_daily_pnl_calendar(mode=)`
- `get_strategy_performance(mode=)`, `get_execution_quality(mode=)`, `get_slippage_stats(mode=)`
- All prediction queries filter by `predictions.mode`
- Position-monitor, risk-check, generate-signals, square-off all pass `ctx.config.mode`

**Critical**: When switching mode via Settings UI, `ctx.broker._mode` is synced automatically. `trade_execute` has a safety check that detects and auto-fixes broker/config mode mismatches.

### Manual Approval Flow

When `execution.transaction_mode == "manual"`:
1. Signal passes risk-check → LLM review → queued to `pending_trades` table
2. Telegram notification: `/approve SYMBOL` or `/reject SYMBOL`
3. Dashboard: `PendingTradesBanner` component with approve/reject/edit buttons
4. On approval: executes immediately via `trade_execute`
5. On execution failure: pending trade reverts to `status='pending'` for retry
6. Pending trades auto-expire after 30 minutes (via dashboard polling)

### Position Adoption

Position-monitor auto-adopts untracked broker positions/holdings:
1. Compares `broker.get_positions()` + `broker.get_holdings()` vs local DB
2. Untracked positions (not locked) are adopted with ATR-based SL/target
3. Trade record created with `origin='adopted'`, `mode=ctx.config.mode`
4. Symbol auto-added to watchlist for ML signal coverage
5. Telegram notification sent for each adoption

### AppContext

`AppContext` dataclass (`context.py`) holds references to all subsystems via Protocol types:
- `config`, `db`, `broker`, `llm`, `market_data`, `notify`, `market_hours`, `event_bus`
- `ml` (optional ML provider), `news_aggregator` (built at startup), `memory` (agent persistence)

### Inter-Skill Data Contracts

All data exchange between skills uses typed Pydantic models in `backend/src/yolovest/models/schemas.py`: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`, `NewsArticle`, `MLPrediction`, `BacktestResult`.

## Database

SQLite with WAL mode. Schema versioned via 22 numbered migration scripts in `backend/migrations/`.

### Key Tables

| Table | Purpose |
|-------|---------|
| `trades` | All trade records. Key columns: `mode` (paper/live), `status` (open/closed), `origin` (system/adopted), `order_id`, `sl_order_id` |
| `signals` | Generated signals (used for dedup via `get_todays_signaled_symbols`) |
| `predictions` | ML predictions with `mode` column. Scored against actuals. |
| `pending_trades` | Manual approval queue. `created_at` uses Python ISO format (not SQLite `datetime('now')`) for correct expiry comparison. |
| `quarantined_symbols` | Auto-blocked after 3 fetch failures. `replacement_symbol` for pipeline substitution. |
| `locked_holdings` | User-protected holdings — never auto-sold or auto-adopted. |
| `config` | Key-value store for UI-editable settings (dot-notation keys). |
| `ohlcv` | OHLCV bars. Unique on (symbol, interval, timestamp). |
| `watchlist` | Auto-generated by market-scan. Adopted symbols auto-added. |
| `user_watchlist` | User-managed, persists across market-scan refreshes. |
| `dry_run_results` | Signal preview with next-day scoring. |

### Migrations of Note

- **019**: `replacement_symbol` on quarantined_symbols
- **020**: Fix trade status 'complete'/'filled' → 'open'
- **021**: `mode` column on predictions (paper/live separation)
- **022**: `origin` column on trades ('system' vs 'adopted')

## Telegram Commands

| Command | Purpose |
|---------|---------|
| `/start` | Quick status summary |
| `/help` | Full command reference |
| `/status` | System health + integration checks |
| `/pnl` | Today's PnL summary |
| `/positions` | Open positions |
| `/dashboard` | Dashboard link |
| `/pending` | Show pending trades |
| `/approve SYMBOL [overrides]` | Approve pending trade (supports full/partial overrides) |
| `/reject SYMBOL` | Reject pending trade |
| `/trade BUY SYMBOL ENTRY TARGET SL [PRODUCT] [QTY]` | Manual trade |
| `/clear` | Clear today's signals + pending trades for regeneration |
| `/review [SYMBOL ...]` | ML review of any symbol or all holdings |
| `/skills` | List all registered skills |
| `/run SKILL_NAME` | Execute a skill |
| `/stop` | Pause trading (kill switch) |
| `/kill` | Emergency square-off + pause |
| `/resume` | Resume trading |
| `/auth TOKEN` | Daily Kite re-auth |
| `/holiday [add\|rm DATE]` | Manage market holidays |

## Configuration

Config is split between a YAML file (file-only keys) and a SQLite `config` table (everything else, editable via UI).

### File-only keys (config.yaml)
Secrets, filesystem paths, and server binding:
- `broker.api_key`, `broker.api_secret`, `llm.api_key`
- `database.path`, `database.backup_dir`, `market_data.bhavcopy_dir`
- `dashboard.host`, `dashboard.port`, `dashboard.password`
- `log.log_dir`, `log.max_bytes`, `log.backup_count`
- `notifications.telegram.bot_token`, `notifications.telegram.chat_id`

### Key Config Sections

| Section | Key Fields |
|---------|-----------|
| `mode` | `"paper"` or `"live"` — controls broker execution and data filtering |
| `strategy.mode` | `"balanced"` / `"intraday"` / `"short_term"` / `"long_term"` |
| `risk` | max_risk_per_trade_pct (2%), max_open_positions (3), max_single_stock_pct (25%), max_portfolio_exposure_pct (60%), daily_loss_limit_pct (3%) |
| `market_hours` | open (09:15), close (15:30), square_off (15:15), **intraday_cutoff (14:30)** |
| `execution` | transaction_mode ("auto"/"manual"), max_order_retries (3), price_drift_max_pct (2%) |
| `market_data` | kite_data_enabled, news_enabled, scrapers_enabled |

### Service Toggles

| Service | Config key | Default |
|---------|-----------|---------|
| Gemini LLM | `llm.enabled` | `false` |
| News sources | `market_data.news_enabled` | `true` |
| Scrapers | `market_data.scrapers_enabled` | `true` |
| Kite data | `market_data.kite_data_enabled` | `false` |
| Telegram | `notifications.telegram.enabled` | `false` |
| LLM review gate | `risk.llm_review_enabled` | `true` |

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL** = Stop-Loss Limit (requires both price and trigger_price). App converts SL-M to SL with 1% buffer.
- Market hours: 9:15 AM - 3:30 PM IST. Square-off at 3:15 PM. Intraday cutoff at 2:30 PM.
- Kite API: 10 req/s aggregate limit (shared semaphore between broker + data provider). MARKET orders converted to LIMIT with 1% protection buffer.
- Daily re-auth required (user pastes request_token via Telegram `/auth` or dashboard).
- SELL signals for stocks not in holdings are forced to MIS/intraday (Indian equity rules: no overnight short selling for retail).

## Conventions

- Python 3.11+, async throughout
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`
- Every skill logs a completion summary at INFO level with key metrics
- `safe_execute()` wraps all skill execution with `logger.exception()` on failure
- Config via YAML (file-only keys) + DB `config` table (everything else) + Pydantic validation
- SQLite with WAL mode. Schema versioned via numbered migration scripts.
- Paper trading mode by default — live trading requires explicit `mode: live` in config.
- All trade/position queries filter by `ctx.config.mode` — paper and live data never mix.
- Trades track `origin` ('system' or 'adopted') and `mode` ('paper' or 'live').
- Predictions track `mode` separately for clean analytics.
- Pending trades use Python ISO timestamps (not SQLite `datetime('now')`) for correct expiry.
- Broker retry helper skips permanent errors (validation, margin, auth) — only retries transient errors.
- Before retrying order placement, checks Kite for recent completed orders to prevent duplicates.
- All timestamps use IST for market logic, UTC for DB storage.
- Frontend uses Vite + TypeScript + Tailwind CSS. All UI timestamps use `timeZone: "Asia/Kolkata"`.
- Destructive actions (delete trade, bulk delete, unquarantine) require user confirmation.
- Tests use pytest-asyncio with `asyncio_mode = "auto"`. Run with `cd backend && PYTHONPATH=src python -m pytest tests/ -v`.
- Frontend dev: `cd frontend && npm run dev`. Build: `npm run build`.

## Key Files

- **`main.py`** — Entry point. Builds context, starts orchestrator, Telegram, dashboard. Syncs broker mode on config change.
- **`orchestrator.py`** — Heartbeat pipeline, per-signal chain, manual approval queueing, signal cleanup on failure.
- **`context.py`** — `AppContext`, Protocol types, `MarketHoursChecker` (early close + intraday cutoff aware).
- **`config.py`** — 34 config models. `_MODE_HOLDING_DAYS` maps strategy modes to day ranges. `apply_db_config()` for hot-reload.
- **`data/db.py`** — SQLite layer (~160KB). All trade queries accept `mode` parameter. `clear_todays_signals()`, `bulk_delete()`.
- **`data/kite_data.py`** — Optional Kite data provider. Shared rate limiter. LTP=0 rejected. Bid/ask safe extraction. Token cache cleared on re-auth.
- **`data/ingester.py`** — Fallback chain. Deduplicates Kite provider in quote list. Quarantine replacement swapping.
- **`broker/zerodha.py`** — MARKET→LIMIT conversion, SL-M→SL conversion, shared rate limiter, permanent error detection, paper order dicts match Kite field names.
- **`skills/generate_signals.py`** — Balanced dual-model, intraday cutoff, SELL→MIS for non-holdings.
- **`skills/trade_execute.py`** — Mode mismatch safety check, fill_price=0 handling, duplicate order prevention via broker check before retry.
- **`skills/position_monitor.py`** — Broker exit on target/SL hit (cancel SL + market order), trailing SL with try-except, position adoption, holdings inclusion.
- **`skills/risk_check.py`** — Counts pending trades toward max_open_positions in manual mode.
- **`skills/report_generate.py`** — Uses `predictions_result["items"]` (paginated dict). Weekly on Friday.
- **`telegram_bot.py`** — 19 commands. `/review` works for any symbol. `/approve` uses symbol (not ID). Token sync on `/auth`.
- **`dashboard/app.py`** — 65+ endpoints. Mode passed to all trade queries. Bulk delete, clear signals, review endpoint (`/api/review`).
- **`frontend/src/pages/HoldingsPage.tsx`** — Multi-select checkboxes, bulk lock/unlock, ML review panel.
- **`frontend/src/pages/WatchlistPage.tsx`** — Quick ML Review section for any symbol.
- **`frontend/src/components/PendingTradesBanner.tsx`** — Approval UI + `ClearSignalsButton` (always visible on dashboard).
- **`frontend/src/pages/SettingsPage.tsx`** — Click-to-toggle info tooltips (mobile-friendly).
