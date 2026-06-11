"""system_state + UI-editable config table.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import logging

logger = logging.getLogger(__name__)


class StateConfigMixin:
    # System State
    # ------------------------------------------------------------------

    async def is_kill_switch_active(self) -> bool:
        cursor = await self.read_conn.execute(
            "SELECT value FROM system_state WHERE key = 'kill_switch'"
        )
        row = await cursor.fetchone()
        return row is not None and row[0] == "active"

    async def set_system_state(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )
        await self.conn.commit()

    async def get_system_state(self, key: str) -> str | None:
        cursor = await self.read_conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Config (UI-editable settings)
    # ------------------------------------------------------------------

    async def get_all_config(self) -> dict[str, str]:
        """Return all config key-value pairs from the config table."""
        cursor = await self.read_conn.execute(
            "SELECT key, value FROM config ORDER BY key"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_config(self, key: str) -> str | None:
        """Get a single config value by key."""
        cursor = await self.read_conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_config(self, key: str, value: str) -> None:
        """Upsert a single config value."""
        await self.conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )
        await self.conn.commit()

    async def set_config_bulk(self, items: dict[str, str]) -> int:
        """Upsert multiple config values in a single transaction. Returns count."""
        if not items:
            return 0
        # First DML auto-begins a transaction (Python sqlite3 default
        # isolation_level). Explicit BEGIN would conflict with concurrent
        # writers sharing the same aiosqlite connection.
        try:
            for key, value in items.items():
                await self.conn.execute(
                    "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value),
                )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return len(items)

    async def is_config_empty(self) -> bool:
        """Check if the config table has any rows."""
        cursor = await self.read_conn.execute("SELECT COUNT(*) FROM config")
        row = await cursor.fetchone()
        return row[0] == 0

    # ------------------------------------------------------------------
