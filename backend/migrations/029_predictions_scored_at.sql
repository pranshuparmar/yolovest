-- Add scored_at column to predictions table.
-- The feedback loop (db.get_feedback_data) and model-retrain query this
-- column to find recently-scored predictions inside a lookback window.
-- Previously the column only existed on dry_run_results, so those
-- queries failed with "no such column: p.scored_at".

ALTER TABLE predictions ADD COLUMN scored_at TEXT;

-- Backfill scored_at = created_at for predictions that have already been
-- scored (actual_price IS NOT NULL). It's the best approximation we have
-- since the actual scoring time wasn't tracked previously.
UPDATE predictions
SET scored_at = created_at
WHERE actual_price IS NOT NULL AND scored_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_predictions_scored_at
ON predictions(scored_at)
WHERE scored_at IS NOT NULL;
