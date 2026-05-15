-- Audit trail for GTT lifecycle events. Logged by trade-execute (place),
-- position-monitor (modify on trailing SL / partial booking; reconcile
-- status changes), and square-off / dashboard close (delete). Lets us
-- debug "why didn't this position close at target" by reviewing the
-- exact sequence of placements, modifications, status changes, and
-- deletions for any trade.

CREATE TABLE IF NOT EXISTS gtt_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL DEFAULT (datetime('now')),
    trade_id TEXT,
    gtt_id INTEGER,
    symbol TEXT,
    event_type TEXT NOT NULL,           -- placed / modified / deleted / status_change / rejected_placement
    status TEXT,                         -- broker-reported status when known
    details_json TEXT                    -- per-event payload: legs, prices, reason, etc.
);

CREATE INDEX IF NOT EXISTS idx_gtt_events_trade
ON gtt_events(trade_id, timestamp_utc DESC);

CREATE INDEX IF NOT EXISTS idx_gtt_events_gtt
ON gtt_events(gtt_id, timestamp_utc DESC);
