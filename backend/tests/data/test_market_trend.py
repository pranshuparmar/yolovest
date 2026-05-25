"""Tests for the market-trend circuit-breaker signal and the swing mode.

`compute_market_trend` builds an equal-weight index from the universe's
daily closes and reports whether it's above its moving average — the
long-only bear-protection signal consumed by risk-check's
market_trend_filter gate.
"""

from datetime import datetime, timedelta

import pytest

from yolovest.config import _MODE_HOLDING_DAYS, _MODE_HOLDING_PERIODS
from yolovest.data.db import Database
from yolovest.models.schemas import OHLCVBar


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


def _bars(closes: list[float]) -> list[OHLCVBar]:
    """Daily bars ending today, one per day, with given closes."""
    n = len(closes)
    today = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    out = []
    for i, c in enumerate(closes):
        out.append(OHLCVBar(
            timestamp=today - timedelta(days=(n - 1 - i)),
            open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1_000_000,
        ))
    return out


class TestComputeMarketTrend:
    async def test_uptrend_when_index_above_ma(self, db):
        # Two symbols both rising steadily → index well above its MA.
        rising = [100.0 + i for i in range(60)]
        await db.upsert_ohlcv("AAA", "daily", _bars(rising), "test")
        await db.upsert_ohlcv("BBB", "daily", _bars(rising), "test")
        t = await db.compute_market_trend(ma_window=20)
        assert t["sample_size"] > 0
        assert t["in_uptrend"] is True
        assert t["index_level"] >= t["ma"]

    async def test_downtrend_when_index_below_ma(self, db):
        # Rise for 40 days then fall sharply for 20 → latest index level
        # drops below its trailing MA.
        closes = [100.0 + i for i in range(40)] + [140.0 - 3 * i for i in range(20)]
        await db.upsert_ohlcv("AAA", "daily", _bars(closes), "test")
        await db.upsert_ohlcv("BBB", "daily", _bars(closes), "test")
        t = await db.compute_market_trend(ma_window=20)
        assert t["sample_size"] > 0
        assert t["in_uptrend"] is False
        assert t["index_level"] < t["ma"]

    async def test_neutral_when_no_data(self, db):
        # Fail-open: empty table → in_uptrend True, sample_size 0 (never
        # blocks trading on a cold cache).
        t = await db.compute_market_trend(ma_window=20)
        assert t["sample_size"] == 0
        assert t["in_uptrend"] is True


class TestSwingMode:
    def test_swing_mode_is_swing_only_no_intraday(self):
        # The swing mode must cover the full short+long day range and must
        # NOT include the intraday (MIS) bucket.
        assert _MODE_HOLDING_DAYS["swing"] == (2, 66)
        periods = _MODE_HOLDING_PERIODS["swing"]
        assert "intraday" not in periods
        assert set(periods) == {"short_term", "long_term"}


class TestGetOhlcvAsOf:
    async def test_end_bound_excludes_future_bars(self, db):
        # 30 daily bars ending today; an as-of cutoff 10 days ago must
        # return only bars up to that date (no look-ahead).
        await db.upsert_ohlcv("AAA", "daily", _bars([100.0 + i for i in range(30)]), "test")
        as_of = datetime.now().replace(hour=23, minute=59) - timedelta(days=10)
        sliced = await db.get_ohlcv("AAA", "daily", days=365, end=as_of)
        latest = await db.get_ohlcv("AAA", "daily", days=365)
        assert len(sliced) < len(latest)
        assert all(b.timestamp <= as_of for b in sliced)

    async def test_no_end_returns_latest(self, db):
        await db.upsert_ohlcv("BBB", "daily", _bars([50.0 + i for i in range(20)]), "test")
        bars = await db.get_ohlcv("BBB", "daily", days=365)
        assert len(bars) == 20

