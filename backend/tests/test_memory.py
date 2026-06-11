"""Tests for AgentMemory — the cross-restart KV store.

Runs against a real SQLite database (tmp_path) so the JSON round-trip,
TTL expiry, and namespace isolation are exercised end to end, not
against mocks.
"""

import pytest

from yolovest.data.db import Database
from yolovest.memory import AgentMemory


@pytest.fixture
async def memory(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield AgentMemory(db)
    await db.close()


class TestRoundTrip:
    async def test_json_value_roundtrip(self, memory):
        await memory.set("session", "ctx", {"strategies": ["a", "b"], "n": 3})
        assert await memory.get("session", "ctx") == {"strategies": ["a", "b"], "n": 3}

    async def test_missing_key_returns_none(self, memory):
        assert await memory.get("session", "nope") is None

    async def test_overwrite(self, memory):
        await memory.set("session", "k", 1)
        await memory.set("session", "k", 2)
        assert await memory.get("session", "k") == 2

    async def test_delete(self, memory):
        await memory.set("session", "k", "v")
        await memory.delete("session", "k")
        assert await memory.get("session", "k") is None


class TestNamespaces:
    async def test_namespace_isolation(self, memory):
        await memory.set("session", "k", "session-value")
        await memory.set("reasoning", "k", "reasoning-value")
        assert await memory.get("session", "k") == "session-value"
        assert await memory.get("reasoning", "k") == "reasoning-value"

    async def test_list_keys_scoped(self, memory):
        await memory.set("session", "a", 1)
        await memory.set("session", "b", 2)
        await memory.set("reasoning", "c", 3)
        assert sorted(await memory.list_keys("session")) == ["a", "b"]

    async def test_get_all(self, memory):
        await memory.set("regime", "x", {"v": 1})
        await memory.set("regime", "y", {"v": 2})
        allv = await memory.get_all("regime")
        assert allv == {"x": {"v": 1}, "y": {"v": 2}}


class TestTTL:
    async def test_expired_entry_returns_none(self, memory):
        # Negative TTL -> expires_at in the past.
        await memory.set("session", "stale", "v", ttl_hours=-1)
        assert await memory.get("session", "stale") is None

    async def test_expired_entry_is_lazily_deleted(self, memory):
        await memory.set("session", "stale", "v", ttl_hours=-1)
        await memory.get("session", "stale")
        # After the lazy delete, the row is gone from the namespace.
        assert "stale" not in await memory.list_keys("session")

    async def test_unexpired_entry_survives(self, memory):
        await memory.set("session", "fresh", "v", ttl_hours=24)
        assert await memory.get("session", "fresh") == "v"

    async def test_get_all_skips_expired(self, memory):
        await memory.set("session", "fresh", 1, ttl_hours=24)
        await memory.set("session", "stale", 2, ttl_hours=-1)
        assert await memory.get_all("session") == {"fresh": 1}

    async def test_cleanup_expired(self, memory):
        await memory.set("session", "stale1", 1, ttl_hours=-1)
        await memory.set("session", "stale2", 2, ttl_hours=-2)
        await memory.set("session", "fresh", 3, ttl_hours=24)
        deleted = await memory.cleanup_expired()
        assert deleted == 2
        assert await memory.get("session", "fresh") == 3


class TestConvenienceWrappers:
    async def test_heartbeat_state(self, memory):
        await memory.save_heartbeat_state({"signals_processed": 4})
        assert (await memory.get_last_heartbeat_state())["signals_processed"] == 4

    async def test_market_regime(self, memory):
        await memory.save_market_regime({"breadth": 0.61})
        assert (await memory.get_market_regime())["breadth"] == 0.61


class TestResilience:
    async def test_get_swallows_db_errors(self):
        class _BrokenDB:
            async def get_memory(self, *a):
                raise RuntimeError("db down")

        mem = AgentMemory(_BrokenDB())
        assert await mem.get("session", "k") is None

    async def test_set_swallows_db_errors(self):
        class _BrokenDB:
            async def set_memory(self, *a):
                raise RuntimeError("db down")

        mem = AgentMemory(_BrokenDB())
        await mem.set("session", "k", "v")  # must not raise
