> Detail extracted from the project overview (`CLAUDE.md`), which links here. Refer to it when you need this depth.

## Architecture

### Abstraction Layers (ABCs)

- **`BrokerBase`** (`broker/base.py`) → `ZerodhaBroker`. Order placement, GTT (Good Till Triggered) two-leg OCO orders for CNC, position/holdings/margins queries, daily auth lifecycle. Paper mode simulates fills using Kite-compatible field names (`filled_quantity`, `average_price`, `tradingsymbol`). All Kite calls go through `KiteRateLimiter` (see below).
- **`LLMBase`** (`llm/base.py`) → `GeminiLLM`. 7 methods: `ping`, `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`.
- **`MarketDataBase`** (`data/base.py`) → `MarketDataIngester` orchestrates a fallback chain. When `market_data.kite_data_enabled` is true, the chain is:
  - **daily**: `KiteDataProvider` → `JugaadDataProvider` → `YFinanceProvider`
  - **intraday (5min/15min/1m)**: `KiteDataProvider` → `TVDatafeedProvider`
  When disabled, Kite is removed and jugaad/yfinance/tvdatafeed handle their lanes. Each provider has staleness validation and per-bar quality checks (high ≥ low, close in range).
- **`NewsSource`** (`news/base.py`) → MoneyControl, ET Markets, LiveMint (RSS); NSE Official (API); Google Finance (scraper).
- **`MLBase`** (`strategy/ml_base.py`) → `XGBoostSignalModel` with Platt scaling calibration, config-driven regularized hyperparameters with early stopping (`retraining.xgb`), and `tree_method='hist'` for memory-efficient training.

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
- `compute_schedule() -> str | None` — for CRON skills, returns the LIVE cron
  expression resolved from `self.ctx.config`. The scheduler calls this every
  tick (not the cached `self.schedule`), so a schedule changed in the Settings
  UI (which hot-replaces `ctx.config`) takes effect within one ~30s tick, no
  restart. `__init__` sets `self.schedule = self.compute_schedule()`; skills
  with a hardcoded schedule inherit the base default (returns `self.schedule`).
- Access to shared context via `self.ctx`

Skills are registered in `SKILL_REGISTRY` dict in `skills/__init__.py`.

**Start/Stop schedules**: each CRON skill's auto-fire can be paused independently
of manual `Run Now`. Paused skill names are persisted as a JSON list in
`system_state.disabled_schedules`; `CronScheduler._check_and_fire` reads it fresh
each tick and skips paused skills. `GET /api/skills` reports per-skill `enabled`
(bool for CRON, null otherwise) + `next_run`; `POST /api/skills/{name}/schedule`
`{enabled}` toggles it. Surfaced on the Skills page as a Start/Stop control.

### Heartbeat Pipeline (market hours, every 15min — configurable)

```
expire-pending → health-check → ingest-data → depth-snapshot → market-scan → generate-signals
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

`db.get_todays_signaled_symbols` dedups today's signals so a symbol doesn't re-fire repeatedly, but distinguishes terminal from transient dispositions. A symbol with one of the **retryable** dispositions (`risk_rejected`, `expired`, `trade_execute_failed`, `skill_error`) is re-evaluated each heartbeat **until** its count of retryable signals today reaches `risk.max_risk_rejected_retries_per_day` (default 5). After that, the cap engages and the symbol is dedup-blocked for the rest of the day. Any **non-retryable** disposition (`executed`, `llm_rejected`, `awaiting_approval`, in-flight NULL) blocks immediately. The **deferred** disposition `time_blocked` (signal generated outside the order window) is a third class — re-evaluated freely AND cap-exempt, since the underlying condition is a scheduler edge case ("wait N minutes until `market_hours.order_start`"), not a real signal problem. Mode-scoped so paper and live retry budgets stay independent.

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
- **Manual close** — `POST /api/positions/{trade_id}/close` (UI: red Close button per row) cancels SL and target orders, deletes GTT, places market exit at the broker, computes realised PnL with costs, closes the row. `close_position` is **idempotent** (guarded by `status != 'closed'`, returns a bool) so a manual close racing position-monitor's exit (or a duplicated postback) can't double-count PnL or write a second audit row. If the exit price can't be determined (broker fill status + live quote both unavailable) the row still closes (the order did reach the broker) but a loud alert fires that the recorded PnL is unreliable and needs reconciliation — it is no longer silently booked as ~0.
- **Zerodha postback** (`POST /api/auth/zerodha/postback`) — verifies `SHA-256(order_id + order_timestamp + api_secret)` against the body's `checksum` (rejects 401 on mismatch). Looks up the trade via `db.find_trade_by_order_id` and reacts to terminal statuses: entry REJECTED → trade marked failed + alert; entry COMPLETE → backfills fill_price / slippage; SL COMPLETE → cancels the resting target LIMIT (ghost-recovery closes the row next cycle); SL REJECTED → loud alert (position unprotected); target LIMIT COMPLETE → cancels the SL leg. Polling still authoritative — postback is a latency optimisation.
- **Square-off** — CRON skill at `market_hours.square_off` (default 15:15) cancels SL + target orders then market-exits open MIS positions. Ignores `transaction_mode` (manual mode does NOT block EOD square-off — Zerodha auto-squares at 15:30 with penalty regardless). `/kill` runs it with `force=True` for everything including CNC.

### Optional Risk Gates (default off)

All under `risk.*`, opt-in. Watch one or two paper sessions to calibrate before enabling:

- **`regime_gate`** — refuses BUYs when cross-sectional `universe_breadth < min_breadth_for_buy` (0.40), SELLs when breadth > max threshold (0.60). Sizes up to `bullish_size_multiplier` × in strongly-favourable regimes. Live breadth computed once per heartbeat via `db.compute_live_regime` from today's daily-bar returns; cached on the skill instance.
- **`liquidity_gate`** — refuses positions whose size > `max_pct_of_top5` (10%) of the relevant side of the Kite top-5 book. Requires `market_data.kite_data_enabled`.
- **`depth_gate`** — does NOT hard-block. Maps order-book imbalance `(total_buy_qty − total_sell_qty) / (total_buy_qty + total_sell_qty)` to a position-**size multiplier** in `[min_size_multiplier, 1.0]` (default floor 0.4): a neutral / favourable book → full size, the worst-possible opposing book → 40%. Linear ramp between. A single order-book snapshot is too noisy to veto a signal that already passed the model + LLM + every other gate, so it sizes down rather than refusing. Same data source as liquidity_gate.
- **`institutional_flow`** — conviction multiplier on position size based on (a) recent bulk/block deals on the symbol (last N days, BUY count − SELL count) and (b) today's FII net flow vs `fii_net_threshold_cr`. Aligning direction scales up; opposing scales down by 1/multiplier. Reads `bulk_deals` and `fii_dii_daily` tables populated by ingest-data.
- **`earnings_blackout_days`** (default 0 = off) — hard-blocks new entries in any symbol with a scheduled earnings / board-meeting event within N calendar days. Reads the `economic_events` table (NSE corp-actions scraper). Earnings gaps routinely move stocks ±5-20% overnight, wider than any ATR-based SL.
- **`max_portfolio_beta`** (default 0 = off) — caps the sum of `(position notional × |beta|)` across open positions + the candidate signal at `max_portfolio_beta × capital`. Beta is a CAPM-style regression of the symbol's 60-day daily returns against an equal-weight cross-sectional market proxy (`db.compute_symbol_beta`, per-heartbeat cached on the skill). Catches "every position is a high-beta name" correlated-drawdown setups.
- **`max_risk_rejected_retries_per_day`** — caps daily retries of risk_rejected / expired / trade_execute_failed / skill_error dispositions (default 5).

**Multiplier discipline (`risk.risk_uplift_cap`, default 1.5)** — conviction / regime / institutional-flow multipliers stack multiplicatively. After all of them, risk-check re-clamps so the effective rupees-at-risk never exceeds `max_risk_per_trade_pct × risk_uplift_cap`. Without this a strongly-favourable signal (1.5 × 1.5 × 1.2 = 2.7×) silently ran 5%+ risk. `conviction_sizing` is the single confidence-scaling path (the legacy `confidence_scaled_sizing_enabled` knob has been removed); a cumulative "final size N (base B, net multiplier M×)" audit line logs the combined effect.

**Drift auto-suspend (`risk.drift_auto_suspend_enabled`, default off)** — when on, the daily `drift-watch` CRON (16:30 IST) sets a `signal_gen_suspended_by_drift` system_state flag on a >15pp win-rate decay or signal-class collapse. `generate-signals` reads it at `execute()` start and short-circuits to a no-op until the next successful `model-retrain` clears it (or the user clears it via `DELETE /api/drift-suspension`). Position-monitor keeps running so open trades retain protection.

### Exit Tweaks (`risk.exit_tweaks`)

Applied to client-side-managed positions (no GTT, no MIS OCO):
- **`time_stop_enabled`** — exit intraday positions still open after `intraday_stop_after_min` (180) min with target-progress below `intraday_stop_progress_threshold` (30%). Catches the chop trade that never works nor breaks.
- **`volume_exit_enabled`** — exit when the last 5-min bar volume drops below `volume_exit_min_ratio` (30%) of the previous N bars' average AND the position is in 0.5R-2R profit. "Trend is dying."

Applied to all three trailing-SL paths (client-side + GTT + MIS OCO), default-on:
- **`tighten_trailing_enabled`** — step-up curve. Once target progress crosses `tighten_start_at_target_pct` (0.50), trailing-SL step shrinks by `tighten_step_decay` (0.15) per bucket of `tighten_step_size` (0.10), floored at `tighten_min_multiplier` (0.20). Defaults give 1.00 → 0.85 → 0.70 → 0.55 → 0.40 → 0.25 → 0.20 across 50% → 100% target progress. Centralised in `_trailing_step_multiplier` so the three paths can't drift.

### Margin Enforcement

`risk.margin_usage_enabled` defaults `False` (notional-only sizing, no leverage). When enabled, risk-check calls `kite.order_margins` per signal so insufficient-funds / special-margin rejections are caught at signal time rather than at place-order time. When the broker says no, the position is sized down proportionally to whatever fits.

### AppContext

`AppContext` dataclass (`context.py`) holds references to all subsystems via Protocol types: `config`, `db`, `broker`, `llm`, `market_data`, `notify`, `market_hours`, `event_bus`, `ml`, `news_aggregator`, `memory`.

### Inter-Skill Data Contracts

All data exchange between skills uses typed Pydantic models in `models/schemas.py`: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`, `NewsArticle`, `MLPrediction`, `BacktestResult`.

