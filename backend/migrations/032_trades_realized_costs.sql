-- Persist the actual transaction-cost breakdown that was applied when
-- a trade was closed. Stored as JSON: {brokerage, stt, other_charges,
-- total, source} where source is "broker" (from the contract-note API)
-- or "estimate" (from the config-based formula). Lets the trade detail
-- view show real charges when available instead of recomputing the
-- estimate every page load.

ALTER TABLE trades ADD COLUMN realized_costs_json TEXT;
