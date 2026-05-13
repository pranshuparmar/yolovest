# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is an AI-driven Indian stock trading platform. It uses Google Gemini for LLM reasoning, XGBoost for ML signals, and Zerodha Kite Connect for execution. Market data comes from a paid Kite Connect data plan as primary (when enabled), with free providers (jugaad-data, yfinance, tvDatafeed) as fallback. Designed to be self-hosted; the dashboard runs behind nginx-proxy + acme-companion for HTTPS.

## Project Structure

```
/
├── backend/                — Python backend (FastAPI + trading engine)
│   ├── src/yolovest/       — Main package
│   │   ├── broker/         — Broker integration (base.py, zerodha.py,
│   │   │                     kite_rate_limiter.py, circuit_breaker.py)
│   │   ├── dashboard/      — FastAPI REST API + WebSocket (app.py)
│   │   ├── data/           — Market data providers (Kite, jugaad, yfinance,
│   │   │                     tvfeed), DB layer, ingester chain, features,
│   │   │                     NSE constituents, scrapers
│   │   ├── llm/            — LLM integration (base.py, gemini.py)
│   │   ├── models/         — Pydantic data contracts (schemas.py)
│   │   ├── news/           — News scrapers + aggregator
│   │   ├── skills/         — Skills extending SkillBase
│   │   ├── strategy/       — ML signal generation, holding period logic,
│   │   │                     session caps, backtesting
│   │   ├── main.py         — Entry point, context builder
│   │   ├── orchestrator.py — Heartbeat pipeline coordinator
│   │   ├── config.py       — Nested Pydantic config classes
│   │   ├── context.py      — AppContext + Protocol types
│   │   ├── telegram_bot.py — Telegram command handlers
│   │   ├── cron_scheduler.py — CRON skill scheduler
│   │   └── ...             — timezone, memory, costs, events, notify, watchdog
│   ├── tests/              — Tests (mirrors src/ structure)
│   ├── migrations/         — Numbered SQL migration files (.sql)
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/               — React SPA (Vite + TypeScript + Tailwind)
│   ├── src/
│   │   ├── pages/          — Page components
│   │   ├── components/     — Reusable UI components
│   │   ├── api/            — API client + endpoint definitions
│   │   ├── hooks/          — React Query hooks, WebSocket, auth
│   │   ├── types/          — TypeScript type definitions
│   │   └── utils/          — datetime, csvExport helpers
│   ├── nginx.conf          — Reverse proxy for /api → backend (uses Docker DNS resolver)
│   ├── Dockerfile
│   └── package.json
├── nginx/                  — Shared nginx-proxy assets
│   ├── custom.conf         — Global HTTP-level overrides
│   ├── heal-cert-symlinks.sh   — Restore <domain>.crt / <domain>.key on each boot
│   ├── cert-heal-loop.sh   — Periodic heal sidecar entrypoint
│   └── tls-healthcheck.sh  — Detect ssl_reject_handshake / missing symlinks
├── scripts/                — Operational scripts
│   └── cert-backup-loop.sh — Periodic tar backup of the certs volume
├── docs/                   — Operational + design docs
│   ├── tls-recovery.md
│   └── kite-features-backlog.md
├── backups/                — Volume snapshots (.gitignored)
├── docker-compose.yml
└── CLAUDE.md
```

## Architecture

### Abstraction Layers (ABCs)

- **`BrokerBase`** (`broker/base.py`) → `ZerodhaBroker`. Order placement, GTT (Good Till Triggered) two-leg OCO orders for CNC, position/holdings/margins queries, daily auth lifecycle. Paper mode simulates fills using Kite-compatible field names (`filled_quantity`, `average_price`, `tradingsymbol`). All Kite calls go through `KiteRateLimiter` (see below).
- **`LLMBase`** (`llm/base.py`) → `GeminiLLM`. 7 methods: `ping`, `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`.
- **`MarketDataBase`** (`data/base.py`) → `MarketDataIngester` orchestrates a fallback chain. When `market_data.kite_data_enabled` is true, the chain is:
  - **daily**: `KiteDataProvider` → `JugaadDataProvider` → `YFinanceProvider`
  - **intraday (5min/15min/1m)**: `KiteDataProvider` → `TVDatafeedProvider`
  When disabled, Kite is removed and jugaad/yfinance/tvdatafeed handle their lanes. Each provider has staleness validation and per-bar quality checks (high ≥ low, close in range).
- **`NewsSource`** (`news/base.py`) → MoneyControl, ET Markets, LiveMint (RSS); NSE Official (API); Google Finance (scraper).
- **`MLBase`** (`strategy/ml_base.py`) → `XGBoostSignalModel` with Platt scaling calibration and `tree_method='hist'` for memory-efficient training.

### Kite Rate Limiter

`broker/kite_rate_limiter.py` provides a single `KiteRateLimiter` (concurrency cap + time-based interval) shared between `ZerodhaBroker` and `KiteDataProvider`. Built once in `main.build_context()` and injected. Default: 10 req/s and 8 concurrent.

The `historical_data` endpoint additionally has its own tighter throttle (default 0.4s minimum interval = ~2.5 req/s) inside `KiteDataProvider`, since Kite enforces a separate per-second limit on that endpoint. On detected `429 Too many requests`, historical fetches back off 10s before retrying.

`KiteDataProvider` pre-warms its instrument-token cache from a single `kite.instruments("NSE")` call on first lookup, then serves all subsequent lookups from memory.

### Skill System

Skills extend `SkillBase` (`skills/base.py`). Each skill has:
- `async execute(**kwargs) -> SkillResult`
- `should_run() -> bool`
- `async safe_execute()` — wraps `execute` with exception logging (`logger.exception`)
- A trigger type: `HEARTBEAT`, `CRON`, `EVENT`, or `MANUAL`
- Access to shared context via `self.ctx`

Skills are registered in `SKILL_REGISTRY` dict in `skills/__init__.py`.

### Heartbeat Pipeline (market hours, every 15min)

```
health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → [manual: queue pending] OR [auto: trade-execute] → predict-track
  → position-monitor (always runs)
```

Error propagation:
- `health-check` fail → abort entire heartbeat.
- `ingest-data` fail → skip scan + signals; always run `position-monitor`.
- `trade-execute` fail in manual mode → pending row reverted to `status='pending'` for retry. When the failed `place_order` actually placed the order at the broker (Zerodha sometimes returns an error AFTER placing), trade-execute detects this via `kite.orders()` and **reconciles** the surviving order into a successful trade record instead of marking failed.

### Strategy Modes

- **`balanced`**: Runs both `predict_intraday()` and `predict_swing()` concurrently per stock, picks higher confidence. After `intraday_cutoff` (default 14:30 IST), only swing model runs.
- **`intraday`**: Only intraday model (MIS, same-day).
- **`short_term`**: Only swing model (2-5 day holds).
- **`long_term`**: Only swing model (5-66 day holds, CNC).

Holding period per stock is dynamic based on ATR%, trend strength, position-mix bias, and market regime. Target/SL multipliers come from `strategy.holding_periods.{intraday,short_swing,week,long}.{target,stop_loss}`, interpolated by holding-day count.

### Intraday Circuit Caps

For intraday signals (`holding_period == "intraday"`) when `market_data.kite_data_enabled`, ATR-based target/SL are constrained by the exchange's circuit limits read from the live quote:
- BUY target capped at `upper_circuit × 0.99` (orders above won't fill)
- SELL target capped at `lower_circuit × 1.01`
- Stop-loss floored at the opposite circuit accordingly

Today's session high/low are intentionally NOT enforced — they're current extremes, not forward boundaries. A breakout target above today's high is a legitimate model output and is allowed through.

### Mode Filtering (Paper vs Live)

All trade/position/prediction queries filter by `ctx.config.mode`. `trades`, `predictions`, `signals`, and `pending_trades` all carry a `mode` column. Paper and live data never mix in any view, skill, or API endpoint.

**Critical**: When mode changes via Settings UI or config reload, `ctx.broker._mode` is synced automatically. `trade_execute` has a safety check that detects and auto-fixes broker/config mode mismatches.

### Manual Approval Flow

When `execution.transaction_mode == "manual"`:
1. Signal passes risk-check → LLM review → queued to `pending_trades` table.
2. Telegram notification: `/approve SYMBOL` or `/reject SYMBOL`.
3. Dashboard: `PendingTradesBanner` shows pending count + total investment + per-row approve/edit/reject.
4. On approval: executes immediately via `trade_execute`.
5. On execution failure (non-reconciled): pending trade reverts to `status='pending'` for retry.
6. Pending trades auto-expire after 30 minutes.

Risk-check includes pending-trade notional in the `max_portfolio_exposure_pct` check, so the queue can't accumulate past the cap.

### Position Adoption & Exit

`position-monitor` auto-adopts untracked broker positions/holdings:
1. Compares `broker.get_positions()` + `broker.get_holdings()` vs local DB.
2. Untracked positions (not locked) are adopted with ATR-based SL/target.
3. Trade record created with `origin='adopted'`, auto-added to watchlist.
4. Locked holdings are never adopted or auto-managed.

Exit paths:
- **Broker-side GTT** (CNC only) — placed by `trade-execute._attach_oco_gtt` after entry fill. `position-monitor` skips client-side target/SL checks when `gtt_id` is set; ghost-position reconciliation closes the DB row when the GTT fires and the broker position vanishes.
- **Client-side detection** — when no GTT (e.g. MIS), `position-monitor` queues an exit when LTP crosses target/SL.
- **Manual close** — `POST /api/positions/{trade_id}/close` (UI: red Close button per row) cancels SL, deletes GTT, places market exit at the broker, computes realised PnL with costs, closes the row.
- **Square-off** — CRON skill at `market_hours.square_off` (default 15:15) closes MIS positions. `/kill` runs it with `force=True`.

### AppContext

`AppContext` dataclass (`context.py`) holds references to all subsystems via Protocol types: `config`, `db`, `broker`, `llm`, `market_data`, `notify`, `market_hours`, `event_bus`, `ml`, `news_aggregator`, `memory`.

### Inter-Skill Data Contracts

All data exchange between skills uses typed Pydantic models in `models/schemas.py`: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`, `NewsArticle`, `MLPrediction`, `BacktestResult`.

## Database

SQLite with WAL mode. Schema versioned via numbered migration scripts in `backend/migrations/` (run in lexical order at startup).

### Key Tables

| Table | Purpose |
|-------|---------|
| `trades` | Trade records. Key columns: `mode`, `status`, `origin` (`system` / `adopted`), `gtt_id` (broker OCO GTT id when active). |
| `signals` | Generated signals with `mode` column (for dedup via `get_todays_signaled_symbols` + mode-scoped bulk delete). |
| `predictions` | ML predictions with `mode`, `is_shadow`, `model_version`, `scored_at` columns. |
| `pending_trades` | Manual approval queue with `mode` column. Python ISO timestamps for correct expiry comparison. |
| `quarantined_symbols` | Auto-blocked after 3 fetch failures. `replacement_symbol` lets the user substitute (e.g. ZOMATO → ETERNAL). |
| `locked_holdings` | User-protected holdings — never auto-sold or auto-adopted. |
| `config` | Key-value store for UI-editable settings (dot-notation keys). |
| `system_state` | Long-lived key-value: `kite_access_token`, `kill_switch`, `initial_capital`, `universe_constituents:<universe>`, etc. |
| `ohlcv` | OHLCV bars. Unique on (symbol, interval, timestamp). |
| `watchlist` | Auto-generated by market-scan. Adopted symbols auto-added. |
| `user_watchlist` | User-managed, persists across market-scan refreshes. |
| `dry_run_results` | Signal preview with next-day scoring. |
| `model_versions` | Trained model registry with `status` (production / shadow / retired) and performance metrics. |
| `audit_log` | Skill execution audit trail. |
| `agent_memory` | Cross-restart state persistence with TTL. |

### Quarantine and Replacement Resolution

`db.resolve_symbols_with_replacements(symbols)` is the single point that applies quarantine policy across all ingest paths:
- Active symbol → keep as-is.
- Quarantined + replacement set → swap to the replacement.
- Quarantined + no replacement → **drop entirely** (returns shorter list).

All ingest paths (`ingest-universe`, `ingest-data`, `backfill-data`, `backfill-intraday`, `generate-signals` when reading `user_watchlist`) route through this resolver. When `record_fetch_failure` crosses the 3-strike threshold the symbol is auto-quarantined; the appropriate skill logs a WARNING with a pointer to set a replacement.

### Universe Resolution

`scanning.universe` selects from `nifty50` / `nifty100` / `nifty200` / `nifty500`. `ingest-universe` resolves via a cached-fetch chain:
1. `system_state.universe_constituents:<universe>` (≤ 7-day cache).
2. Live fetch from `niftyindices.com/IndexConstituent/ind_<universe>list.csv` — filtered to `Series=EQ`, `DUMMY*` placeholders dropped.
3. Bundled static list in `data/nse_symbols.py` as final fallback.

The legacy alias `"all"` resolves to `"nifty500"` at fetch time for backwards-compat.

## Telegram Commands

| Command | Purpose |
|---------|---------|
| `/start` | Quick status summary |
| `/help` | Full command reference |
| `/status` | System health + integration checks |
| `/pnl` | Today's PnL summary |
| `/positions` | Open positions |
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
| `/auth TOKEN` | Daily Kite re-auth (also syncs to `KiteDataProvider`) |
| `/holiday [add\|rm DATE]` | Manage market holidays |

## Configuration

Config is split between a YAML file (file-only keys) and a SQLite `config` table (everything else, editable via Settings UI). On first start, code defaults are populated into the `config` table. Thereafter, changes are made via UI or API and hot-applied to the running config.

**Bootstrap ordering** (`main.async_main`): DB is initialized and DB-config is applied to the in-memory `AppConfig` **before** `build_context()` constructs the broker / market_data / LLM. This ensures runtime-toggle changes (e.g. `kite_data_enabled`) actually take effect on next restart — previously they were ignored because the ingester chain was frozen during build.

### File-only keys (config.yaml)

Secrets, filesystem paths, and server binding:
- `broker.api_key`, `broker.api_secret`, `llm.api_key`
- `database.path`, `database.backup_dir`, `market_data.bhavcopy_dir`
- `dashboard.host`, `dashboard.port`, `dashboard.password`
- `log.log_dir`, `log.max_bytes`, `log.backup_count`
- `notifications.telegram.bot_token`, `notifications.telegram.chat_id`

### Key Config Sections

| Section | Notable fields |
|---------|----------------|
| `mode` | `"paper"` or `"live"` |
| `strategy.mode` | `"balanced"` / `"intraday"` / `"short_term"` / `"long_term"` |
| `strategy.holding_periods.{intraday,short_swing,week,long}.{target,stop_loss}` | ATR multipliers per holding bucket |
| `risk` | `max_risk_per_trade_pct`, `max_open_positions`, `max_single_stock_pct`, `max_portfolio_exposure_pct` (counts pending notional), `daily_loss_limit_pct`, `weekly_loss_limit_pct`, `max_same_sector_positions` |
| `market_hours` | `open`, `close`, `square_off`, `intraday_cutoff` |
| `execution` | `transaction_mode` (`auto`/`manual`), `max_order_retries`, `price_drift_max_pct` |
| `scanning` | `universe`, `shortlist_size`, `min_avg_daily_volume`, `seed_symbols` (cold-start fallback only) |
| `market_data` | `kite_data_enabled`, `news_enabled`, `scrapers_enabled`, `backfill_days` (daily, used by both ingest-universe and backfill-data), `intraday_backfill_days` (5-minute), `session_cap_atr_buffer` |

### Service Toggles

| Service | Config key | Default |
|---------|-----------|---------|
| Gemini LLM | `llm.enabled` | `false` |
| News sources | `market_data.news_enabled` | `true` |
| Scrapers | `market_data.scrapers_enabled` | `true` |
| Kite data plan | `market_data.kite_data_enabled` | `false` |
| Telegram | `notifications.telegram.enabled` | `false` |
| LLM review gate | `risk.llm_review_enabled` | `true` |

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL** order = stop-loss limit (price + trigger_price). **SL-M** would be stop-loss market but Zerodha disabled it for retail API; `ZerodhaBroker` auto-converts incoming `SL-M` into `SL` with a 0.5% buffer past the trigger.
- **MARKET** orders auto-convert to **LIMIT** at LTP ± buffer (1% without paid data, 0.5% with paid data) — Zerodha API restriction.
- **Tick rounding** is applied automatically to every `price` and `trigger_price` in `_live_place_order` (default 0.05 tick).
- Market hours: 9:15 AM – 3:30 PM IST. Default `intraday_cutoff` = 14:30 IST (configurable; no MIS signals after).
- **GTT** (Good Till Triggered) is CNC-only at Zerodha; MIS positions cannot use GTT and rely on client-side detection.
- Kite Connect daily re-auth required (paste request_token via Telegram `/auth` or dashboard).
- SELL signals for stocks not in holdings are forced to MIS/intraday (Indian equity rules: no overnight short selling for retail).

## TLS / nginx-proxy Reliability

The stack hosts the dashboard behind `nginxproxy/nginx-proxy` + `nginxproxy/acme-companion` (pinned versions in `docker-compose.yml`). Several defensive layers protect against the well-known cert-symlink failure mode where acme-companion deletes top-level `<domain>.crt` / `<domain>.key` symlinks during a failed renewal attempt:

1. **Pinned image versions** prevent silent upstream behaviour drift.
2. **`nginx/heal-cert-symlinks.sh`** runs as the nginx-proxy entrypoint before nginx boots, recreating any missing symlinks.
3. **`cert-heal`** sidecar runs the heal script in a loop every `INTERVAL_SEC` seconds (default 15) so symlinks deleted mid-day are restored quickly.
4. **`nginx/tls-healthcheck.sh`** marks the container unhealthy when `ssl_reject_handshake on;` is present in generated config or when a per-domain dir exists without its top-level symlinks — Docker's restart policy kicks the entrypoint again.
5. **`cert-backup`** sidecar snapshots the certs named volume daily into `./backups/certs/` for disaster recovery without burning Let's Encrypt rate limits.

Details and manual recovery steps in `docs/tls-recovery.md`.

The frontend nginx config (`frontend/nginx.conf`) uses Docker's embedded DNS (`resolver 127.0.0.11`) and a `proxy_pass` variable so that backend container restarts (which assign a new IP) don't strand cached DNS in the frontend's nginx and cause 502s.

## Conventions

- Python 3.11+, async throughout.
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`.
- `safe_execute()` wraps all skill execution with `logger.exception()` on failure.
- Config via YAML (file-only keys) + DB `config` table (everything else) + Pydantic validation. Startup log prints effective values AFTER DB overrides are applied.
- SQLite with WAL mode. Schema versioned via numbered migration scripts. **No explicit `BEGIN` in write methods** — Python sqlite3's default deferred isolation auto-begins on the first DML, and explicit `BEGIN` conflicts when other writers are active on the same connection.
- Paper mode by default — live trading requires explicit `mode: live`.
- All trade/position queries filter by `ctx.config.mode` — paper and live data never mix.
- Trades track `origin` (`system` / `adopted`) and `mode` (`paper` / `live`). Signals, pending_trades, and predictions also carry `mode` so bulk-delete and analytics can scope cleanly.
- Pending trades use Python ISO timestamps (not SQLite `datetime('now')`) for correct expiry comparison.
- Broker `_retry_api_call` skips permanent errors (validation, margin, auth) — only retries transient errors.
- Before retrying order placement, trade-execute checks `kite.orders()` for recent matching orders. If found, the surviving order is **reconciled** as a successful trade (not retried; not marked failed).
- All Kite calls use the shared `KiteRateLimiter` (concurrency + time-based) plus, for `historical_data`, an additional tighter throttle inside `KiteDataProvider`.
- All timestamps use IST for market logic, UTC for DB storage.
- Frontend uses Vite + TypeScript + Tailwind. All UI timestamps localized to IST.
- Destructive actions require user confirmation in the UI.
- Tests: `cd backend && PYTHONPATH=src python -m pytest tests/ -v`.
- Frontend dev: `cd frontend && npm run dev`. Build: `npm run build`.

## Key Files

### Backend

- **`main.py`** — Entry point. Loads file config → opens DB → applies DB config → builds context with shared `KiteRateLimiter` → starts orchestrator + Telegram + dashboard. Syncs broker mode on startup and config change. `_sync_kite_data_token` propagates the Kite access token from broker to `KiteDataProvider` after auth/restore.
- **`orchestrator.py`** — Heartbeat pipeline, per-signal chain, manual approval queueing (sets `mode` on pending row), signal cleanup on execution failure.
- **`context.py`** — `AppContext`, Protocol types, `MarketHoursChecker`.
- **`config.py`** — Config models. `_MODE_HOLDING_DAYS` maps strategy modes to day ranges. `apply_db_config()` for hot-reload.
- **`broker/zerodha.py`** — MARKET → LIMIT conversion, SL-M → SL conversion, universal tick rounding, two-leg OCO GTT (`place_oco_gtt` / `delete_gtt` / `get_gtts`), circuit breaker, daily-auth-cache.
- **`broker/kite_rate_limiter.py`** — Async context manager combining concurrency cap + min-interval rate limit.
- **`data/kite_data.py`** — Optional Kite data provider. Pagination for intraday windows beyond Kite's per-call limit; pre-warmed instrument cache; historical-specific throttle; quote includes circuit limits and depth.
- **`data/ingester.py`** — Fallback chain. Quality + staleness validation.
- **`data/nse_symbols.py`** — Universe constituent live fetch + bundled fallback. `parse_constituent_csv` filters `Series=EQ` and `DUMMY*` placeholders.
- **`data/db.py`** — SQLite layer. `resolve_symbols_with_replacements`, mode-scoped queries, `set_trade_gtt`, `bulk_delete` (paper/live correctly mode-filtered including signals & pending_trades).
- **`skills/ingest_universe.py`** — Cached → live → bundled resolver. Calls `record_fetch_failure` on per-symbol errors so quarantine cascades.
- **`skills/ingest_data.py`** — Deep ingest (news + sentiment + fundamentals). Per-symbol NSE corp-actions limited to open positions + top watchlist (no longer hardcoded `seed_symbols`).
- **`skills/generate_signals.py`** — Balanced dual-model, intraday cutoff, SELL → MIS for non-holdings, intraday session caps via `apply_session_caps`.
- **`skills/risk_check.py`** — Pending trades count toward both `max_open_positions` AND `max_portfolio_exposure_pct` (notional-weighted).
- **`skills/trade_execute.py`** — Mode-mismatch safety check; reconciliation when `place_order` raises but the order actually placed; `_attach_oco_gtt` after CNC fill.
- **`skills/position_monitor.py`** — Client-side target/SL detection (skipped when `gtt_id` present), trailing SL with try-except, position adoption from holdings.
- **`skills/backfill_data.py`** / **`backfill_intraday.py`** — Default symbol set = watchlist + user_watchlist + regime index (not `seed_symbols`). Per-symbol delay configurable; daily and intraday share the same backbone with different defaults.
- **`skills/model_retrain.py`** — `_prepare_training_data` maintains a stable feature_names list across all samples (backfills 0.0 for late-appearing keys) so `np.array(X)` always succeeds.
- **`skills/report_generate.py`** — Daily (16:00) and weekly (Friday).
- **`strategy/ml_signal.py`** — XGBoost training. `tree_method='hist'` for memory-efficient training over multi-year history.
- **`strategy/holding_period.py`** — ATR multiplier interpolation, `apply_session_caps`, `adjust_sell_for_holdings`.
- **`telegram_bot.py`** — `/review` works for any NSE symbol. `/approve` uses symbol name. Token sync on `/auth` propagates to `KiteDataProvider`.
- **`dashboard/app.py`** — Mode passed to all trade/position/prediction queries. `POST /api/positions/{trade_id}/close` for per-position exit. `POST /api/review` for ML review of any symbol.

### Frontend

- **`pages/PositionsPage.tsx`** — Position list with price-level visualisation. Close button per row → `useClosePosition` mutation.
- **`pages/HoldingsPage.tsx`** — Multi-select checkboxes, bulk lock/unlock, ML review panel.
- **`pages/WatchlistPage.tsx`** — Quick ML Review input for any symbol.
- **`pages/IntegrationsPage.tsx`** — Per-service "Mark Inactive" toggles for Gemini and Telegram (flips `llm.enabled` / `notifications.telegram.enabled`). Status pills + test buttons.
- **`pages/DataManagementPage.tsx`** — Mode-scoped bulk delete buttons (paper/live now correctly include signals & pending_trades; standalone all-modes groups for global wipes).
- **`pages/SettingsPage.tsx`** — Click-to-toggle info tooltips. Universe dropdown: Nifty 50 / 100 / 200 / 500.
- **`components/PendingTradesBanner.tsx`** — Approval UI with header showing trade count + total qty + total investment. Per-row Investment column. Override editor.
- **`components/PositionsTable.tsx`** — Close button per row.
- **`components/StatusBadge.tsx`** — Header pills: Healthy/Degraded, mode (live/paper), KILL SWITCH (when active).

### Infrastructure

- **`docker-compose.yml`** — Pinned `nginx-proxy:1.10.1` and `acme-companion:2.6.3`. nginx-proxy entrypoint invokes the symlink heal script. `cert-heal` and `cert-backup` sidecars. Frontend depends on backend healthcheck (start_period 10s, interval 10s).
- **`backend/Dockerfile`** — Healthcheck tightened to detect ready state ~20s after start.
- **`frontend/nginx.conf`** — `resolver 127.0.0.11` + variable in `proxy_pass` for resilient backend resolution.
