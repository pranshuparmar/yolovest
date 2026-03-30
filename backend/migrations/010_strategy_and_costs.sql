-- Migration 010: Add strategy and cost columns
-- Supports holding period / product tracking in dry runs and estimated costs in trades.

-- Dry-run results: track holding period, product, volatility score, and estimated costs
ALTER TABLE dry_run_results ADD COLUMN holding_period TEXT DEFAULT 'intraday';
ALTER TABLE dry_run_results ADD COLUMN product TEXT DEFAULT 'MIS';
ALTER TABLE dry_run_results ADD COLUMN volatility_score REAL;
ALTER TABLE dry_run_results ADD COLUMN estimated_costs REAL;

-- Trades: track estimated transaction costs
ALTER TABLE trades ADD COLUMN estimated_costs REAL;
