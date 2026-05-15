-- Adds the scored_at column to predictions.
-- Queries in db.get_feedback_data filter predictions on this column to
-- locate recently scored entries within a lookback window.

ALTER TABLE predictions ADD COLUMN scored_at TEXT;

-- Backfill scored_at = created_at for predictions already scored so the
-- feedback lookback window has data on first run after migration.
UPDATE predictions
SET scored_at = created_at
WHERE actual_price IS NOT NULL AND scored_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_predictions_scored_at
ON predictions(scored_at)
WHERE scored_at IS NOT NULL;
