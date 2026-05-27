-- Record the historical "as of" date a dry-run was evaluated against
-- (NULL = latest market data). Surfaced in the dry-run history + detail
-- header so a past run shows which date its signals were generated for.
ALTER TABLE dry_run_results ADD COLUMN as_of TEXT;
