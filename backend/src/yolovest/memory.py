"""Agent memory persistence for cross-restart state.

Provides a key-value store backed by SQLite for persisting:
- Agent reasoning context (why trades were taken/avoided)
- Trading session state (last heartbeat results, active strategies)
- LLM conversation history for context continuity
- Market regime classifications

Memory is namespaced to avoid key collisions between subsystems.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from yolovest.timezone import now_utc

logger = logging.getLogger(__name__)


class AgentMemory:
    """Persistent key-value memory backed by the database.

    Namespaces separate different types of memory:
    - 'session': Current trading session state
    - 'reasoning': Trade reasoning history for LLM context
    - 'market_regime': Classified market conditions
    - 'heartbeat': Last heartbeat results for continuity
    - 'llm_context': LLM conversation snippets for context
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def get(self, namespace: str, key: str) -> Any | None:
        """Retrieve a value from memory. Returns None if not found or expired."""
        try:
            row = await self._db.get_memory(namespace, key)
            if row is None:
                return None

            # Check expiry
            if row.get("expires_at"):
                expires = datetime.fromisoformat(row["expires_at"])
                if now_utc() > expires:
                    await self.delete(namespace, key)
                    return None

            value = row.get("value", "")
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
        except Exception as e:
            logger.warning("Memory get failed (%s/%s): %s", namespace, key, e)
            return None

    async def set(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_hours: float | None = None,
    ) -> None:
        """Store a value in memory, optionally with a TTL."""
        try:
            serialized = json.dumps(value, default=str)
            expires_at = None
            if ttl_hours is not None:
                expires_at = (
                    now_utc() + timedelta(hours=ttl_hours)
                ).isoformat()

            await self._db.set_memory(namespace, key, serialized, expires_at)
        except Exception as e:
            logger.warning("Memory set failed (%s/%s): %s", namespace, key, e)

    async def delete(self, namespace: str, key: str) -> None:
        """Remove a key from memory."""
        try:
            await self._db.delete_memory(namespace, key)
        except Exception as e:
            logger.warning("Memory delete failed (%s/%s): %s", namespace, key, e)

    async def list_keys(self, namespace: str) -> list[str]:
        """List all keys in a namespace."""
        try:
            return await self._db.list_memory_keys(namespace)
        except Exception as e:
            logger.warning("Memory list_keys failed (%s): %s", namespace, e)
            return []

    async def get_all(self, namespace: str) -> dict[str, Any]:
        """Get all key-value pairs in a namespace."""
        try:
            rows = await self._db.get_all_memory(namespace)
            result = {}
            for row in rows:
                key = row["key"]
                # Check expiry
                if row.get("expires_at"):
                    expires = datetime.fromisoformat(row["expires_at"])
                    if now_utc() > expires:
                        continue
                try:
                    result[key] = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    result[key] = row["value"]
            return result
        except Exception as e:
            logger.warning("Memory get_all failed (%s): %s", namespace, e)
            return {}

    async def cleanup_expired(self) -> int:
        """Remove all expired memory entries. Returns count deleted."""
        try:
            return await self._db.cleanup_expired_memory()
        except Exception as e:
            logger.warning("Memory cleanup failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Convenience methods for common patterns
    # ------------------------------------------------------------------

    async def save_heartbeat_state(self, state: dict[str, Any]) -> None:
        """Persist heartbeat results for cross-restart continuity."""
        await self.set("heartbeat", "last_result", state, ttl_hours=24)

    async def get_last_heartbeat_state(self) -> dict[str, Any] | None:
        """Retrieve last heartbeat state."""
        return await self.get("heartbeat", "last_result")

    async def save_trade_reasoning(
        self, trade_id: str, reasoning: dict[str, Any]
    ) -> None:
        """Persist trade reasoning for LLM context."""
        await self.set("reasoning", trade_id, reasoning, ttl_hours=168)  # 7 days

    async def save_market_regime(self, regime: dict[str, Any]) -> None:
        """Persist current market regime classification."""
        await self.set("market_regime", "current", regime, ttl_hours=12)

    async def get_market_regime(self) -> dict[str, Any] | None:
        """Retrieve current market regime."""
        return await self.get("market_regime", "current")

    async def save_session_context(self, context: dict[str, Any]) -> None:
        """Persist session-level context (strategies, config overrides)."""
        await self.set("session", "context", context)

    async def get_session_context(self) -> dict[str, Any] | None:
        """Retrieve session context."""
        return await self.get("session", "context")
