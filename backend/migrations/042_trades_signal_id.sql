-- DB-level idempotency: a single generated signal can map to at most
-- one trade. trade-execute now writes signal_id when persisting the
-- trade row; the UNIQUE index causes a second insert with the same
-- signal_id to fail at the DB level even if the in-memory dedup cache
-- (agent_memory) misses or is bypassed by a container restart between
-- the broker order placement and the trade-row insert.
--
-- Existing rows have NULL signal_id and are exempt (partial UNIQUE).
-- Going forward, generate_signals writes the signal id back onto the
-- signal dict so trade-execute can carry it through every code path
-- (paper, scaled live, single-leg live).

ALTER TABLE trades ADD COLUMN signal_id INTEGER REFERENCES signals(id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_signal_id
    ON trades(signal_id) WHERE signal_id IS NOT NULL;
