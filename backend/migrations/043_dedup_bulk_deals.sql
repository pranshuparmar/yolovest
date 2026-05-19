-- Collapse duplicate bulk_deals rows that accumulated under different
-- `deal_date` values for the same logical deal.
--
-- Root cause (now fixed in nse_official._normalize_deal +
-- upsert_bulk_deals): the consolidated NSE largedeal endpoint returns
-- deals from the past several sessions, but our normaliser stripped
-- the per-deal date and upsert defaulted everything to "today". Result:
-- the same deal got re-inserted under each subsequent day's date, so
-- "X bulk deals fetched (X new)" fired every heartbeat.
--
-- For each (symbol, client_name, buy_sell, quantity, trade_price)
-- group, keep only the row with the earliest deal_date (closest to
-- when the deal actually happened) and drop the rest.
--
-- Note: this is a one-shot. After this migration, the new
-- code paths preserve the real deal_date so the UNIQUE constraint
-- works across-day too.

DELETE FROM bulk_deals
WHERE id NOT IN (
    SELECT MIN(id)
    FROM bulk_deals
    GROUP BY symbol, client_name, buy_sell, quantity, trade_price
);
