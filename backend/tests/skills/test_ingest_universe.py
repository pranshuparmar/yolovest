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


class TestQuarantineReplacementsInResolution:
    """User-configured replacements must be substituted in the resolved list."""

    async def test_replacement_is_substituted(self, skill):
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()
        # ZOMATO -> ETERNAL substitution wired via set_replacement_symbol
        skill.ctx.db.resolve_symbols_with_replacements = AsyncMock(
            return_value=["RELIANCE", "ETERNAL", "TCS"],
        )
        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituents",
            new=AsyncMock(return_value=["RELIANCE", "ZOMATO", "TCS"]),
        ):
            symbols = await skill._resolve_universe_symbols("nifty500")

        # The substituted list is what gets returned
        assert symbols == ["RELIANCE", "ETERNAL", "TCS"]
        # ... and resolve_symbols_with_replacements was called with the raw live list
        skill.ctx.db.resolve_symbols_with_replacements.assert_awaited_once_with(
            ["RELIANCE", "ZOMATO", "TCS"],
        )


class TestDaysDefaultsToConfig:
    """ingest-universe should default to config.market_data.backfill_days, not 365."""

    async def test_uses_config_backfill_days(self, skill):
        skill.ctx.config.market_data.backfill_days = 1825
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()
        skill.ctx.db.resolve_symbols_with_replacements = AsyncMock(return_value=["RELIANCE"])
        skill.ctx.db.upsert_ohlcv = AsyncMock(return_value=0)
        skill.ctx.market_data.get_ohlcv = AsyncMock(return_value=[])

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituents",
            new=AsyncMock(return_value=["RELIANCE"]),
        ):
            result = await skill.execute()

        # The 'days' value passed to get_ohlcv must reflect the config, not 365
        call_args = skill.ctx.market_data.get_ohlcv.call_args_list[0]
        assert call_args.kwargs.get("days") == 1825 or call_args.args[2] == 1825
