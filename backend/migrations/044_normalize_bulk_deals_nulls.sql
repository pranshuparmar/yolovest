-- Eliminate NULL quantity / trade_price values that bypass the
-- UNIQUE(deal_date, symbol, client_name, buy_sell, quantity, trade_price)
-- constraint. SQLite treats NULL != NULL in UNIQUE indexes, so every
-- heartbeat that fetched a deal with a missing price (NSE sometimes
-- returns the field empty for off-market block deals or stale rows)
-- created a fresh duplicate row instead of being deduped. The user's
-- /institutional-flows page showed dozens of identical AGIIL bulk-deal
-- rows with PRICE="—" as the symptom.
--
-- Step 1: dedup FIRST using COALESCE so NULL groups can be collapsed.
-- (Doing the UPDATE first would itself trip the UNIQUE constraint when
-- the soon-to-be-equal rows still exist side-by-side.) Keep the
-- earliest-inserted row per logical-tuple where NULL is treated as 0.
DELETE FROM bulk_deals
WHERE id NOT IN (
    SELECT MIN(id)
    FROM bulk_deals
    GROUP BY
        symbol,
        client_name,
        buy_sell,
        COALESCE(quantity, 0),
        COALESCE(trade_price, 0.0)
);

-- Step 2: with no more dupes, normalise remaining NULLs to 0 / 0.0 so
-- the UNIQUE constraint correctly dedupes future inserts the upsert
-- now stores as numeric.
UPDATE bulk_deals SET quantity    = 0   WHERE quantity    IS NULL;
UPDATE bulk_deals SET trade_price = 0.0 WHERE trade_price IS NULL;
