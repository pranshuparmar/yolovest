-- Daily snapshot of broker funds/margins.
-- Populated by the `funds-snapshot` CRON skill (default schedule
-- 16:05 IST, after the market close and before report-generate).
-- One row per (snapshot_date, mode) — UNIQUE keeps the table from
-- accumulating duplicates when the skill is triggered manually.
--
-- The numbers come from broker.get_margins() (Kite's equity segment)
-- so they reflect what Zerodha saw at the time of the snapshot,
-- letting the user reconcile cash movements over time without
-- needing to log into Kite.
CREATE TABLE IF NOT EXISTS funds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,        -- YYYY-MM-DD IST
    captured_at TEXT NOT NULL,          -- ISO timestamp (UTC)
    mode TEXT NOT NULL,                 -- 'paper' / 'live'
    available_cash REAL NOT NULL DEFAULT 0,
    live_balance REAL NOT NULL DEFAULT 0,
    opening_balance REAL NOT NULL DEFAULT 0,
    utilised_margin REAL NOT NULL DEFAULT 0,
    m2m_unrealised REAL NOT NULL DEFAULT 0,
    m2m_realised REAL NOT NULL DEFAULT 0,
    payout REAL NOT NULL DEFAULT 0,
    collateral REAL NOT NULL DEFAULT 0,
    exposure REAL NOT NULL DEFAULT 0,
    span REAL NOT NULL DEFAULT 0,
    delivery REAL NOT NULL DEFAULT 0,
    net REAL NOT NULL DEFAULT 0,
    holdings_invested REAL NOT NULL DEFAULT 0,
    holdings_current REAL NOT NULL DEFAULT 0,
    raw_json TEXT,                      -- original kite payload for forensics
    UNIQUE(snapshot_date, mode)
);

CREATE INDEX IF NOT EXISTS idx_funds_snapshots_date
    ON funds_snapshots(snapshot_date DESC);
