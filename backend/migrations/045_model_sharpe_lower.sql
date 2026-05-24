-- Bootstrapped lower-bound Sharpe (p25) stored alongside the point
-- Sharpe. Deploy/promote decisions compare on this robust number so a
-- model whose headline Sharpe is propped up by one lucky holdout slice
-- can't out-rank (or block) a model with a genuinely consistent edge.
ALTER TABLE model_versions ADD COLUMN sharpe_lower REAL;
