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

- ~~Modify GTT on trailing SL~~ — done. `ZerodhaBroker.modify_gtt`
  wraps `kite.modify_gtt` (re-supplies both legs);
  `position-monitor._maybe_trail_gtt_sl` raises the SL leg in place
  once the `trailing_sl_trigger_multiple` × initial risk threshold is
  hit. Partial-profit booking also calls `modify_gtt` to resize the
  GTT to the remaining quantity so subsequent fires don't get rejected.
- Postback handler verification + business logic (next P1 item) so a
  GTT firing closes the trade row in real time instead of waiting for
  position-monitor's next heartbeat to reconcile.

---

## ~~P1 — Security: postback checksum verification~~ (done)

`/api/auth/zerodha/postback` now reads the raw request body, computes
`SHA-256(order_id + order_timestamp + api_secret)`, and rejects with
401 when the result doesn't match the body's `checksum`. Spoofed
order-update broadcasts to dashboard clients are no longer possible.

In addition to the security fix, the postback now drives real business
logic instead of just broadcasting to the UI:

- Entry REJECTED → trade row marked `failed`, urgent Telegram alert.
- Entry COMPLETE → fill_price / slippage backfilled if not already set.
- SL COMPLETE → cancel resting target LIMIT (MIS OCO bookkeeping);
  ghost-recovery closes the DB row on the next heartbeat.
- SL REJECTED → loud Telegram alert (position is unprotected).
- Target LIMIT COMPLETE → cancel SL leg; ghost-recovery closes DB row.

The match is done by `db.find_trade_by_order_id` which searches all
three columns (`order_id`, `sl_order_id`, `target_order_id`). Orders
that don't match any local trade (e.g. GTT-triggered orders we never
saw an order_id for) are logged and left for ghost-recovery to clean
up by detecting the broker position vanishing.

---

## ~~P1 — Observability: order `tag` parameter~~ (done)

`BrokerBase.place_order` now accepts an optional `tag` kwarg (truncated
to Kite's 20-char limit). Callers pass an identifier so the placer is
visible in every `kite.orders()` row and on every postback:

  - `yv-entry`, `yv-entry-l1`, `yv-entry-l2` — trade-execute entries
  - `yv-sl` — stop-loss orders
  - `yv-tgt` — MIS resting target LIMIT
  - `yv-partial` — partial profit-booking exits
  - `yv-sqoff` — square-off market exits
  - `yv-close` — dashboard manual close
  - `yv-manual` — Telegram `/trade` / dashboard manual order placement

---

## ~~P2 — Risk accuracy: pre-trade margin~~ (done)

`BrokerBase.estimate_margin(legs)` wraps `kite.order_margins`;
ZerodhaBroker returns `{total, legs}` (per-leg breakdown with the
broker's exact charges block) or None on paper/offline.

Risk-check uses it when `margin_usage_enabled` is true — the broker's
canonical margin (which includes MIS leverage, STT, GST, exchange,
SEBI, stamp) replaces the `entry × qty` notional check. When position
size would exceed available cash, risk-check shrinks proportionally
rather than rejecting outright.

When `margin_usage_enabled` is false (current default) we keep the
notional check — accurate for CNC, intentionally conservative for MIS
(no leverage applied).

---

## ~~P2 — Real-time monitoring: KiteTicker~~ (LTP cache shipped)

`broker/kite_ticker.py` (`KiteTickerClient`) wraps `KiteTicker` with
an asyncio-friendly facade — `start`, `stop`, `subscribe`,
`unsubscribe`, `get_ltp`. Runs in threaded mode (Twisted reactor in a
background thread); auto-reconnect from the SDK; sync callbacks
marshal back to asyncio via `loop.call_soon_threadsafe`.

Lifecycle wired in `main.py`: when `market_data.kite_websocket_enabled`
is true and the broker has a restored access token, the ticker is
created, started, and attached to `ctx.ticker`. `position-monitor`
subscribes to every open-position symbol each cycle (idempotent), and
`_get_ltp_with_retry` prefers the cached LTP (max 5s old) before
falling back to the existing REST path. Sub-second target/SL detection
becomes the primary path; REST stays as a safety net.

Subscription mode is `MODE_LTP` (cheapest, 8-byte payload) — that's
enough for the target/SL use case. `MODE_QUOTE` / `MODE_FULL` (OHLC,
volume, depth) can be enabled per-symbol later if a charting feature
needs them.

**Not in this round:** order-update bridge from the ticker into the
postback business logic. HTTP postbacks already cover that path with
checksum verification; using both would just be redundant. Wire it up
later if HTTP postbacks turn out to be lossy in practice.

**Default:** off (`kite_websocket_enabled: false`). Opt-in until the
user has tested it on their EC2 setup.

---

## P3 — Strategy features

### Single-leg entry GTT for breakouts (deferred)

**What:** Place a single-leg GTT that fires a BUY order only when
price breaks above a configured trigger, rather than sitting on a
resting LIMIT order that may or may not fill.

**Why deferred:** the current signal-generation flow produces entries
at or near LTP — there's no breakout-style signal that would benefit
from this. The primitive (`broker.place_entry_gtt` + a
`pending_entry_gtt` trade status + a reconciler that promotes the row
to `open` when the GTT fires) is straightforward, but the higher-level
integration (when to use entry GTT vs immediate LIMIT, lifecycle on
stale signals, risk-check timing) needs a breakout strategy module
first. Revisit when that exists.

**Scope when picked up:**
- `ZerodhaBroker.place_entry_gtt(symbol, side, qty, trigger, limit)`
- New trade status `pending_entry_gtt` (no migration needed, just a
  string value).
- Trade-execute branch: if `signal.entry_price > LTP * (1 + threshold)`
  for BUY (or `< LTP * (1 - threshold)` for SELL) and product is CNC,
  use entry GTT instead of LIMIT.
- Position-monitor reconciler: for `pending_entry_gtt` rows, watch GTT
  status; on `triggered`, fetch the resulting order id, capture fill
  price, transition trade to `open`, then attach the standard
  exit-side OCO GTT.
- Telegram `/breakout` command and a `/api/breakouts/{trade_id}/cancel`
  endpoint for manual cancellation.

---

### ~~`convert_position` — manual MIS → CNC~~ (primitive done)

`BrokerBase.convert_position(symbol, qty, from, to, side)` wraps
`kite.convert_position`. Exposed via
`POST /api/positions/{trade_id}/convert` with `{"to_product": "CNC"}`.
Updates the local trade row's `product`, cancels any MIS-side broker
OCO orders (resting target LIMIT + SL) since those are product-
specific, and sends a Telegram alert.

**Still open under this theme:**
- Auto-decision logic — `square-off` could evaluate "profitable
  MIS trades up >X% with confidence >Y" near the 3:15 cutoff and
  call `convert_position` instead of `_close_single_position`.
  Skipped because the heuristic is opinionated; keeping it as an
  explicit user action for now.
- UI button on the positions table to trigger conversion in one
  click. Currently only API + Telegram.

---

## P3 — Analytics

### ~~`order_history` / `order_trades` for fill detail~~ (done)

`BrokerBase` gained `get_order_history(order_id)` and
`get_order_trades(order_id)`; `ZerodhaBroker` wraps the SDK calls.
`GET /api/trades/{trade_id}/order-detail` aggregates history + fills
across every order id attached to a trade (entry / SL / target).

The trade detail page surfaces this behind a "Fetch from broker"
toggle (kept lazy so opening a trade page doesn't fire 3 extra API
calls every time). Renders the lifecycle table (timestamps, status
transitions, filled qty, avg price, broker notes) and the per-fill
table (essential for partial-fill cases).

Not persisted to DB — fetched live each click so the data is always
current and there's no migration to maintain.

---

## ~~P2 — Adaptive risk: trailing SL via `modify_gtt`~~ (done)

Shipped in commit `c472ffb` alongside the partial-booking GTT resize.
`ZerodhaBroker.modify_gtt` wraps `kite.modify_gtt` (both legs re-
supplied as the API requires). `position-monitor._maybe_trail_gtt_sl`
raises the SL leg in place once the trailing condition fires; target
leg untouched.

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
