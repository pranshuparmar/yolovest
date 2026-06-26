"""Tests for the market data ingester (fallback chain)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
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
    # Use IST-aware now, then strip timezone (providers return naive IST).
    # Daily bars only fall on weekdays — the ingester now drops weekend-dated
    # daily bars from non-kite providers, so the fixture must reflect that.
    end = datetime.now(IST).replace(tzinfo=None) - timedelta(days=age_days)
    days: list[datetime] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return [
        OHLCVBar(
            timestamp=days[i],
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
    # is_available is a synchronous flag check; keep it a sync Mock so the
    # ingester's availability filter doesn't leave an unawaited coroutine.
    provider.is_available = MagicMock(return_value=True)
    provider.source_name = "jugaad"
    return provider


class TestFallbackChain:
    async def test_primary_succeeds(self):
        bars = _make_bars()
        primary = _make_provider(bars=bars)
        fallback = _make_provider()

        ingester = MarketDataIngester([primary, fallback])
        # Fresh weekday bars must not be flagged stale on ANY day, including
        # weekends — now that _is_stale counts trading days, the last Friday
        # bar read on a Sat/Sun/Mon is fresh. This doubles as the weekend
        # regression guard (it failed before the trading-day fix).
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


class TestStalenessWeekendAware:
    """Daily staleness counts TRADING days, so the last Friday bar is fresh
    over the weekend and on Monday (the old calendar-day threshold flagged it
    stale every Mon and flaked on Sundays)."""

    def _ingester(self):
        return MarketDataIngester([_make_provider(bars=_make_bars())])

    def _bar(self, dt: datetime):
        return [OHLCVBar(timestamp=dt, open=100, high=101, low=99, close=100, volume=1000)]

    def test_friday_bar_fresh_on_monday(self, monkeypatch):
        import yolovest.data.ingester as ing
        monkeypatch.setattr(ing, "now_ist", lambda: datetime(2026, 6, 15, 9, 30, tzinfo=IST))  # Mon
        assert self._ingester()._is_stale(self._bar(datetime(2026, 6, 12, 15, 30)), "daily") is False  # Fri

    def test_friday_bar_fresh_on_sunday(self, monkeypatch):
        import yolovest.data.ingester as ing
        monkeypatch.setattr(ing, "now_ist", lambda: datetime(2026, 6, 14, 12, 0, tzinfo=IST))  # Sun
        assert self._ingester()._is_stale(self._bar(datetime(2026, 6, 12, 15, 30)), "daily") is False  # Fri

    def test_three_trading_sessions_old_is_stale(self, monkeypatch):
        import yolovest.data.ingester as ing
        monkeypatch.setattr(ing, "now_ist", lambda: datetime(2026, 6, 18, 9, 30, tzinfo=IST))  # Thu
        assert self._ingester()._is_stale(self._bar(datetime(2026, 6, 15, 15, 30)), "daily") is True  # Mon

    def test_same_day_is_fresh(self, monkeypatch):
        import yolovest.data.ingester as ing
        monkeypatch.setattr(ing, "now_ist", lambda: datetime(2026, 6, 16, 16, 0, tzinfo=IST))  # Tue
        assert self._ingester()._is_stale(self._bar(datetime(2026, 6, 16, 9, 30)), "daily") is False


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


class TestSubTickClamp:
    """Sub-tick open/close outside [low, high] (1-min feed rounding) is
    clamped into range and retained; gross violations still hard-drop."""

    def test_open_sub_tick_below_low_is_clamped(self):
        # The exact shape from the backfill logs: open 0.02 below low.
        bars = [OHLCVBar(timestamp=datetime(2024, 6, 25, 9, 15),
                         open=238.5, high=239.55, low=238.52,
                         close=239.55, volume=26552)]
        out = MarketDataIngester._validate_bars(bars, "1m", "kite", "X")
        assert len(out) == 1
        assert out[0].open == 238.52  # clamped up to low

    def test_close_sub_tick_above_high_is_clamped(self):
        bars = [OHLCVBar(timestamp=datetime(2024, 6, 25, 9, 16),
                         open=100.0, high=100.5, low=99.5,
                         close=100.52, volume=1000)]
        out = MarketDataIngester._validate_bars(bars, "1m", "kite", "X")
        assert len(out) == 1
        assert out[0].close == 100.5  # clamped down to high

    def test_gross_open_violation_still_dropped(self):
        # 200 vs a [238.52, 239.55] band — wrong scale, not a rounding blip.
        bars = [OHLCVBar(timestamp=datetime(2024, 6, 25, 9, 15),
                         open=200.0, high=239.55, low=238.52,
                         close=239.0, volume=1000)]
        out = MarketDataIngester._validate_bars(bars, "1m", "kite", "X")
        assert out == []

    def test_high_priced_bar_uses_relative_tolerance(self):
        # 5030 open vs 5032.1 low (the second log line): 2.1 < 0.1% of 5065.
        bars = [OHLCVBar(timestamp=datetime(2024, 6, 25, 9, 15),
                         open=5030.0, high=5065.2, low=5032.1,
                         close=5064.5, volume=6048)]
        out = MarketDataIngester._validate_bars(bars, "1m", "kite", "X")
        assert len(out) == 1
        assert out[0].open == 5032.1


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


class TestCorruptionGuards:
    """Inter-bar guards that drop the systematic free-provider corruption
    (weekend-dated bars from a UTC/IST off-by-one; wrong-symbol price
    spikes). kite (paid primary) is trusted and exempt from both."""

    @staticmethod
    def _bar(d: datetime, close: float) -> OHLCVBar:
        return OHLCVBar(
            timestamp=d, open=close, high=close * 1.01,
            low=close * 0.99, close=close, volume=1000,
        )

    def test_weekend_daily_dropped_for_non_kite(self):
        # 2026-05-23 is a Saturday, 05-24 Sunday, 05-22 Fri / 05-25 Mon.
        bars = [self._bar(datetime(2026, 5, 22), 100),
                self._bar(datetime(2026, 5, 23), 101),
                self._bar(datetime(2026, 5, 25), 102)]
        out = MarketDataIngester._validate_bars(bars, "daily", "jugaad", "X")
        dates = {b.timestamp.date().isoformat() for b in out}
        assert "2026-05-23" not in dates
        assert len(out) == 2

    def test_weekend_kept_for_kite(self):
        # Real special session (Muhurat / Budget Saturday) — kite is exempt.
        bars = [self._bar(datetime(2026, 5, 23), 100)]
        out = MarketDataIngester._validate_bars(bars, "daily", "kite", "X")
        assert len(out) == 1

    def test_price_outlier_dropped_for_non_kite(self):
        bars = [self._bar(datetime(2026, 5, 11), 100),
                self._bar(datetime(2026, 5, 12), 101),
                self._bar(datetime(2026, 5, 13), 102),
                self._bar(datetime(2026, 5, 14), 103),
                self._bar(datetime(2026, 5, 15), 104),
                self._bar(datetime(2026, 5, 18), 800)]  # ~7.8x median → wrong symbol
        out = MarketDataIngester._validate_bars(bars, "daily", "jugaad", "M&MFIN")
        assert 800 not in [b.close for b in out]
        assert len(out) == 5

    def test_price_outlier_kept_for_kite(self):
        bars = [self._bar(datetime(2026, 5, 11), 100),
                self._bar(datetime(2026, 5, 12), 101),
                self._bar(datetime(2026, 5, 13), 102),
                self._bar(datetime(2026, 5, 14), 103),
                self._bar(datetime(2026, 5, 15), 104),
                self._bar(datetime(2026, 5, 18), 800)]
        out = MarketDataIngester._validate_bars(bars, "daily", "kite", "X")
        assert any(b.close == 800 for b in out)

    def test_intraday_interval_not_weekend_filtered(self):
        # Intraday bars legitimately span any clock time; weekend guard is
        # daily-only.
        bars = [self._bar(datetime(2026, 5, 23, 10, 0), 100)]
        out = MarketDataIngester._validate_bars(bars, "5minute", "tvdatafeed", "X")
        assert len(out) == 1
