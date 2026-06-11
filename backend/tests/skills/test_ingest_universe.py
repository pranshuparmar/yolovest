"""Tests for ingest-universe constituent resolution (live fetch + cache + fallback)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from yolovest.skills.ingest_universe import IngestUniverseSkill


@pytest.fixture
def skill(app_context):
    skill = IngestUniverseSkill(app_context)
    # upsert_symbol_sectors is called after a successful live fetch to
    # persist the Industry column. Default it to a benign success so
    # individual tests don't have to mock it unless they care.
    skill.ctx.db.upsert_symbol_sectors = AsyncMock(return_value=0)
    # _resolve_universe_symbols runs the raw list through the
    # quarantine resolver — default to identity so tests that don't
    # care about quarantine still get their list back unchanged.
    skill.ctx.db.resolve_symbols_with_replacements = AsyncMock(
        side_effect=lambda syms: list(syms),
    )
    return skill


def _as_details(syms: list[str]) -> list[dict[str, str]]:
    """Helper: lift a list of plain ticker strings to the
    {"symbol": "...", "industry": "..."} dict shape that
    fetch_live_constituent_details returns."""
    return [{"symbol": s, "industry": "Unknown"} for s in syms]


class TestUniverseResolution:
    """The resolver must try cache → live → bundled in that order."""

    async def test_uses_fresh_cache_when_available(self, skill):
        cached_payload = json.dumps({
            "symbols": ["RELIANCE", "TCS", "INFY"],
            "fetched_at": "2099-01-01T00:00:00+05:30",  # always fresh
        })
        skill.ctx.db.get_system_state = AsyncMock(return_value=cached_payload)

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
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
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["NEW1", "NEW2", "NEW3"])),
        ):
            symbols = await skill._resolve_universe_symbols("nifty500")

        assert symbols == ["NEW1", "NEW2", "NEW3"]
        # New result must be persisted
        skill.ctx.db.set_system_state.assert_called_once()

    async def test_live_fetch_when_cache_missing(self, skill):
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["A", "B", "C"])),
        ):
            symbols = await skill._resolve_universe_symbols("nifty500")

        assert symbols == ["A", "B", "C"]
        skill.ctx.db.set_system_state.assert_called_once()

    async def test_falls_back_to_bundled_when_live_fails(self, skill):
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
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
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["X", "Y"])),
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
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["RELIANCE", "ZOMATO", "TCS"])),
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
        skill.ctx.db.record_fetch_success = AsyncMock()
        skill.ctx.market_data.get_ohlcv = AsyncMock(return_value=[])

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["RELIANCE"])),
        ):
            result = await skill.execute()

        # The 'days' value passed to get_ohlcv must reflect the config, not 365
        call_args = skill.ctx.market_data.get_ohlcv.call_args_list[0]
        assert call_args.kwargs.get("days") == 1825 or call_args.args[2] == 1825


class TestFailureTracking:
    """Per-symbol fetch failures must increment the quarantine counter so
    replacement symbols that also fail get auto-quarantined."""

    async def test_records_failure_on_exception(self, skill):
        skill.ctx.config.market_data.backfill_days = 365
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()
        skill.ctx.db.resolve_symbols_with_replacements = AsyncMock(
            return_value=["BADSYMBOL"],
        )
        skill.ctx.db.record_fetch_failure = AsyncMock(return_value=False)
        skill.ctx.db.record_fetch_success = AsyncMock()
        skill.ctx.market_data.get_ohlcv = AsyncMock(
            side_effect=ValueError("delisted"),
        )

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["BADSYMBOL"])),
        ):
            await skill.execute()

        skill.ctx.db.record_fetch_failure.assert_awaited()
        call = skill.ctx.db.record_fetch_failure.call_args
        assert call.args[0] == "BADSYMBOL"
        assert "delisted" in call.args[1]

    async def test_reports_newly_quarantined(self, skill):
        """When record_fetch_failure returns True (3rd consecutive failure),
        the symbol should appear in results['newly_quarantined']."""
        skill.ctx.config.market_data.backfill_days = 365
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()
        skill.ctx.db.resolve_symbols_with_replacements = AsyncMock(
            return_value=["BADSYMBOL"],
        )
        skill.ctx.db.record_fetch_failure = AsyncMock(return_value=True)
        skill.ctx.db.record_fetch_success = AsyncMock()
        skill.ctx.market_data.get_ohlcv = AsyncMock(
            side_effect=ValueError("delisted"),
        )

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["BADSYMBOL"])),
        ):
            result = await skill.execute()

        assert "BADSYMBOL" in result.data["newly_quarantined"]

    async def test_records_success_clears_failures(self, skill):
        """A clean fetch must reset the counter via record_fetch_success."""
        from datetime import datetime

        from yolovest.models.schemas import OHLCVBar

        bars = [OHLCVBar(timestamp=datetime(2026, 5, 1), open=1, high=2, low=1, close=1.5, volume=100)]
        skill.ctx.config.market_data.backfill_days = 365
        skill.ctx.db.get_system_state = AsyncMock(return_value=None)
        skill.ctx.db.set_system_state = AsyncMock()
        skill.ctx.db.resolve_symbols_with_replacements = AsyncMock(return_value=["RELIANCE"])
        skill.ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        skill.ctx.db.record_fetch_success = AsyncMock()
        skill.ctx.market_data.get_ohlcv = AsyncMock(return_value=bars)

        with patch(
            "yolovest.skills.ingest_universe.fetch_live_constituent_details",
            new=AsyncMock(return_value=_as_details(["RELIANCE"])),
        ):
            await skill.execute()

        skill.ctx.db.record_fetch_success.assert_awaited_with("RELIANCE")
