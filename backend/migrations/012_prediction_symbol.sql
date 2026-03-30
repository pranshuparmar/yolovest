-- Add symbol column to predictions table.
-- Previously symbol was only available via LEFT JOIN to signals,
-- which returned NULL when signal_id was missing.

ALTER TABLE predictions ADD COLUMN symbol TEXT;

-- Backfill existing predictions from linked signals
UPDATE predictions SET symbol = (
    SELECT s.symbol FROM signals s WHERE s.id = predictions.signal_id
) WHERE signal_id IS NOT NULL AND symbol IS NULL;

-- Backfill from trades if signal link is missing
UPDATE predictions SET symbol = (
    SELECT t.symbol FROM trades t WHERE t.trade_id = predictions.trade_id
) WHERE trade_id IS NOT NULL AND symbol IS NULL;
