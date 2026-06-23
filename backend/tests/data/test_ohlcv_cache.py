"""Short-TTL OHLCV cache used by the on-demand review / deep-dive fetches."""

from unittest.mock import AsyncMock

import pytest

from yolovest.data import ohlcv_cache
from yolovest.data.ohlcv_cache import get_ohlcv_cached


@pytest.fixture(autouse=True)
def _clear():
    ohlcv_cache.clear()
    yield
    ohlcv_cache.clear()


def _md(return_value):
    md = AsyncMock()
    md.get_ohlcv = AsyncMock(return_value=return_value)
    return md


async def test_caches_within_ttl():
    md = _md(["bar1", "bar2"])
    r1 = await get_ohlcv_cached(md, "TCS", 365)
    r2 = await get_ohlcv_cached(md, "TCS", 365)
    assert r1 == r2 == ["bar1", "bar2"]
    md.get_ohlcv.assert_awaited_once()  # 2nd served from cache


async def test_symbol_is_case_normalised():
    md = _md(["bar"])
    await get_ohlcv_cached(md, "tcs", 365)
    await get_ohlcv_cached(md, "TCS", 365)
    md.get_ohlcv.assert_awaited_once()


async def test_empty_result_is_not_cached():
    md = _md([])
    await get_ohlcv_cached(md, "BOGUS", 365)
    await get_ohlcv_cached(md, "BOGUS", 365)
    assert md.get_ohlcv.await_count == 2  # transient miss is retried, never cached


async def test_different_day_windows_cache_separately():
    md = _md(["x"])
    await get_ohlcv_cached(md, "TCS", 365)
    await get_ohlcv_cached(md, "TCS", 30)
    assert md.get_ohlcv.await_count == 2


async def test_refetches_after_expiry():
    md = _md(["bar"])
    await get_ohlcv_cached(md, "TCS", 365)
    # Force the entry's expiry into the past (monotonic clock is always > 0).
    ohlcv_cache._cache[("TCS", 365)] = (0.0, ["bar"])
    await get_ohlcv_cached(md, "TCS", 365)
    assert md.get_ohlcv.await_count == 2
