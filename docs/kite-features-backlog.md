# Kite Connect Features Backlog

Items identified during the Kite-paid-plan migration that we deliberately
deferred. Listed in the priority order I'd suggest tackling. None of these
block current operation; they're improvements over what we already have.

## P0 — Reliability

### GTT (Good Till Triggered) orders for CNC SL/target

**What:** Kite's server-side conditional orders that persist until triggered.
Two flavours: single-trigger (one limit order on price hit) and OCO
("two-leg", a stop-loss + target pair where firing one cancels the other).
Persist for up to 1 year.

**Why:** Currently SL/target enforcement is client-side via
`position-monitor` running every 15 minutes. If yolovest is down — deploy,
restart, network hiccup, Kite session expiry — CNC positions sit
unprotected. Each trail update also consumes Kite quota via `modify_order`.

A two-leg GTT placed at entry survives all of that on the broker's side.

**Scope:**
- New methods on `ZerodhaBroker`: `place_gtt`, `modify_gtt`, `delete_gtt`,
  `get_gtts`.
- Track GTT IDs in `trades` table (new column `gtt_id`).
- `trade-execute`: after fill, place a two-leg GTT instead of (or in
  addition to) the SL order.
- `position-monitor`: modify GTT on trail instead of placing new SL.
- Migration to add `gtt_id` column.

**Estimated work:** ~250-400 lines + tests + migration.

---

## P1 — Security

### Postback checksum verification

**What:** Kite signs every postback with
`checksum = SHA256(order_id + order_timestamp + api_secret)`. Our
`/api/auth/zerodha/postback` endpoint currently accepts any POST.

**Why:** The endpoint URL is published. Anyone who knows it can spoof
order updates, broadcasting fake `order_update` events over the WebSocket
to all dashboard clients.

**Scope:** ~30 lines in `dashboard/app.py`; compare the `checksum` field
in the body against locally-computed SHA256. Reject mismatches with 401.

**Estimated work:** Half a day.

---

## P1 — Observability

### Order `tag` parameter for provenance

**What:** Kite `place_order` accepts a `tag` (≤20 chars) that flows back
through `orders()` and postbacks.

**Why:** Right now we can't programmatically tell which skill / code path
placed a given order. The `origin='system'/'adopted'` column on `trades`
helps, but doesn't distinguish `trade-execute` from `square-off` from
manual Telegram `/trade`.

**Scope:** ~15 lines. Add a `tag` kwarg to `broker.place_order`, default
e.g. `"yolovest"`. Callers pass their identifier. Surface in audit log.

**Estimated work:** Half a day.

---

## P2 — Risk accuracy

### Pre-trade margin via `order_margins` / `basket_order_margins`

**What:** Kite returns the canonical margin/brokerage/tax/duty breakdown
for a proposed order *before* placement.

**Why:** Our current cash check is `entry × qty ≤ available_cash`, which
ignores brokerage (₹20/leg cap), STT (0.1% delivery, 0.025% intraday),
exchange charges, GST, SEBI fees, stamp duty. For ₹10k positions, charges
are ~0.5% — small but real. Also unblocks an accurate "total cost"
column in the PendingTradesBanner.

**Scope:**
- New method `broker.estimate_margin(orders)`.
- `risk-check`: replace simple cash check with margin check.
- API endpoint to expose breakdown for the pending-trades UI.

**Estimated work:** 1-2 days including UI surface.

---

## P2 — Real-time monitoring

### WebSocket streaming (KiteTicker)

**What:** Persistent WebSocket to `wss://ws.kite.trade`. Subscribe to
instrument tokens, receive tick-by-tick LTP/quote/depth updates.

**Why:** Heartbeat-polled quote currently gives us 15-minute resolution.
A target/SL crossing mid-window isn't acted on until the next heartbeat.
With WebSocket, `position-monitor` reacts in real time and we eliminate
quote/LTP REST quota usage entirely.

**Scope:**
- New `KiteTickerStreamer` class managing the WS connection (reconnect,
  subscription churn, heartbeat).
- New `position-monitor` mode that subscribes to currently-held symbols
  and reacts to tick events instead of timer.
- Graceful fallback to REST polling if WS connection fails.

**Estimated work:** 2-4 days. Non-trivial: persistent connection +
reconnection + state sync.

---

## P3 — Strategy features

### `convert_position` — promote winning MIS to CNC

**What:** Convert an open MIS (intraday) position to CNC (delivery)
before square-off, holding overnight.

**Why:** Today, profitable MIS trades hit auto-square-off at 3:15 PM and
the gain is locked. With conversion, a high-confidence MIS trade up
significantly near close could be held overnight on the swing thesis.

**Scope:** Strategic decision more than plumbing. New skill/method to
evaluate conversion candidates near square-off time. Touches risk
checks (margin requirements change CNC↔MIS), `square-off` skill.

**Estimated work:** Discussion first, then 1-2 days.

---

## P3 — Analytics

### `order_history` / `order_trades` for fill detail

**What:** `order_history(order_id)` returns the state-transition timeline
of an order. `order_trades(order_id)` returns individual fill records
for partial fills.

**Why:** Today we keep our own status snapshot. With history we'd get:
- Exact slippage (placement price vs fill price, with timestamps).
- Partial-fill behavior visibility.
- Better data for the existing `_get_slippage_penalty` feedback loop.

**Scope:** ~100 lines. Methods on broker + persistence of the history
into a new `order_history` table (or extend `trades` columns).

**Estimated work:** 1 day.

---

## Won't do — discontinued or out of scope

- **Bracket Order (BO), Cover Order (CO)** — discontinued by Zerodha for
  retail in 2020 (post peak-margin rules). Not available.
- **AMO (After-Market Orders)** — limited use; orders execute at next
  open with whatever price. Not worth the complexity.
- **Iceberg orders** — only useful for large orders (>₹10L). Not
  relevant at current capital.
- **Mutual fund endpoints** — out of scope for equity strategy.
- **Auction participation** — niche, short-delivery edge case.
- **`virtual_contract_note`** — duplicates `order_margins` for the cost
  breakdown.
