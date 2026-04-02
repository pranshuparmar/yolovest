-- Migration 016: Add expected_holding_days to trades and dry_run_results
-- Supports dynamic holding period computation per stock.

-- Trades: store predicted number of trading days for this position
ALTER TABLE trades ADD COLUMN expected_holding_days INTEGER;

-- Dry-run results: store predicted holding days for scoring comparison
ALTER TABLE dry_run_results ADD COLUMN expected_holding_days INTEGER;
