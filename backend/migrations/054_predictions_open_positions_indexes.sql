-- Hot-path indexes for per-heartbeat reads the audit flagged.
--
-- predictions: get_feedback_data runs every heartbeat (it joins/filters by
-- symbol) and the nightly retention delete filters on created_at. Neither a
-- symbol lookup nor a bare created_at range can seek the existing
-- (model_version, created_at) composite (its leading column is model_version),
-- so both degrade toward full scans as the table grows.
CREATE INDEX IF NOT EXISTS idx_predictions_symbol ON predictions(symbol);
CREATE INDEX IF NOT EXISTS idx_predictions_created_at ON predictions(created_at);

-- trades: get_open_positions filters
--   status IN ('open','partially_filled') AND mode = ? ORDER BY created_at DESC
-- many times per heartbeat, but the only matching index is on status alone. A
-- partial index over just the open rows, keyed by (mode, created_at DESC),
-- covers the filter + sort without indexing the (large, growing) closed rows.
CREATE INDEX IF NOT EXISTS idx_trades_open_positions
    ON trades(mode, created_at DESC)
    WHERE status IN ('open', 'partially_filled');
