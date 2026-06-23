-- Migration 053: Hot-path indexes for per-heartbeat queries.
--
-- signals(mode, created_at): backs get_todays_signaled_symbols (runs every
--   heartbeat: WHERE created_at >= ? AND mode = ? GROUP BY symbol) and the
--   recommendations read (WHERE created_at >= ? ORDER BY created_at DESC).
--   The existing idx_signals_mode is single-column low-cardinality, so the
--   created_at filter degraded toward a scan as signals grew (signals is the
--   highest-insert-rate table — one row per evaluated symbol per heartbeat).
--
-- predictions(model_version, created_at): backs get_model_drift_stats
--   (WHERE model_version = ? AND created_at >= ?).
--
-- Both are small, additive covering-ish indexes; IF NOT EXISTS keeps re-runs
-- safe.
CREATE INDEX IF NOT EXISTS idx_signals_mode_created ON signals(mode, created_at);
CREATE INDEX IF NOT EXISTS idx_predictions_modelver_created ON predictions(model_version, created_at);
