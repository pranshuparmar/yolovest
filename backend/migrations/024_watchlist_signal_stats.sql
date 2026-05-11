-- Migration 024: Watchlist rotation stats
-- Tracks per-symbol streak of heartbeats without an actionable signal so
-- generate-signals can mark stale symbols for rotation, and market-scan can
-- temporarily exclude them (cooldown) to bring in fresh candidates.

CREATE TABLE IF NOT EXISTS watchlist_signal_stats (
    symbol TEXT PRIMARY KEY,
    no_signal_streak INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_watchlist_signal_stats_cooldown
    ON watchlist_signal_stats(cooldown_until);
