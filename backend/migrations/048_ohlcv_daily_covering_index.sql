-- Covering partial index for daily OHLCV reads.
--
-- get_ohlcv(symbol, "daily", ...) seeks via idx_ohlcv_lookup but that index
-- lacks the OHLCV value columns, so every returned bar triggers a heap fetch
-- into the 6 GB table — scattered random reads that made market-scan's
-- per-universe enrichment take minutes. This partial index covers all the
-- columns get_ohlcv selects for the daily slice only (~1M rows), so the query
-- is answered entirely from a compact, cache-resident B-tree with no heap
-- fetches. get_ohlcv inlines `interval = 'daily'` as a literal so SQLite can
-- prove the partial-index predicate at compile time and actually use it.
--
-- Building this scans the table once; pre-build it manually off-hours
-- (CREATE INDEX IF NOT EXISTS ...) so this migration is a no-op at startup
-- instead of blocking boot while the index builds.

-- Supersedes the earlier close-only attempt, which was never usable
-- (parameterised interval defeated the partial-index predicate, and it
-- lacked open/high/low/volume so reads still hit the heap).
DROP INDEX IF EXISTS idx_ohlcv_daily;

CREATE INDEX IF NOT EXISTS idx_ohlcv_daily_covering
    ON ohlcv(symbol, timestamp, open, high, low, close, volume)
    WHERE interval = 'daily';
