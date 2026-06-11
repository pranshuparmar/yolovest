"""Tests for quarantine triggering on provider-level errors."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from yolovest.skills.ingest_data import IngestDataSkill


@pytest.fixture
def ingest_skill(app_context):
    skill = IngestDataSkill(app_context)
    # Default: market hours off, no intraday
    app_context.market_hours.is_market_hours = MagicMock(return_value=False)
    # Disable expensive fetches (news, scrapers) to isolate OHLCV tests
    app_context.config.market_data.news_enabled = False
    app_context.config.market_data.scrapers_enabled = False
    # Disable market-regime so the index symbol (NIFTY 50) isn't
    # appended to the ingest list — these tests assert on the exact
    # symbol passed to record_fetch_failure and the appended index
    # would otherwise be the last call.
    app_context.config.strategy.market_regime.enabled = False
    return skill


class TestQuarantineOnProviderErrors:
    async def test_quarantine_when_all_providers_empty(self, ingest_skill):
        """All providers return empty data → should count as failure."""
        # Market data raises (simulating all providers exhausted)
        ingest_skill.ctx.market_data.get_ohlcv = AsyncMock(
            side_effect=ValueError("No providers returned data for DELISTED/daily"),
        )
        ingest_skill.ctx.db.get_combined_watchlist = AsyncMock(
            return_value=[{"symbol": "DELISTED"}],
        )
        ingest_skill.ctx.db.get_all_quarantined_symbol_set = AsyncMock(return_value=set())
        ingest_skill.ctx.db.record_fetch_failure = AsyncMock(return_value=False)

        result = await ingest_skill.execute()

        # Should call record_fetch_failure (not record_fetch_success)
        ingest_skill.ctx.db.record_fetch_failure.assert_awaited()
        call_args = ingest_skill.ctx.db.record_fetch_failure.call_args
        assert call_args[0][0] == "DELISTED"

    async def test_quarantine_on_provider_errors_with_stale_data(self, ingest_skill):
        """Provider errors + stale-ish data → should count as failure."""
        from datetime import datetime, timedelta

        from yolovest.models.schemas import OHLCVBar

        # Return data that's 2 days old (within 5-day threshold)
        old_bar = OHLCVBar(
            timestamp=datetime.now() - timedelta(days=2),
            open=100, high=105, low=95, close=102, volume=10000,
        )
        ingest_skill.ctx.market_data.get_ohlcv = AsyncMock(return_value=[old_bar])

        # Simulate provider metadata: one provider errored, one returned empty
        mock_meta = {
            "provider_errors": 1,
            "providers_empty": 1,
            "all_providers_tried": True,
        }
        ingest_skill.ctx.market_data.get_fetch_meta = MagicMock(return_value=mock_meta)

        ingest_skill.ctx.db.get_combined_watchlist = AsyncMock(
            return_value=[{"symbol": "GMRINFRA"}],
        )
        ingest_skill.ctx.db.get_all_quarantined_symbol_set = AsyncMock(return_value=set())
        ingest_skill.ctx.db.record_fetch_failure = AsyncMock(return_value=False)

        result = await ingest_skill.execute()

        # Should call record_fetch_failure due to provider issues
        ingest_skill.ctx.db.record_fetch_failure.assert_awaited()
        call_args = ingest_skill.ctx.db.record_fetch_failure.call_args
        assert call_args[0][0] == "GMRINFRA"
        assert "provider issues" in call_args[0][1].lower()
        # Should NOT call record_fetch_success
        ingest_skill.ctx.db.record_fetch_success.assert_not_awaited()

    async def test_no_quarantine_when_all_providers_healthy(self, ingest_skill):
        """Fresh data, no provider errors → should call record_fetch_success."""
        from datetime import datetime

        from yolovest.models.schemas import OHLCVBar

        fresh_bar = OHLCVBar(
            timestamp=datetime.now(),
            open=100, high=105, low=95, close=102, volume=10000,
        )
        ingest_skill.ctx.market_data.get_ohlcv = AsyncMock(return_value=[fresh_bar])

        # No provider issues
        mock_meta = {
            "provider_errors": 0,
            "providers_empty": 0,
            "all_providers_tried": False,
        }
        ingest_skill.ctx.market_data.get_fetch_meta = MagicMock(return_value=mock_meta)

        ingest_skill.ctx.db.get_combined_watchlist = AsyncMock(
            return_value=[{"symbol": "RELIANCE"}],
        )
        ingest_skill.ctx.db.get_all_quarantined_symbol_set = AsyncMock(return_value=set())

        result = await ingest_skill.execute()

        ingest_skill.ctx.db.record_fetch_success.assert_awaited()
        ingest_skill.ctx.db.record_fetch_failure.assert_not_awaited()

    async def test_quarantine_after_3_consecutive_failures(self, ingest_skill):
        """Symbol quarantined after record_fetch_failure returns True."""
        ingest_skill.ctx.market_data.get_ohlcv = AsyncMock(return_value=[])
        ingest_skill.ctx.db.get_combined_watchlist = AsyncMock(
            return_value=[{"symbol": "BADSTOCK"}],
        )
        ingest_skill.ctx.db.get_all_quarantined_symbol_set = AsyncMock(return_value=set())
        # Simulate: this is the 3rd failure, quarantine triggered
        ingest_skill.ctx.db.record_fetch_failure = AsyncMock(return_value=True)

        result = await ingest_skill.execute()

        assert result.data["quarantined"] >= 1

    async def test_quarantined_symbols_skipped(self, ingest_skill):
        """Symbols in quarantine set should be skipped entirely."""
        ingest_skill.ctx.db.get_combined_watchlist = AsyncMock(
            return_value=[{"symbol": "RELIANCE"}, {"symbol": "GMRINFRA"}],
        )
        ingest_skill.ctx.db.get_all_quarantined_symbol_set = AsyncMock(
            return_value={"GMRINFRA"},
        )

        from datetime import datetime

        from yolovest.models.schemas import OHLCVBar
        fresh_bar = OHLCVBar(
            timestamp=datetime.now(),
            open=100, high=105, low=95, close=102, volume=10000,
        )
        ingest_skill.ctx.market_data.get_ohlcv = AsyncMock(return_value=[fresh_bar])
        mock_meta = {"provider_errors": 0, "providers_empty": 0, "all_providers_tried": False}
        ingest_skill.ctx.market_data.get_fetch_meta = MagicMock(return_value=mock_meta)

        result = await ingest_skill.execute()

        # Only RELIANCE should be ingested
        assert result.data["symbols_ingested"] == 1
        assert result.data["quarantined"] == 1
