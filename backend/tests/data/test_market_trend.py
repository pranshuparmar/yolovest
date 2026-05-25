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



class TestDryRunModelOverride:
    """The dry-run model-override + as-of-date plumbing in the DB layer."""

    async def test_get_model_version_by_version(self, db):
        await db.save_model_version(
            "swing", "swing_v20260525_191626", "/m/swing.pkl",
            {"sharpe": 4.76, "argmax_sharpe": 2.07},
        )
        row = await db.get_model_version("swing_v20260525_191626")
        assert row is not None
        assert row["model_type"] == "swing"
        assert row["status"] == "shadow"
        assert await db.get_model_version("does_not_exist") is None

    async def test_dry_run_persists_as_of_and_surfaces_in_history(self, db):
        sig = {
            "symbol": "AAA", "signal_type": "BUY", "entry_price": 100.0,
            "target_price": 110.0, "stop_loss_price": 95.0,
            "confidence_score": 0.7, "model_version": "swing_v_shadow",
            "strategy_mode": "swing",
        }
        await db.insert_dry_run_results("run0001", [sig], as_of="2022-06-15")
        hist = await db.get_dry_run_history(limit=5)
        run = next(r for r in hist if r["run_id"] == "run0001")
        assert run["as_of"] == "2022-06-15"
        assert run["model_version"] == "swing_v_shadow"
        # And the per-signal rows carry the as_of stamp.
        rows = await db.get_dry_run_signals("run0001")
        assert rows[0]["as_of"] == "2022-06-15"

    async def test_dry_run_as_of_null_for_latest(self, db):
        sig = {
            "symbol": "BBB", "signal_type": "BUY", "entry_price": 50.0,
            "target_price": 55.0, "stop_loss_price": 48.0,
            "confidence_score": 0.6, "model_version": "swing_v_prod",
            "strategy_mode": "swing",
        }
        await db.insert_dry_run_results("run0002", [sig])  # no as_of → latest
        run = next(r for r in await db.get_dry_run_history(limit=5) if r["run_id"] == "run0002")
        assert run["as_of"] is None
        assert run["model_version"] == "swing_v_prod"


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
        # row — and the source-priority WHERE makes KITE win regardless of
        # ingestion order (yfinance can't clobber kite).
        aware = datetime(2024, 3, 15, 0, 0, tzinfo=self._IST)
        naive = datetime(2024, 3, 15, 0, 0)
        # kite first, then yfinance tries to overwrite → kite must stay.
        await db.upsert_ohlcv("ZZ", "daily", [OHLCVBar(
            timestamp=aware, open=255, high=256, low=249, close=249.4, volume=100)], "kite")
        await db.upsert_ohlcv("ZZ", "daily", [OHLCVBar(
            timestamp=naive, open=218, high=219, low=213, close=213.3, volume=100)], "yfinance")
        bars = await db.get_ohlcv("ZZ", "daily", days=3650)
        assert len(bars) == 1
        assert bars[0].close == 249.4  # kite kept, yfinance discarded

    async def test_kite_overwrites_existing_lower_source(self, db):
        # yfinance first, then kite → kite wins (higher priority).
        naive = datetime(2024, 3, 15, 0, 0)
        aware = datetime(2024, 3, 15, 0, 0, tzinfo=self._IST)
        await db.upsert_ohlcv("YY", "daily", [OHLCVBar(
            timestamp=naive, open=218, high=219, low=213, close=213.3, volume=100)], "yfinance")
        await db.upsert_ohlcv("YY", "daily", [OHLCVBar(
            timestamp=aware, open=255, high=256, low=249, close=249.4, volume=100)], "kite")
        bars = await db.get_ohlcv("YY", "daily", days=3650)
        assert len(bars) == 1
        assert bars[0].close == 249.4  # kite won despite arriving second

    async def test_same_source_refreshes(self, db):
        # Re-ingesting the same source updates the bar (ties allowed).
        d = datetime(2024, 3, 15, 0, 0)
        await db.upsert_ohlcv("WW", "daily", [OHLCVBar(
            timestamp=d, open=100, high=101, low=99, close=100, volume=10)], "kite")
        await db.upsert_ohlcv("WW", "daily", [OHLCVBar(
            timestamp=d, open=100, high=105, low=99, close=104, volume=20)], "kite")
        bars = await db.get_ohlcv("WW", "daily", days=3650)
        assert len(bars) == 1
        assert bars[0].close == 104  # refreshed


class TestIsValidOhlc:
    def test_accepts_positive(self):
        from yolovest.models.schemas import is_valid_ohlc
        assert is_valid_ohlc(100, 105, 99, 102) is True

    def test_rejects_zero_partial_zero_nan_negative_none(self):
        from yolovest.models.schemas import is_valid_ohlc
        assert is_valid_ohlc(0.0, 0.0, 0.0, 0.0) is False
        assert is_valid_ohlc(102, 0.0, 100, 101) is False     # one zero
        assert is_valid_ohlc(100, 105, 99, float("nan")) is False
        assert is_valid_ohlc(100, 105, -1, 102) is False
        assert is_valid_ohlc(None, 105, 99, 102) is False
