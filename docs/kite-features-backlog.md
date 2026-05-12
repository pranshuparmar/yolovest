# Kite Connect Features Backlog

Identified during the Kite paid-plan integration. None block current
operation; these are improvements on top of the already-integrated
feature set.

## P0 — Reliability

### GTT (Good Till Triggered) orders for CNC SL/target

**What:** Kite's server-side conditional orders that persist until
triggered. Two flavours: single-trigger (one limit order on price hit)
and OCO ("two-leg", a stop-loss + target pair where firing one cancels
the other). Persist for up to 1 year.

**Why:** SL/target enforcement is currently client-side via
`position-monitor` running on the heartbeat cadence. While the
application is down — deploy, restart, network hiccup, Kite session
expiry — CNC positions sit unprotected. Each trail update also consumes
Kite quota via `modify_order`. A two-leg GTT placed at entry survives
all of that on the broker's side.

**Scope:**
- New methods on `ZerodhaBroker`: `place_gtt`, `modify_gtt`,
  `delete_gtt`, `get_gtts`.
- Track GTT IDs in `trades` table (new column `gtt_id`).
- `trade-execute`: after fill, place a two-leg GTT instead of (or in
  addition to) the SL order.
- `position-monitor`: modify GTT on trail instead of placing new SL.
- Migration to add `gtt_id` column.

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
