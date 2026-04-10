-- Fix sentiment table: add UNIQUE constraint on symbol for upsert to work.
-- The upsert uses ON CONFLICT(symbol) which requires UNIQUE, not just INDEX.
DROP INDEX IF EXISTS idx_sentiment_lookup;

-- Keep only the latest sentiment per symbol, remove duplicates
DELETE FROM sentiment WHERE rowid NOT IN (
    SELECT MAX(rowid) FROM sentiment GROUP BY symbol
);

CREATE UNIQUE INDEX idx_sentiment_symbol ON sentiment(symbol);
CREATE INDEX idx_sentiment_lookup ON sentiment(symbol, created_at DESC);
