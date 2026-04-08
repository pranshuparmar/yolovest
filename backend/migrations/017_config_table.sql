-- Config table: stores UI-editable configuration as key-value pairs.
-- Keys use dot-notation (e.g. "risk.max_open_positions").
-- On first start, code populates defaults. Thereafter loaded from here.
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
