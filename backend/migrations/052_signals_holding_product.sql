-- Persist the holding-period decision (product + horizon) onto each
-- signal at generation time. Previously these lived only on the
-- transient signal dict and the dry_run_results table, so the
-- "Today's Recommendations" view couldn't show whether a signal was
-- MIS (intraday) or CNC (delivery), nor compute its target date.
-- product:               "MIS" / "CNC" (square-off vs delivery)
-- holding_period:        label, e.g. intraday / short_term / week / long
-- expected_holding_days: predicted trading-day horizon (0 for intraday),
--                        used to derive the signal's target date.
ALTER TABLE signals ADD COLUMN product TEXT;
ALTER TABLE signals ADD COLUMN holding_period TEXT;
ALTER TABLE signals ADD COLUMN expected_holding_days INTEGER;
