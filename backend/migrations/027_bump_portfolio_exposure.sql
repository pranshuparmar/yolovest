-- Bump portfolio exposure limit from 60% to 90%.
-- With adopted CNC holdings, the entire portfolio is deployed in stocks —
-- 60% blocks all new signals since exposure = holdings / capital ≈ 100%.
-- 90% allows new trades while still leaving a safety margin.
UPDATE config
SET value = '0.90', updated_at = datetime('now')
WHERE key = 'risk.max_portfolio_exposure_pct'
  AND CAST(value AS REAL) <= 0.60;
