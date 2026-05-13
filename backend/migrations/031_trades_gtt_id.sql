-- Add gtt_id to trades so an open trade can reference the broker-side
-- GTT (Good Till Triggered) order pair that enforces target + stoploss.
-- GTT applies to CNC trades only. MIS rows leave this column NULL and
-- continue to rely on client-side target/SL detection in position-monitor.

ALTER TABLE trades ADD COLUMN gtt_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_trades_gtt_id
ON trades(gtt_id)
WHERE gtt_id IS NOT NULL;
