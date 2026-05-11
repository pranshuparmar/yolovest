-- Add replacement_symbol column to quarantined_symbols.
-- When a symbol is quarantined but the user knows the correct/new symbol,
-- they can set a replacement. The pipeline will use the replacement symbol
-- instead of skipping the quarantined one entirely.
ALTER TABLE quarantined_symbols ADD COLUMN replacement_symbol TEXT;
