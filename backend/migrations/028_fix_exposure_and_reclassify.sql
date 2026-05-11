-- 1. Revert exposure limit back to 60% — the code now calculates exposure
--    as system trades / available capital (excluding adopted holdings),
--    so 60% is the correct threshold again.
UPDATE config
SET value = '0.60', updated_at = datetime('now')
WHERE key = 'risk.max_portfolio_exposure_pct'
  AND CAST(value AS REAL) > 0.60;

-- 2. Re-classify adopted positions that were missed by migration 022.
UPDATE trades
SET origin = 'adopted'
WHERE origin = 'system'
  AND order_id LIKE 'ADOPTED-%';
