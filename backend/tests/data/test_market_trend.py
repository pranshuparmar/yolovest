"""Tests for the market-trend circuit-breaker signal and the swing mode.

`compute_market_trend` builds an equal-weight index from the universe's
daily closes and reports whether it's above its moving average — the
long-only bear-protection signal consumed by risk-check's
market_trend_filter gate.
"""

from datetime import datetime, timedelta, timezone

import pytest

from yolovest.config import _MODE_HOLDING_DAYS, _MODE_HOLDING_PERIODS
from yolovest.data.db import Database, _canonical_ohlcv_ts
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


class TestLiveSectorRegime:
    async def test_sector_breadth_and_returns(self, db):
        # 4 symbols in sector "BANK": 3 up, 1 down → breadth 0.75.
        await db.upsert_symbol_sectors([
            {"symbol": s, "industry": "BANK"} for s in ("A", "B", "C", "D")
        ])
        # 2 daily bars each: prev=100, latest = up or down.
        for s, latest in [("A", 102), ("B", 103), ("C", 101), ("D", 97)]:
            await db.upsert_ohlcv(s, "daily", _bars([100.0, float(latest)]), "test")
        stats, rets = await db.compute_live_sector_regime()
        assert "BANK" in stats
        assert abs(stats["BANK"]["breadth"] - 0.75) < 1e-9
        assert stats["BANK"]["n"] == 4
        assert rets["A"] > 0 and rets["D"] < 0

    async def test_thin_sector_excluded(self, db):
        # Only 2 peers (< 3) → sector gets no stats.
        await db.upsert_symbol_sectors([
            {"symbol": "X", "industry": "TINY"}, {"symbol": "Y", "industry": "TINY"},
        ])
        await db.upsert_ohlcv("X", "daily", _bars([100.0, 101.0]), "test")
        await db.upsert_ohlcv("Y", "daily", _bars([100.0, 102.0]), "test")
        stats, _ = await db.compute_live_sector_regime()
        assert "TINY" not in stats


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



class TestCanonicalTimestamp:
    _IST = timezone(timedelta(hours=5, minutes=30))

    def test_daily_strips_tz_and_time(self):
        assert _canonical_ohlcv_ts(datetime(2024, 3, 15, 0, 0, tzinfo=self._IST), "daily") == "2024-03-15T00:00:00"
        assert _canonical_ohlcv_ts(datetime(2024, 3, 15, 0, 0), "daily") == "2024-03-15T00:00:00"

    def test_intraday_keeps_minute_drops_tz(self):
        assert _canonical_ohlcv_ts(datetime(2024, 3, 15, 9, 20, tzinfo=self._IST), "5minute") == "2024-03-15T09:20:00"
        assert _canonical_ohlcv_ts(datetime(2024, 3, 15, 9, 20), "5minute") == "2024-03-15T09:20:00"

    async def test_upsert_dedupes_kite_tzaware_vs_yfinance_tznaive(self, db):
        # The exact 581k-duplicate bug: same day, kite tz-aware vs yfinance
        # tz-naive. After the canonicalization fix they must collapse to ONE
        # row (last writer wins via upsert), not two.
        aware = datetime(2024, 3, 15, 0, 0, tzinfo=self._IST)
        naive = datetime(2024, 3, 15, 0, 0)
        await db.upsert_ohlcv("ZZ", "daily", [OHLCVBar(
            timestamp=aware, open=255, high=256, low=249, close=249, volume=100)], "kite")
        await db.upsert_ohlcv("ZZ", "daily", [OHLCVBar(
            timestamp=naive, open=218, high=219, low=213, close=213, volume=100)], "yfinance")
        bars = await db.get_ohlcv("ZZ", "daily", days=3650)
        assert len(bars) == 1
