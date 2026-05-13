# Kite Connect Features Backlog

Identified during the Kite paid-plan integration. None block current
operation; these are improvements on top of the already-integrated
feature set.

## ~~P0 — GTT for CNC SL/target~~ (done in initial cut)

Two-leg OCO GTTs are placed automatically after a CNC trade fills.
The broker enforces target + stoploss without dependence on the app's
heartbeat. Implementation:

- `ZerodhaBroker.place_oco_gtt`, `delete_gtt`, `get_gtts`
- Migration 031 added `trades.gtt_id`
- `trade-execute._attach_oco_gtt` runs after a successful CNC fill
- `position-monitor` skips client-side target/SL detection on
  positions that have a GTT attached (avoids double exit)
- Manual close via `/api/positions/{trade_id}/close` deletes the
  GTT before placing the exit order

**Known limitation: GTT applies to CNC only.** Zerodha doesn't allow
GTT on MIS — intraday positions continue to rely on client-side
detection in position-monitor and on the 15:15 auto-square-off.

**Still open under this theme:**

- Modify GTT on trailing SL — currently trailing is disabled when
  `gtt_id` is set. A proper implementation would call `kite.modify_gtt`
  to raise the stoploss trigger as profit accrues. Skipped because
  Kite's modify_gtt requires re-supplying both legs in full; needs a
  clean API on the broker.
- Postback handler verification + business logic (next P1 item) so a
  GTT firing closes the trade row in real time instead of waiting for
  position-monitor's next heartbeat to reconcile.

---

## P1 — Security

### Postback checksum verification

**What:** Kite signs every postback with
`checksum = SHA256(order_id + order_timestamp + api_secret)`. The
`/api/auth/zerodha/postback` endpoint currently accepts any POST.

**Why:** The endpoint URL is published. Anyone who knows it can spoof
order updates, broadcasting fake `order_update` events over the
WebSocket to all dashboard clients.

**Scope:** compare the `checksum` field in the body against locally
computed SHA256, reject mismatches with 401.

---

## P1 — Observability

### Order `tag` parameter for provenance

**What:** Kite `place_order` accepts a `tag` (≤20 chars) that flows
back through `orders()` and postbacks.

**Why:** The codebase currently can't programmatically distinguish
which skill or code path placed a given order. The
`origin='system'/'adopted'` column on `trades` helps but doesn't
separate `trade-execute` from `square-off` or manual Telegram
`/trade` entries.

**Scope:** add a `tag` kwarg to `broker.place_order`, callers pass
their identifier, surface in audit log.

---

## P2 — Risk accuracy

### Pre-trade margin via `order_margins` / `basket_order_margins`

**What:** Kite returns the canonical margin/brokerage/tax/duty
breakdown for a proposed order before placement.

**Why:** The current cash check is `entry × qty ≤ available_cash`,
which ignores brokerage, STT, exchange charges, GST, SEBI fees, and
stamp duty. Replacing it with `order_margins` gives accurate pre-trade
sizing and unlocks an exact "total cost" column in the
PendingTradesBanner.

**Scope:**
- New method `broker.estimate_margin(orders)`.
- `risk-check`: replace simple cash check with margin check.
- API endpoint to expose breakdown for the pending-trades UI.

---

## P2 — Real-time monitoring

### WebSocket streaming (KiteTicker)

**What:** Persistent WebSocket to `wss://ws.kite.trade`. Subscribe to
instrument tokens, receive tick-by-tick LTP/quote/depth updates.

**Why:** Heartbeat-polled quotes give 15-minute resolution. A
target/SL crossing mid-window isn't acted on until the next heartbeat.
With WebSocket, `position-monitor` reacts in real time and quote/LTP
REST quota usage is eliminated.

**Scope:**
- New `KiteTickerStreamer` class managing the WS connection (reconnect,
  subscription churn, heartbeat).
- New `position-monitor` mode that subscribes to currently-held symbols
  and reacts to tick events instead of timer.
- Graceful fallback to REST polling if WS connection fails.

---

## P3 — Strategy features

### `convert_position` — promote winning MIS to CNC

**What:** Convert an open MIS (intraday) position to CNC (delivery)
before square-off, holding overnight.

**Why:** Profitable MIS trades hit auto-square-off and the gain is
locked. With conversion, a high-confidence MIS trade up significantly
near close could be held overnight on the swing thesis.

**Scope:** new skill/method to evaluate conversion candidates near
square-off time. Touches risk checks (margin requirements change
CNC↔MIS), `square-off` skill.

---

## P3 — Analytics

### `order_history` / `order_trades` for fill detail

**What:** `order_history(order_id)` returns the state-transition
timeline of an order. `order_trades(order_id)` returns individual fill
records for partial fills.

**Why:** Only a status snapshot is persisted today. History gives exact
slippage (placement price vs fill price with timestamps), partial-fill
visibility, and better feedback data for the existing
`_get_slippage_penalty` loop.

**Scope:** methods on broker + persistence into a new `order_history`
table (or extra columns on `trades`).

---

## P2 — Adaptive risk

### Trailing SL via `modify_gtt`

**What:** When a CNC position is in profit by ≥
`trailing_sl_trigger_multiple` × initial risk, modify the existing
GTT's stoploss leg to lock in some of the gain. Continue trailing
upward in `trailing_sl_step_pct` increments.

**Why:** Currently `position-monitor` skips trailing entirely when
`gtt_id` is set, because trailing requires modifying an existing SL
order and `kite.modify_gtt` needs both legs re-supplied in full
(not just the stoploss leg). Until we implement that, GTT-attached
positions get the broker-side exit guarantee but lose adaptive
trailing.

**Scope:**
- `ZerodhaBroker.modify_gtt(gtt_id, sl_trigger, sl_limit, target_trigger, target_limit, last_price, ...)`.
- `position-monitor` trail block: when `gtt_id` present and trail
  condition met, call `modify_gtt` instead of `modify_sl_order`.
- Test: trail moves SL up over multiple heartbeats.

---

## P3 — Backtest realism

### Replace synthetic +1%/−0.5% scoring with a real walk-forward sim

**What:** `ml_signal.py` currently scores each prediction with a
hardcoded payoff (+1% correct, −0.5% wrong, 0% HOLD) and computes
Sharpe / drawdown / win-rate off that synthetic stream. The metrics
shown on the ML Models dashboard are inflated by this geometry —
Sharpe > 5 looks great but doesn't translate to real PnL.

**Why:** A proper backtest walks the test set day-by-day applying the
full signal pipeline: risk-check, position sizing, entry slippage,
SL/target/holding-period exits, transaction costs. The resulting
equity curve is the honest basis for Sharpe and drawdown.

**Scope:** moderate refactor. New `strategy/backtest.py` that mirrors
the live pipeline but operates on historical OHLCV. Hooks into
`model_retrain` to replace the synthetic scoring block. Reuses
`compute_transaction_costs`, `apply_session_caps`, and the holding-
period logic so backtest geometry matches production exactly.

---

## P3 — Observability

### External uptime monitoring

**What:** Run a separate monitor (Uptime Kuma, healthchecks.io, etc.)
on a different host that hits `https://<domain>/api/health` every
minute and alerts on failure.

**Why:** The current healthcheck only fires Docker's restart policy
locally — if the whole host is down nobody is told. External
monitoring closes that gap, and also catches DNS / TLS / firewall
problems the in-container healthcheck can't see.

**Scope:** infrastructure change outside the repo. Recommend
configuring on the monitoring service side pointing at the public
endpoint, with Telegram or email as the alert channel.

---

## Out of scope

- **Bracket Order (BO), Cover Order (CO)** — discontinued by Zerodha
  for retail in 2020 (post peak-margin rules).
- **AMO (After-Market Orders)** — limited use; orders execute at next
  open with whatever price.
- **Iceberg orders** — designed for sizes exceeding exchange freeze
  limits, primarily F&O.
- **Mutual fund endpoints** — out of scope for equity strategy.
- **Auction participation** — short-delivery edge case.
- **`virtual_contract_note`** — duplicates `order_margins` for the
  cost breakdown.
