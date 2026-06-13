-- Stamp the producing model's version onto each trade at execution time.
-- Attribution previously required joining trades.signal_id -> signals,
-- which breaks once signals are cleaned up (bulk delete / retention) —
-- the trade row itself should carry which model put the money on. Read
-- paths COALESCE with the signals join so legacy rows still resolve
-- while their signal exists.
ALTER TABLE trades ADD COLUMN model_version TEXT;
