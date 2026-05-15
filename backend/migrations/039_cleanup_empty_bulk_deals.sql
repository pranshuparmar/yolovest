-- One-shot cleanup for the symbol-only bulk_deals rows that NSE's
-- schema change left behind. Before today, the parser only knew the
-- camelCase keys (clientName / buySell / quantity / tradePrice) but
-- NSE switched to BD_*-prefixed keys for at least some endpoints.
-- The result was rows where every payload field beyond symbol +
-- deal_type was empty/null, with the unique constraint failing to
-- catch the dupes (SQLite treats NULL ≠ NULL on unique constraints).
--
-- The new upsert_bulk_deals + _normalize_deal stop the bleeding;
-- this drops the existing junk so the institutional-flows page
-- doesn't keep showing them.
DELETE FROM bulk_deals
WHERE (client_name IS NULL OR client_name = '')
  AND (buy_sell IS NULL OR buy_sell = '')
  AND quantity IS NULL
  AND trade_price IS NULL;
