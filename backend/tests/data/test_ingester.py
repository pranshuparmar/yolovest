"""Tests for the market data ingester (fallback chain)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from yolovest.data.ingester import MarketDataIngester
from yolovest.models.schemas import OHLCVBar

IST = ZoneInfo("Asia/Kolkata")


def _make_bars(n: int = 3, age_days: int = 0) -> list[OHLCVBar]:
    """Create test OHLCV bars. age_days=0 means today's data.

    Uses naive timestamps in IST-equivalent time so staleness checks work
    correctly with the IST-aware ingester.
    """
    # Use IST-aware now, then strip timezone (providers return naive IST)
    now_ist = datetime.now(IST).replace(tzinfo=None)
    base = now_ist - timedelta(days=age_days + n)
    return [
        OHLCVBar(
            timestamp=base + timedelta(days=i),
            open=100.0 + i,
            high=105.0 + i,
            low=95.0 + i,
            close=102.0 + i,
            volume=1000 * (i + 1),
        )
        for i in range(n)
    ]


def _make_provider(bars=None, quote=None, healthy=True, fail=False):
    """Create a mock provider."""
    provider = AsyncMock()
    if fail:
        provider.get_ohlcv = AsyncMock(side_effect=ConnectionError("provider down"))
        provider.get_quote = AsyncMock(side_effect=ConnectionError("provider down"))
        provider.health_check = AsyncMock(return_value=False)
    else:
        provider.get_ohlcv = AsyncMock(return_value=bars or [])
        provider.get_quote = AsyncMock(return_value=quote or {"ltp": 100.0})
        provider.health_check = AsyncMock(return_value=healthy)
    return provider


class TestFallbackChain:
    async def test_primary_succeeds(self):
        bars = _make_bars()
        primary = _make_provider(bars=bars)
        fallback = _make_provider()

        ingester = MarketDataIngester([primary, fallback])
        result = await ingester.get_ohlcv("RELIANCE", "daily", 30)

        assert len(result) == 3
        primary.get_ohlcv.assert_called_once()
        fallback.get_ohlcv.assert_not_called()

    async def test_primary_fails_uses_fallback(self):
        bars = _make_bars()
        primary = _make_provider(fail=True)
        fallback = _make_provider(bars=bars)

        ingester = MarketDataIngester([primary, fallback])
        result = await ingester.get_ohlcv("RELIANCE", "daily", 30)

        assert len(result) == 3
        primary.get_ohlcv.assert_called_once()
        fallback.get_ohlcv.assert_called_once()

    async def test_all_providers_fail_raises(self):
        primary = _make_provider(fail=True)
        fallback = _make_provider(fail=True)

        ingester = MarketDataIngester([primary, fallback])
        with pytest.raises(ConnectionError):
            await ingester.get_ohlcv("RELIANCE", "daily", 30)

    async def test_requires_at_least_one_provider(self):
        with pytest.raises(ValueError, match="At least one"):
            MarketDataIngester([])


class TestSourceProvenance:
    """The ingester records WHICH provider produced a symbol's bars so
    callers can stamp the real source (kite/jugaad/...) into ohlcv.source."""

    async def test_winning_provider_recorded(self):
        primary = _make_provider(bars=_make_bars())
        primary.source_name = "kite"
        fallback = _make_provider()
        fallback.source_name = "jugaad"

        ingester = MarketDataIngester([primary, fallback])
        await ingester.get_ohlcv("RELIANCE", "daily", 30)

        assert ingester.get_fetch_meta("RELIANCE")["source"] == "kite"

    async def test_fallback_provider_recorded(self):
        primary = _make_provider(fail=True)
        primary.source_name = "kite"
        fallback = _make_provider(bars=_make_bars())
        fallback.source_name = "jugaad"

        ingester = MarketDataIngester([primary, fallback])
        await ingester.get_ohlcv("RELIANCE", "daily", 30)

        assert ingester.get_fetch_meta("RELIANCE")["source"] == "jugaad"

    async def test_source_name_derived_from_class(self):
        from yolovest.data.kite_data import KiteDataProvider
        from yolovest.data.yfinance_provider import YFinanceProvider
        # property is class-level; read on an unconfigured instance is fine
        assert KiteDataProvider.__new__(KiteDataProvider).source_name == "kite"
        assert YFinanceProvider.__new__(YFinanceProvider).source_name == "yfinance"


class TestStalenessValidation:
    async def test_fresh_data_accepted(self):
        bars = _make_bars(age_days=0)
        provider = _make_provider(bars=bars)
        ingester = MarketDataIngester([provider])
        result = await ingester.get_ohlcv("RELIANCE", "daily", 30)
        assert len(result) == 3

    async def test_stale_daily_data_triggers_fallback(self):
        stale_bars = _make_bars(age_days=5)  # 5 days old
        fresh_bars = _make_bars(age_days=0)
        primary = _make_provider(bars=stale_bars)
        fallback = _make_provider(bars=fresh_bars)

        ingester = MarketDataIngester([primary, fallback])
        result = await ingester.get_ohlcv("RELIANCE", "daily", 30)

        assert len(result) == 3
        # Both should have been called since primary was stale
        primary.get_ohlcv.assert_called_once()
        fallback.get_ohlcv.assert_called_once()


class TestDataQualityValidation:
    async def test_invalid_bar_high_lt_low_dropped(self):
        bars = [
            OHLCVBar(
                timestamp=datetime.now(),
                open=100.0, high=90.0, low=95.0,  # high < low
                close=92.0, volume=1000,
            )
        ]
        provider = _make_provider(bars=bars)
        ingester = MarketDataIngester([provider])
        # Invalid bar dropped, empty result → raises
        with pytest.raises(ValueError, match="No providers"):
            await ingester.get_ohlcv("BAD", "daily", 30)

    async def test_valid_bars_pass_through(self):
        bars = _make_bars()
        provider = _make_provider(bars=bars)
        ingester = MarketDataIngester([provider])
        result = await ingester.get_ohlcv("RELIANCE", "daily", 30)
        assert len(result) == 3


class TestIntradayRouting:
    async def test_intraday_uses_intraday_provider(self):
        # Intraday bars must be very recent (within stale_threshold_minutes)
        # Use IST-equivalent naive timestamps
        now = datetime.now(IST).replace(tzinfo=None)
        bars = [
            OHLCVBar(
                timestamp=now - timedelta(minutes=10 - i),
                open=100.0 + i, high=105.0 + i, low=95.0 + i,
                close=102.0 + i, volume=1000,
            )
            for i in range(3)
        ]
        daily = _make_provider()
        intraday = _make_provider(bars=bars)

        ingester = MarketDataIngester([daily], intraday_provider=intraday)
        result = await ingester.get_ohlcv("RELIANCE", "5minute", 1)

        assert len(result) == 3
        daily.get_ohlcv.assert_not_called()
        intraday.get_ohlcv.assert_called_once()

    async def test_intraday_without_provider_raises(self):
        daily = _make_provider()
        ingester = MarketDataIngester([daily])
        with pytest.raises(ValueError, match="No intraday provider"):
            await ingester.get_ohlcv("RELIANCE", "5minute", 1)


class TestQuoteFallback:
    async def test_quote_primary_succeeds(self):
        primary = _make_provider(quote={"ltp": 2500.0})
        ingester = MarketDataIngester([primary])
        result = await ingester.get_quote("RELIANCE")
        assert result["ltp"] == 2500.0

    async def test_quote_falls_back(self):
        primary = _make_provider(fail=True)
        fallback = _make_provider(quote={"ltp": 2500.0})
        ingester = MarketDataIngester([primary, fallback])
        result = await ingester.get_quote("RELIANCE")
        assert result["ltp"] == 2500.0

    async def test_quote_prefers_intraday(self):
        daily = _make_provider(quote={"ltp": 2500.0})
        intraday = _make_provider(quote={"ltp": 2501.0})
        ingester = MarketDataIngester([daily], intraday_provider=intraday)
        result = await ingester.get_quote("RELIANCE")
        assert result["ltp"] == 2501.0  # intraday preferred


class TestHealthCheck:
    async def test_healthy_if_any_provider_up(self):
        provider1 = _make_provider(healthy=False)
        provider2 = _make_provider(healthy=True)
        ingester = MarketDataIngester([provider1, provider2])
        assert await ingester.health_check() is True

    async def test_unhealthy_if_all_down(self):
        provider1 = _make_provider(healthy=False)
        provider2 = _make_provider(healthy=False)
        ingester = MarketDataIngester([provider1, provider2])
        assert await ingester.health_check() is False


class TestIntegrationFallbackToDB:
    """Integration test: primary fails → fallback → data can be persisted and read."""

    async def test_fallback_data_round_trips_through_db(self, tmp_path):
        from yolovest.data.db import Database

        # Setup DB
        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        # Primary fails, fallback returns data
        fresh_bars = _make_bars(age_days=0)
        primary = _make_provider(fail=True)
        fallback = _make_provider(bars=fresh_bars)
        ingester = MarketDataIngester([primary, fallback])

        # Fetch through ingester
        bars = await ingester.get_ohlcv("RELIANCE", "daily", 30)
        assert len(bars) == 3

        # Persist to DB
        count = await db.upsert_ohlcv("RELIANCE", "daily", bars, "yfinance")
        assert count == 3

        # Read back
        stored = await db.get_ohlcv("RELIANCE", "daily", 30)
        assert len(stored) == 3
        assert stored[0].open == bars[0].open

        await db.close()
