-- Migration 015: Re-run prediction symbol backfill
-- Ensures all predictions have symbol populated, even if migration 012
-- partially applied (column added but backfill didn't complete).

-- Backfill from linked signals
UPDATE predictions SET symbol = (
    SELECT s.symbol FROM signals s WHERE s.id = predictions.signal_id
) WHERE signal_id IS NOT NULL AND symbol IS NULL;

-- Backfill from trades if signal link is missing
UPDATE predictions SET symbol = (
    SELECT t.symbol FROM trades t WHERE t.trade_id = predictions.trade_id
) WHERE trade_id IS NOT NULL AND symbol IS NULL;
