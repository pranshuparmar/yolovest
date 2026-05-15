-- Daily F&O option-chain aggregates per underlying.
-- Only F&O-eligible NSE names (~200 of Nifty 500). Other stocks are
-- absent from this table — `compute_fno_features` returns is_fno_stock=0
-- for misses so the model can learn that absent ≠ neutral.
-- Cannot be backfilled: Kite doesn't expose historical option-chain
-- snapshots. ingest-fno starts collecting forward-only from day one.

CREATE TABLE IF NOT EXISTS fno_daily (
    date TEXT NOT NULL,            -- YYYY-MM-DD, NSE trading session
    symbol TEXT NOT NULL,          -- underlying tradingsymbol (e.g. RELIANCE)
    pcr_oi REAL,                   -- sum(PE OI) / sum(CE OI) across all strikes
    pcr_volume REAL,               -- sum(PE volume) / sum(CE volume)
    futures_oi REAL,               -- current-month futures contract OI
    futures_volume REAL,
    futures_close REAL,            -- futures last_price at ingest time
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_fno_daily_date
    ON fno_daily(date DESC);
