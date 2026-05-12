"""Tests for ingest-universe constituent resolution (live fetch + cache + fallback)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from yolovest.skills.ingest_universe import IngestUniverseSkill


@pytest.fixture
def skill(app_context):
    return IngestUniverseSkill(app_context)


class TestUniverseResolution:
    """The resolver must try cache → live → bundled in that order."""

    async def test_uses_fresh_cache_when_available(self, skill):
        cached_payload = json.dumps({
            "symbols": ["RELIANCE", "TCS", "INFY"],
            "fetched_at": "2099-01-01T00:00:00+05:30",  # always fresh
        })
        skill.ctx.db.get_system_state = AsyncMock(return_value=cached_payload)

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituents",
            new=AsyncMock(),
        ) as mock_live:
            symbols = await skill._resolve_universe_symbols("nifty500")

        assert symbols == ["RELIANCE", "TCS", "INFY"]
        # Live fetcher must NOT be called when cache is fresh
        mock_live.assert_not_called()

    async def test_expired_cache_triggers_live_fetch(self, skill):
        stale_payload = json.dumps({
            "symbols": ["OLD1", "OLD2"],
            "fetched_at": "2000-01-01T00:00:00+05:30",  # ancient
        })
        skill.ctx.db.get_system_state = AsyncMock(return_value=stale_payload)
        skill.ctx.db.set_system_state = AsyncMock()

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituents",
            new=AsyncMock(return_value=["NEW1", "NEW2", "NEW3"]),
        ):
            symbols = await skill._resolve_universe_symbols("nifty500")

        assert symbols == ["NEW1", "NEW2", "NEW3"]
        # New result must be persisted
        skill.ctx.db.set_system_state.assert_called_once()

    async def test_live_fetch_when_cache_missing(self, skill):
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituents",
            new=AsyncMock(return_value=["A", "B", "C"]),
        ):
            symbols = await skill._resolve_universe_symbols("nifty500")

        assert symbols == ["A", "B", "C"]
        skill.ctx.db.set_system_state.assert_called_once()

    async def test_falls_back_to_bundled_when_live_fails(self, skill):
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituents",
            new=AsyncMock(return_value=None),  # live fetch failed
        ):
            symbols = await skill._resolve_universe_symbols("nifty500")

        # Should be the bundled NIFTY_500_SUBSET
        from yolovest.data.nse_symbols import NIFTY_500_SUBSET
        assert symbols == NIFTY_500_SUBSET
        # Don't persist a fallback result — keep retrying live on next run
        skill.ctx.db.set_system_state.assert_not_called()

    async def test_corrupt_cache_treated_as_miss(self, skill):
        skill.ctx.db.get_system_state = AsyncMock(return_value="not valid json {{{")
        skill.ctx.db.set_system_state = AsyncMock()

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituents",
            new=AsyncMock(return_value=["X", "Y"]),
        ):
            symbols = await skill._resolve_universe_symbols("nifty500")

        assert symbols == ["X", "Y"]
