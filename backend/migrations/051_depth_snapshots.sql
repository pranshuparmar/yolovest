-- Order-book depth snapshots, collected each heartbeat from the Kite
-- batch quote API for the active watchlist. This is the long-game
-- intraday dataset: bar-derived features can rank intraday outcomes
-- (AUC ~0.58) but cannot pay intraday costs at any label/geometry —
-- order-flow imbalance is the class of feature that can. Collection
-- only; nothing consumes these rows for trading until months of
-- history accumulate and an offline experiment proves an edge.
CREATE TABLE IF NOT EXISTS depth_snapshots (
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ltp REAL,
    bid REAL,
    ask REAL,
    total_buy_qty INTEGER,
    total_sell_qty INTEGER,
    top5_buy_qty INTEGER,
    top5_sell_qty INTEGER,
    volume INTEGER,
    PRIMARY KEY (ts, symbol)
);

CREATE INDEX IF NOT EXISTS idx_depth_snapshots_symbol_ts
    ON depth_snapshots(symbol, ts);
