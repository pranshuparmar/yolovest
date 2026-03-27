-- Agent memory persistence
-- Stores agent state, reasoning context, and cross-restart memory

CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,           -- e.g., 'orchestrator', 'llm_context', 'trade_reasoning'
    key TEXT NOT NULL,                  -- lookup key within namespace
    value TEXT NOT NULL,                -- JSON-serialized value
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,                    -- optional TTL
    UNIQUE(namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_namespace ON agent_memory(namespace);
CREATE INDEX IF NOT EXISTS idx_agent_memory_expires ON agent_memory(expires_at) WHERE expires_at IS NOT NULL;
