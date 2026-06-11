-- Unconditioned (pre-gate) per-symbol feature snapshots for feature-drift
-- monitoring. generate-signals upserts one row per (day, symbol, mode) —
-- later heartbeats the same day overwrite. drift-watch compares this live
-- feature distribution against the training distribution stamped into the
-- production model artifact (PSI per feature).
--
-- signals.features_snapshot cannot serve this purpose: it exists only for
-- PASSED signals, a gate-conditioned subset whose distribution is shifted
-- by construction — PSI against it would alarm spuriously.
CREATE TABLE IF NOT EXISTS feature_snapshots (
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'paper',
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (day, symbol, mode)
);

CREATE INDEX IF NOT EXISTS idx_feature_snapshots_day
    ON feature_snapshots(day);
