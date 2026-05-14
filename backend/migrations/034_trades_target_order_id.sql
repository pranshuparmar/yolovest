-- For MIS positions Zerodha doesn't allow GTT, so we DIY an OCO by
-- placing a LIMIT order at target alongside the entry's SL order.
-- Position-monitor cancels whichever side hasn't filled when the other
-- does; square-off cancels both before market-exit at EOD.

ALTER TABLE trades ADD COLUMN target_order_id TEXT;

CREATE INDEX IF NOT EXISTS idx_trades_target_order_id
ON trades(target_order_id)
WHERE target_order_id IS NOT NULL;
