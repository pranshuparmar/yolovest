-- Shadow inference: track shadow model predictions alongside production
ALTER TABLE predictions ADD COLUMN is_shadow INTEGER NOT NULL DEFAULT 0;
ALTER TABLE predictions ADD COLUMN model_version TEXT;
CREATE INDEX IF NOT EXISTS idx_predictions_shadow ON predictions(is_shadow, created_at);
