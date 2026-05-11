-- Add mode column to predictions so paper and live predictions
-- are tracked separately and don't pollute each other's analytics.
ALTER TABLE predictions ADD COLUMN mode TEXT DEFAULT 'paper';

-- Backfill: match existing predictions to their trade's mode where possible.
UPDATE predictions SET mode = (
    SELECT t.mode FROM trades t WHERE t.trade_id = predictions.trade_id
) WHERE trade_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM trades t WHERE t.trade_id = predictions.trade_id
);
