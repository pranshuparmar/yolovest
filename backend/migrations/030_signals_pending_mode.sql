-- Adds the mode column to signals and pending_trades.
-- Enables mode-scoped bulk delete and per-mode analytics without
-- joining through trades.

ALTER TABLE signals ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper';
ALTER TABLE pending_trades ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper';

CREATE INDEX IF NOT EXISTS idx_signals_mode ON signals(mode);
CREATE INDEX IF NOT EXISTS idx_pending_trades_mode ON pending_trades(mode);
