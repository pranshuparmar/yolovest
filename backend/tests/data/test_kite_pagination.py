"""Tests for KiteDataProvider date-range pagination."""

import importlib
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


@pytest.mark.skipif(
    not _has_module("kiteconnect"),
    reason="kiteconnect not installed",
)
class TestKitePagination:
    """Long historical windows must be split into per-call chunks."""

    async def test_daily_under_limit_does_one_call(self):
        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")
        prov._get_instrument_token = AsyncMock(return_value=12345)

        with patch.object(
            prov, "_fetch_historical", new=AsyncMock(return_value=[])
        ) as mock_fetch:
            await prov.get_ohlcv("RELIANCE", "daily", days=500)

        assert mock_fetch.call_count == 1

    async def test_5min_long_window_chunks_into_100_day_calls(self):
        """5-minute requests over 365 days should hit Kite ~4 times (100d each)."""
        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")
        prov._get_instrument_token = AsyncMock(return_value=12345)

        with patch.object(
            prov, "_fetch_historical", new=AsyncMock(return_value=[])
        ) as mock_fetch:
            await prov.get_ohlcv("RELIANCE", "5minute", days=365)

        # 365 days at max 100 per call -> ceil(365/100) = 4 calls
        assert mock_fetch.call_count == 4

    async def test_chunks_have_no_overlap_or_gap(self):
        """Adjacent chunks should be back-to-back: no overlapping or skipped days."""
        from datetime import date as _date

        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")
        prov._get_instrument_token = AsyncMock(return_value=12345)

        captured_ranges: list[tuple[_date, _date]] = []

        async def capture(_token, _interval, start, end):
            captured_ranges.append((start, end))
            return []

        with patch.object(prov, "_fetch_historical", new=AsyncMock(side_effect=capture)):
            await prov.get_ohlcv("RELIANCE", "5minute", days=250)

        # Verify continuity: each chunk's start = previous chunk's end + 1 day
        for prev, curr in zip(captured_ranges, captured_ranges[1:]):
            from datetime import timedelta
            assert curr[0] == prev[1] + timedelta(days=1), (
                f"Gap or overlap between {prev} and {curr}"
            )

    async def test_minute_interval_uses_60_day_chunks(self):
        """1-minute interval has the tightest Kite limit (60 days/call)."""
        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")
        prov._get_instrument_token = AsyncMock(return_value=12345)

        with patch.object(
            prov, "_fetch_historical", new=AsyncMock(return_value=[])
        ) as mock_fetch:
            await prov.get_ohlcv("RELIANCE", "1m", days=200)

        # 200 days at max 60 per call -> ceil(200/60) = 4 calls
        assert mock_fetch.call_count == 4


@pytest.mark.skipif(
    not _has_module("kiteconnect"),
    reason="kiteconnect not installed",
)
class TestKiteThrottling:
    """Sequential historical fetches must self-throttle to stay under
    Kite's 3 req/s historical limit, and on 429 must back off hard."""

    async def test_throttle_enforces_min_interval(self):
        """Two consecutive historical calls must be spaced by at least
        _historical_min_interval_sec."""
        import time as _time

        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")
        prov._historical_min_interval_sec = 0.1
        # First call: no wait needed
        t0 = _time.monotonic()
        await prov._throttle_historical()
        # Second call: must wait at least the interval
        await prov._throttle_historical()
        elapsed = _time.monotonic() - t0
        assert elapsed >= 0.1

    def test_rate_limit_error_detection(self):
        """_is_rate_limit_error must catch Kite's 'Too many requests' message
        regardless of error class."""
        from yolovest.data.kite_data import KiteDataProvider

        assert KiteDataProvider._is_rate_limit_error(
            Exception("Too many requests")
        )
        assert KiteDataProvider._is_rate_limit_error(
            ValueError("HTTP 429 received")
        )
        assert KiteDataProvider._is_rate_limit_error(
            RuntimeError("rate limit exceeded")
        )
        # Negatives — genuine errors shouldn't be misclassified
        assert not KiteDataProvider._is_rate_limit_error(
            Exception("Instrument token not found")
        )
        assert not KiteDataProvider._is_rate_limit_error(
            ValueError("Invalid date range")
        )


@pytest.mark.skipif(
    not _has_module("kiteconnect"),
    reason="kiteconnect not installed",
)
class TestInstrumentCachePrewarming:
    """Regression: _get_instrument_token must NOT call kite.instruments()
    once per symbol — that's an N+1 that exhausts the rate-limit budget."""

    async def test_prewarm_fetches_instruments_only_once(self):
        from unittest.mock import MagicMock

        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")
        fake_kite = MagicMock()
        fake_kite.instruments.return_value = [
            {"tradingsymbol": "RELIANCE", "instrument_token": 1},
            {"tradingsymbol": "TCS", "instrument_token": 2},
            {"tradingsymbol": "INFY", "instrument_token": 3},
        ]
        prov._kite = fake_kite

        # Three sequential lookups should result in exactly one
        # instruments() call, with all subsequent lookups served from cache.
        t1 = await prov._get_instrument_token("RELIANCE")
        t2 = await prov._get_instrument_token("TCS")
        t3 = await prov._get_instrument_token("INFY")

        assert t1 == 1
        assert t2 == 2
        assert t3 == 3
        assert fake_kite.instruments.call_count == 1

    async def test_cache_warmed_flag_reset_on_token_refresh(self):
        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")
        prov._token_cache_warmed = True
        prov._token_cache = {"RELIANCE": 1}

        prov.set_access_token("new-token")

        assert prov._token_cache_warmed is False
        assert prov._token_cache == {}


@pytest.mark.skipif(
    not _has_module("kiteconnect"),
    reason="kiteconnect not installed",
)
class TestKiteZeroBarFilter:
    """Kite returns 0.0-OHLC placeholder bars for pre-listing days; one such
    row must not fail the whole symbol's fetch."""

    async def test_fetch_skips_nonpositive_bars(self):
        from datetime import date as _date

        from yolovest.data.kite_data import KiteDataProvider

        prov = KiteDataProvider(api_key="x", access_token="y")

        class _FakeKite:
            def historical_data(self, *a, **k):
                return [
                    {"date": datetime(2020, 10, 1), "open": 0.0, "high": 0.0,
                     "low": 0.0, "close": 0.0, "volume": 0},          # pre-listing → skip
                    {"date": datetime(2020, 10, 5), "open": 100.0, "high": 105.0,
                     "low": 99.0, "close": 102.0, "volume": 1000},    # real → keep
                    {"date": datetime(2020, 10, 6), "open": 102.0, "high": 0.0,
                     "low": 100.0, "close": 101.0, "volume": 500},    # partial-zero → skip
                ]

        prov._get_kite = lambda: _FakeKite()
        bars = await prov._fetch_historical(
            12345, "day", _date(2020, 1, 1), _date(2020, 12, 31),
        )
        assert len(bars) == 1
        assert bars[0].close == 102.0
