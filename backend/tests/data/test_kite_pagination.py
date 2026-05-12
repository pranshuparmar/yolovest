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
