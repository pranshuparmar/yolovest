-- Canonical sector / industry lookup keyed by NSE symbol. Populated by
-- ingest-universe from the niftyindices.com constituent CSVs (which carry
-- an Industry column we previously discarded). Everything that reads a
-- sector (risk-check sector cap, sector rotation analytics, watchlist
-- display, RiskExposureChart, LLM context) now resolves through this
-- table so the field is no longer perpetually NULL.

CREATE TABLE IF NOT EXISTS symbol_sectors (
    symbol TEXT PRIMARY KEY,
    sector TEXT,
    industry TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_symbol_sectors_sector
ON symbol_sectors(sector)
WHERE sector IS NOT NULL;
