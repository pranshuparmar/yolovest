-- Per-signal feature attribution.
-- Stores the top-N feature contributions (SHAP-style) that pushed the
-- ML model's confidence over the threshold for this signal. Surfaced
-- on the trade detail page so the user can see WHY the model picked
-- each setup — the replacement debug aid for the LLM-review layer
-- that's no longer in the path for live autonomous trading.
ALTER TABLE signals ADD COLUMN attribution_json TEXT;
