-- Fix trades inserted with status 'complete' or 'filled' instead of 'open'.
-- These are positions that were filled on the broker but never tracked as
-- open positions because get_open_positions() only queries for 'open'.
UPDATE trades SET status = 'open'
WHERE status IN ('complete', 'filled', 'COMPLETE', 'FILLED')
  AND closed_at IS NULL;
