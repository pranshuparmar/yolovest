-- Bump stale risk.max_open_positions values up to the new default of 10.
-- The code default changed from 3 to 10, but DB-stored values from earlier
-- runs override the code default and were silently blocking signal flow.
-- Only updates rows where the existing value is below the new default —
-- users who intentionally set 10+ are not touched.
UPDATE config
SET value = '10', updated_at = datetime('now')
WHERE key = 'risk.max_open_positions'
  AND CAST(value AS INTEGER) < 10;
