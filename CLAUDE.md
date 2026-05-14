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

### KiteTicker WebSocket

Optional sub-second LTP feed (opt-in via `market_data.kite_websocket_enabled`). When enabled and the broker is authenticated, `main.async_main` instantiates `broker.kite_ticker.KiteTickerClient` and attaches it to `ctx.ticker`. Position-monitor subscribes to every open-position symbol each cycle (idempotent), and `_get_ltp_with_retry` reads from the cache first (max 5s freshness) before falling back to REST. Mode is `MODE_LTP` — the 8-byte payload is enough for target/SL; richer modes (`MODE_QUOTE` / `MODE_FULL`) are available on the wrapper but not consumed yet.

The ticker also bridges `on_order_update` text frames into `dashboard.app._apply_order_postback` — the same business logic the HTTP postback handler runs. WebSocket is the primary push channel because Kite postbacks are explicitly best-effort with no retry; the HTTP handler stays as a backup, and the heartbeat ghost-recovery (which cancels dangling exit orders) is the last-resort reconciler. The three layers are idempotent — if the same event arrives via multiple channels, later hits are no-ops.

Tick frames also fan out to dashboard clients as `tick_update` events, throttled to one broadcast per symbol per second so the browser socket doesn't drown. `frontend/src/hooks/useLtpStream.ts` maintains the per-symbol LTP map; `PositionsTable` consumes it to render live LTP + move% columns next to entry/SL/target.

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
expire-pending → health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → [manual: queue pending] OR [auto: trade-execute] → predict-track
  → position-monitor (always runs)
```

`expire-pending` runs first every cycle (`HeartbeatOrchestrator._execute_pipeline`) so abandoned pending trades free their `max_open_positions` / `max_trades_per_day` / `max_portfolio_exposure_pct` budgets before risk-check evaluates the day's signals. `execution.pending_expiry_minutes` (default 30) governs the timeout.

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
6. Pending trades auto-expire after `execution.pending_expiry_minutes` (default 30). The heartbeat sweeps every cycle (`db.expire_pending_trades`); the dashboard endpoint defends the same way as a backstop.

Risk-check includes pending-trade notional in the `max_portfolio_exposure_pct` check, so the queue can't accumulate past the cap. Square-off (EOD CRON) **ignores** manual mode and force-closes MIS positions — the broker auto-square at 15:30 is the binding deadline, asking for approval there buys nothing.

### Signal-Disposition Retry Caps

`db.get_todays_signaled_symbols` dedups today's signals so a symbol doesn't re-fire repeatedly, but distinguishes terminal from transient dispositions. A symbol with one of the **retryable** dispositions (`risk_rejected`, `expired`, `trade_execute_failed`, `skill_error`) is re-evaluated each heartbeat **until** its count of retryable signals today reaches `risk.max_risk_rejected_retries_per_day` (default 5). After that, the cap engages and the symbol is dedup-blocked for the rest of the day. Any **non-retryable** disposition (`executed`, `llm_rejected`, `awaiting_approval`, in-flight NULL) blocks immediately. Mode-scoped so paper and live retry budgets stay independent.

This closes the failure mode where 12 transient risk-rejections (broad-market chop / depth / correlation with pending / exposure cap) at 9:30 would have permanently blocked those symbols for the rest of the day, leaving `max_trades_per_day` unused.

### Position Adoption & Exit

`position-monitor` auto-adopts untracked broker positions/holdings:
1. Compares `broker.get_positions()` + `broker.get_holdings()` vs local DB.
2. Untracked positions (not locked) are adopted with ATR-based SL/target.
3. Trade record created with `origin='adopted'`, auto-added to watchlist.
4. Locked holdings are never adopted or auto-managed.

Exit paths:
- **Broker-side GTT** (CNC only) — placed by `trade-execute._attach_oco_gtt` after entry fill. Pre-flight validation rejects nonsense SL/target combos; if the broker already has ≥45 active GTTs (cap is 50) we skip placement and fall back to client-side. `position-monitor` skips client-side target/SL checks when `gtt_id` is set; ghost-position reconciliation closes the DB row when the GTT fires and the broker position vanishes. Each cycle, `_reconcile_gtts` cross-checks `trades.gtt_id` against `broker.get_gtts()` — GTTs that have been cancelled, rejected, expired, or vanished get their `gtt_id` wiped so client-side detection resumes. Latest GTT status is cached in `trades.gtt_status` and rendered as a badge on the trade detail page. `_maybe_trail_gtt_sl` raises the SL leg in place via `broker.modify_gtt` once trailing-SL triggers, and partial-profit booking resizes the GTT to the remaining quantity so later fires aren't rejected.
- **Broker-side MIS OCO** — Kite doesn't allow GTT on MIS, so `trade-execute._attach_mis_target_limit` places a resting LIMIT order at the target alongside the SL after entry fills. `position-monitor._enforce_mis_oco` watches both order statuses each cycle and cancels the surviving leg when one fills. `position-monitor._maybe_trail_mis_sl` lifts the broker-side SL trigger in place via `kite.modify_order` once trailing-SL triggers (mirror of `_maybe_trail_gtt_sl` for CNC), so MIS positions ratchet their breakeven floor the same way GTT-managed CNC trades do. Ghost-position reconciliation closes the DB row.
- **Client-side detection** — fallback for trades that have neither `gtt_id` nor both `target_order_id`+`sl_order_id` (older rows, or LIMIT placement failed). `position-monitor` exits when LTP crosses target (with `risk.target_early_exit_pct` buffer) or SL.
- **Manual close** — `POST /api/positions/{trade_id}/close` (UI: red Close button per row) cancels SL and target orders, deletes GTT, places market exit at the broker, computes realised PnL with costs, closes the row.
- **Zerodha postback** (`POST /api/auth/zerodha/postback`) — verifies `SHA-256(order_id + order_timestamp + api_secret)` against the body's `checksum` (rejects 401 on mismatch). Looks up the trade via `db.find_trade_by_order_id` and reacts to terminal statuses: entry REJECTED → trade marked failed + alert; entry COMPLETE → backfills fill_price / slippage; SL COMPLETE → cancels the resting target LIMIT (ghost-recovery closes the row next cycle); SL REJECTED → loud alert (position unprotected); target LIMIT COMPLETE → cancels the SL leg. Polling still authoritative — postback is a latency optimisation.
- **Square-off** — CRON skill at `market_hours.square_off` (default 15:15) cancels SL + target orders then market-exits open MIS positions. Ignores `transaction_mode` (manual mode does NOT block EOD square-off — Zerodha auto-squares at 15:30 with penalty regardless). `/kill` runs it with `force=True` for everything including CNC.

### Optional Risk Gates (default off)

All under `risk.*`, opt-in. Watch one or two paper sessions to calibrate before enabling:

- **`regime_gate`** — refuses BUYs when cross-sectional `universe_breadth < min_breadth_for_buy` (0.40), SELLs when breadth > max threshold (0.60). Sizes up to `bullish_size_multiplier` × in strongly-favourable regimes. Live breadth computed once per heartbeat via `db.compute_live_regime` from today's daily-bar returns; cached on the skill instance.
- **`liquidity_gate`** — refuses positions whose size > `max_pct_of_top5` (10%) of the relevant side of the Kite top-5 book. Requires `market_data.kite_data_enabled`.
- **`depth_gate`** — refuses when `(total_buy_qty − total_sell_qty) / (total_buy_qty + total_sell_qty)` opposes the signal direction past threshold. Same data source as liquidity_gate.
- **`institutional_flow`** — conviction multiplier on position size based on (a) recent bulk/block deals on the symbol (last N days, BUY count − SELL count) and (b) today's FII net flow vs `fii_net_threshold_cr`. Aligning direction scales up; opposing scales down by 1/multiplier. Reads `bulk_deals` and `fii_dii_daily` tables populated by ingest-data.
- **`max_risk_rejected_retries_per_day`** — caps daily retries of risk_rejected / expired / trade_execute_failed / skill_error dispositions (default 5).

### Exit Tweaks (`risk.exit_tweaks`)

Applied to client-side-managed positions (no GTT, no MIS OCO):
- **`time_stop_enabled`** — exit intraday positions still open after `intraday_stop_after_min` (180) min with target-progress below `intraday_stop_progress_threshold` (30%). Catches the chop trade that never works nor breaks.
- **`volume_exit_enabled`** — exit when the last 5-min bar volume drops below `volume_exit_min_ratio` (30%) of the previous N bars' average AND the position is in 0.5R-2R profit. "Trend is dying."

Applied to all three trailing-SL paths (client-side + GTT + MIS OCO), default-on:
- **`tighten_trailing_enabled`** — step-up curve. Once target progress crosses `tighten_start_at_target_pct` (0.50), trailing-SL step shrinks by `tighten_step_decay` (0.15) per bucket of `tighten_step_size` (0.10), floored at `tighten_min_multiplier` (0.20). Defaults give 1.00 → 0.85 → 0.70 → 0.55 → 0.40 → 0.25 → 0.20 across 50% → 100% target progress. Centralised in `_trailing_step_multiplier` so the three paths can't drift.

### Margin Enforcement

`risk.margin_usage_enabled` defaults `True`. Before placing, risk-check calls `kite.order_margins` per signal so insufficient-funds / special-margin rejections are caught at signal time rather than at place-order time. When the broker says no, the position is sized down proportionally to whatever fits.

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
| `signals` | Generated signals with `mode`, `disposition`, `attribution_json` (top-N TreeSHAP contributions stored per signal). Dedup via `get_todays_signaled_symbols` is mode-scoped and respects the retryable-disposition cap. |
| `predictions` | ML predictions with `mode`, `is_shadow`, `model_version`, `scored_at` columns. |
| `pending_trades` | Manual approval queue with `mode` column. Python ISO timestamps for correct expiry comparison. |
| `quarantined_symbols` | Auto-blocked after 3 fetch failures (transient errors don't count — see Conventions). `replacement_symbol` lets the user substitute (e.g. ZOMATO → ETERNAL). |
| `locked_holdings` | User-protected holdings — never auto-sold or auto-adopted. |
| `config` | Key-value store for UI-editable settings (dot-notation keys). |
| `system_state` | Long-lived key-value: `kite_access_token`, `kill_switch`, `initial_capital`, `universe_constituents:<universe>`, etc. `partial_booked_{trade_id}` sentinels are auto-cleaned on `close_position`. |
| `ohlcv` | OHLCV bars. Unique on (symbol, interval, timestamp). `delivery_pct` column stamped by ingest-data when NSE returns it. |
| `bulk_deals` | NSE bulk/block deals — per-symbol institutional accumulation/distribution events. Powers the `institutional_flow` risk multiplier + `bulk_deal_*` ML features. Unique on (deal_date, symbol, client, side, qty, price). |
| `fii_dii_daily` | One row per date with FII + DII buy/sell/net values in ₹ crore. Powers FII regime side of the `institutional_flow` multiplier. |
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
| `/watch [add\|rm SYM ...]` | List or mutate `user_watchlist` |
| `/quarantine [unblock\|replace SYM ...]` | List quarantined symbols / clear / route to replacement |
| `/lock SYM [SYM ...]` / `/unlock SYM` | Protect / unprotect holdings from auto-management |
| `/mode [auto\|manual]` | Show or hot-flip `execution.transaction_mode` (uses same `apply_db_config` path as the dashboard) |
| `/symbol SYM` | One-stop snapshot: price + day-change, quarantine, 5d avg delivery %, latest signal + top-5 attribution, last 5 trades, last 5 bulk deals |

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
| `risk` | `max_risk_per_trade_pct`, `max_open_positions`, `max_single_stock_pct`, `max_portfolio_exposure_pct` (counts pending notional), `daily_loss_limit_pct`, `weekly_loss_limit_pct`, `max_same_sector_positions`, `target_early_exit_pct` (default 0.0015), `loss_cooldown_minutes` (portfolio + per-symbol), `max_risk_rejected_retries_per_day` (default 5), `margin_usage_enabled` (default `true`) |
| `risk.regime_gate` / `risk.liquidity_gate` / `risk.depth_gate` / `risk.institutional_flow` | All default-disabled. See `### Optional Risk Gates` for what each does. |
| `risk.exit_tweaks` | Time-stop / volume-exhaustion (default off), trailing-SL tighten step-up curve (default on). |
| `market_hours` | `open`, `close`, `square_off`, `intraday_cutoff` |
| `execution` | `transaction_mode` (`auto`/`manual`), `max_order_retries`, `price_drift_max_pct`, `pending_expiry_minutes` (default 30) |
| `scanning` | `universe`, `shortlist_size`, `min_avg_daily_volume`, `seed_symbols` (cold-start fallback only) |
| `market_data` | `kite_data_enabled`, `news_enabled`, `scrapers_enabled`, `backfill_days` (daily, used by both ingest-universe and backfill-data), `intraday_backfill_days` (5-minute) |

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
- **GTT** (Good Till Triggered) is CNC-only at Zerodha. MIS positions get a broker-side resting LIMIT-target + SL pair (OCO enforced by position-monitor) instead.
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
- **`data/db.py`** — SQLite layer. `resolve_symbols_with_replacements`, mode-scoped queries, `set_trade_gtt`, `bulk_delete` (paper/live correctly mode-filtered including signals & pending_trades). `get_storage_stats` is 60s-TTL cached and invalidated by destructive endpoints (cleanup_table / bulk_delete / reset_all / restore_backup). `compute_live_regime` (cross-sectional breadth scan), `compute_recent_delivery_pct`, `count_recent_bulk_deals`, `get_latest_fii_dii` — all wired into risk-check at signal time. `record_fetch_failure` skips the counter on transient errors (rate limit / timeout / 5xx / SSL / unreachable).
- **`skills/ingest_universe.py`** — Cached → live → bundled resolver. Calls `record_fetch_failure` on per-symbol errors so quarantine cascades.
- **`skills/ingest_data.py`** — Deep ingest (news + sentiment + fundamentals). Per-symbol NSE corp-actions limited to open positions + top watchlist (no longer hardcoded `seed_symbols`).
- **`skills/generate_signals.py`** — Balanced dual-model, intraday cutoff, SELL → MIS for non-holdings, intraday session caps via `apply_session_caps`.
- **`skills/risk_check.py`** — Pending trades count toward both `max_open_positions` AND `max_portfolio_exposure_pct` (notional-weighted). Per-symbol cooldown on top of portfolio-wide (`minutes_since_last_loss_for_symbol`). Correlation gate includes same-direction pending trades in its comparison set (catches "3 correlated BUYs in one batch"). Opt-in regime / liquidity / depth / institutional-flow gates compose multiplicatively with the existing conviction multiplier, all capped by `max_single_stock_pct`.
- **`skills/trade_execute.py`** — Mode-mismatch safety check; reconciliation when `place_order` raises but the order actually placed; `_attach_oco_gtt` after CNC fill.
- **`skills/position_monitor.py`** — Mode-scoped (`get_open_positions(mode=...)`). Three trailing-SL paths use the same `_trailing_step_multiplier` step-up curve: client-side (`modify_sl_order`), GTT (`_maybe_trail_gtt_sl` → `modify_gtt`), MIS broker-OCO (`_maybe_trail_mis_sl` → `modify_sl_order`). `_check_auxiliary_exits` runs before target/SL for client-side positions and handles time-stop + volume-exhaustion. Position adoption from holdings remains.
- **`skills/backfill_data.py`** / **`backfill_intraday.py`** — Default symbol set = watchlist + user_watchlist + regime index (not `seed_symbols`). Per-symbol delay configurable; daily and intraday share the same backbone with different defaults.
- **`skills/model_retrain.py`** — `_prepare_training_data` maintains a stable feature_names list across all samples (backfills 0.0 for late-appearing keys) so `np.array(X)` always succeeds. Labels are **path-aware** (`_path_aware_label`): walks the future window bar-by-bar and labels BUY only when high reaches `entry × (1 + target_atr_mult × ATR%)` before low reaches `entry × (1 - sl_atr_mult × ATR%)`, mirroring the live target/SL geometry. Each model trains against its own multipliers — intraday uses `holding_periods.intraday` (0.6 / 0.3), swing uses `holding_periods.short_swing` (1.5 / 0.75). Per-sample feature merges:
  - **Universe regime** (`universe_breadth`, `universe_avg_return`) via `_compute_regime_index` — cross-sectional breadth proxy.
  - **Sector-relative** (`sector_breadth`, `sector_avg_return`, `relative_momentum`) via `_compute_sector_index` over `symbol_sectors` join — stock vs its industry cohort.
  - **Institutional flow** (`bulk_deal_buy_5d`, `bulk_deal_sell_5d`, `bulk_deal_net_5d`, `delivery_pct_avg_5d`) — pre-built lookup from `bulk_deals` + per-bar `ohlcv.delivery_pct`.
  - **Time-of-day** (`minutes_since_open`, `day_phase`) via `data/features.py::_minutes_since_open`.
  - **Feedback** (`fb_*`) including `fb_recent_loss_count` from `get_feedback_data`.
- **`skills/drift_watch.py`** — CRON 16:30 IST, calls `db.get_model_drift_stats(days=14)` and pushes the existing drift `warning` field to Telegram via `notify.send(alert_type="errors")`. Replaces the LLM-review safety net for autonomous mode; flag when realised win-rate drops > 15 pp over last 7d vs prior 7d.
- **`skills/report_generate.py`** — Daily (16:00) and weekly (Friday).
- **`strategy/ml_signal.py`** — XGBoost training. `tree_method='hist'` for memory-efficient training over multi-year history. When `bars_meta` is threaded through from `model_retrain._prepare_training_data`, fold-test predictions are scored via `strategy/walk_forward_backtest.run_walk_forward_backtest` (real PnL: simulated one-bar trades through `compute_transaction_costs`, sized by `risk_per_trade_pct × capital`, with configurable entry slippage). Falls back to the legacy `+1%/-0.5%` synthetic payoff when bars_meta isn't supplied (older tests). `metrics["backtest_source"]` is `walk_forward_real_pnl` or `synthetic_legacy`.
- **`strategy/walk_forward_backtest.py`** — `BarMeta`, `BacktestConfig`, `run_walk_forward_backtest`. Each prediction → real trade through entry slippage → walks the future window bar-by-bar via `_path_aware_exit` (target / SL / fallback to exit_close) → transaction costs → capital update. Tie-break when both barriers touched in the same bar = SL (keeps the Sharpe number hard to game). Returns `BacktestResult` with sharpe / max_drawdown_pct / win_rate / profit_factor / net_pnl / final_capital from the realised PnL series.
- **`strategy/holding_period.py`** — ATR multiplier interpolation, `apply_session_caps`, `adjust_sell_for_holdings`.
- **`telegram_bot.py`** — `/review` works for any NSE symbol. `/approve` uses symbol name. Token sync on `/auth` propagates to `KiteDataProvider`. State-mutation surface for Telegram-only operation: `/watch`, `/quarantine`, `/lock`/`/unlock`, `/mode` (hot-applies through the same `apply_db_config` path the dashboard PUT uses). `/symbol SYM` is the unified read snapshot (price + attribution + bulk deals + recent trades).
- **`dashboard/app.py`** — Mode passed to all trade/position/prediction queries. `POST /api/positions/{trade_id}/close` for per-position exit. `POST /api/review` for ML review of any symbol. New read endpoints surfaced this iteration: `GET /api/model-drift`, `GET /api/institutional-flows`, `GET /api/symbol/{symbol}/context`. `GET /api/symbol/{symbol}/ohlcv` normalises `1d` / `1day` / `day` → `daily` and returns `delivery_pct` per bar. `DELETE /api/backups/{filename}` with path-traversal protection. Postback handler cancels orphan SL / target legs on late entry-REJECTED to prevent unintended short positions.

### Frontend

- **`pages/PositionsPage.tsx`** — Position list with price-level visualisation. Close button per row → `useClosePosition` mutation.
- **`pages/HoldingsPage.tsx`** — Multi-select checkboxes, bulk lock/unlock, ML review panel.
- **`pages/WatchlistPage.tsx`** — Quick ML Review input for any symbol. Paginated user + algo tabs.
- **`pages/SymbolPage.tsx`** — Symbol detail: price chart with SMA 9/21/50 overlays, volume bars coloured by close-vs-open, delivery-% chart, quarantine banner, recent bulk-deals table, trade markers + linked trade history. New `/api/symbol/{symbol}/context` composes the extras in one round-trip.
- **`pages/TradeDetailPage.tsx`** — Reasoning timeline + "Why this signal?" panel rendering top-5 TreeSHAP feature attribution from `signals.attribution_json`.
- **`pages/ModelDriftPage.tsx`** — Predicted vs realised win-rate by day + calibration buckets per model version. Warning banner on > 15 pp 7d drop.
- **`pages/InstitutionalFlowsPage.tsx`** — FII / DII bar chart + paginated bulk/block deals table. Symbol filter.
- **`pages/IntegrationsPage.tsx`** — Per-service "Mark Inactive" toggles for Gemini and Telegram (flips `llm.enabled` / `notifications.telegram.enabled`). Status pills + test buttons.
- **`pages/DataManagementPage.tsx`** — Mode-scoped bulk delete buttons (paper/live now correctly include signals & pending_trades; standalone all-modes groups for global wipes). Page-shell decouples from slow storage-stats — backups + quarantine + bulk-delete render immediately. Per-backup Delete button.
- **`pages/SettingsPage.tsx`** — Click-to-toggle info tooltips. Universe dropdown: Nifty 50 / 100 / 200 / 500.
- **`pages/AuditPage.tsx`** — Audit log + server logs tabs, paginated.
- **`components/SymbolLink.tsx`** — Single source of truth for "click symbol → detail page". Used everywhere a symbol is rendered; preserves caller colour, stops propagation so row-level clicks still work.
- **`components/Pagination.tsx`** — Shared Prev / N of M / Next widget used by trades / predictions / audit / watchlist tabs / institutional-flows / etc.
- **`components/PendingTradesBanner.tsx`** — Approval UI with header showing trade count + total qty + total investment. Per-row Investment column. Override editor.
- **`components/PositionsTable.tsx`** — Close button per row.
- **`components/StatusBadge.tsx`** — Header pills: Healthy/Degraded, mode (live/paper), KILL SWITCH (when active).

### Infrastructure

- **`docker-compose.yml`** — Pinned `nginx-proxy:1.10.1` and `acme-companion:2.6.3`. nginx-proxy entrypoint invokes the symlink heal script. `cert-heal` and `cert-backup` sidecars. Frontend depends on backend healthcheck (start_period 10s, interval 10s).
- **`backend/Dockerfile`** — Healthcheck tightened to detect ready state ~20s after start.
- **`frontend/nginx.conf`** — `resolver 127.0.0.11` + variable in `proxy_pass` for resilient backend resolution.
