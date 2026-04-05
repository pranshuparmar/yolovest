-- Migration 014: Locked holdings
-- Prevents YoloVest from selling shares the user has marked as locked.
-- Persists across broker token expirations and server restarts.

CREATE TABLE IF NOT EXISTS locked_holdings (
    symbol TEXT PRIMARY KEY,
    locked_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT
);
