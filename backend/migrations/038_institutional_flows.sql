-- Persist NSE bulk/block deals + FII/DII daily activity.
-- Until now both were fetched, returned, and dropped on the floor.
-- bulk_deals: per-symbol institutional accumulation/distribution events.
-- fii_dii_daily: market-wide foreign + domestic institutional net flows.
-- delivery_pct: per-symbol per-day fraction of traded volume that took
--   physical delivery (vs intraday). High delivery = strong-hand
--   accumulation. Stored on ohlcv since it's a daily-bar attribute.

CREATE TABLE IF NOT EXISTS bulk_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    deal_type TEXT NOT NULL,
    client_name TEXT,
    buy_sell TEXT,
    quantity INTEGER,
    trade_price REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(deal_date, symbol, client_name, buy_sell, quantity, trade_price)
);
CREATE INDEX IF NOT EXISTS idx_bulk_deals_symbol_date
    ON bulk_deals(symbol, deal_date DESC);
CREATE INDEX IF NOT EXISTS idx_bulk_deals_date
    ON bulk_deals(deal_date DESC);

CREATE TABLE IF NOT EXISTS fii_dii_daily (
    date TEXT PRIMARY KEY,
    fii_buy REAL,
    fii_sell REAL,
    fii_net REAL,
    dii_buy REAL,
    dii_sell REAL,
    dii_net REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE ohlcv ADD COLUMN delivery_pct REAL;
