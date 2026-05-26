"""Tests for `Database.get_intraday_training_dataset` — the 5-min decision +
1-min path loader for the intraday model."""

from datetime import datetime, timedelta

import pytest

from yolovest.data.db import Database
from yolovest.models.schemas import OHLCVBar


def _bars(start: datetime, n: int, step_min: int) -> list[OHLCVBar]:
    out = []
    for i in range(n):
        ts = start + timedelta(minutes=i * step_min)
        base = 100.0 + i * 0.1
        out.append(OHLCVBar(
            symbol="X", timestamp=ts, open=base, high=base + 0.5,
            low=base - 0.5, close=base + 0.1, volume=1000 + i,
        ))
    return out


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


class TestIntradayTrainingDataset:
    async def test_returns_decision_and_minute_bars(self, db):
        # Recent session so the (default-unbounded) window includes them.
        day = datetime(2026, 5, 22, 9, 15)
        await db.upsert_ohlcv("RELIANCE", "5minute", _bars(day, 6, 5), "kite")
        await db.upsert_ohlcv("RELIANCE", "1m", _bars(day, 30, 1), "kite")

        ds = await db.get_intraday_training_dataset()

        assert len(ds["decision_bars"]) == 6
        assert all(b["symbol"] == "RELIANCE" for b in ds["decision_bars"])
        assert len(ds["minute_bars"]["RELIANCE"]) == 30
        # decision bars ordered ascending by timestamp
        ts = [b["timestamp"] for b in ds["decision_bars"]]
        assert ts == sorted(ts)
        # minute bars ascending per symbol
        mts = [b["timestamp"] for b in ds["minute_bars"]["RELIANCE"]]
        assert mts == sorted(mts)

    async def test_intervals_are_separated(self, db):
        day = datetime(2026, 5, 22, 9, 15)
        await db.upsert_ohlcv("RELIANCE", "5minute", _bars(day, 4, 5), "kite")
        await db.upsert_ohlcv("RELIANCE", "1m", _bars(day, 10, 1), "kite")
        # daily bars must never leak into either intraday bucket
        await db.upsert_ohlcv("RELIANCE", "daily", _bars(day, 3, 1440), "kite")

        ds = await db.get_intraday_training_dataset()

        assert len(ds["decision_bars"]) == 4          # only 5-min
        assert len(ds["minute_bars"]["RELIANCE"]) == 10  # only 1-min

    async def test_symbols_filter_scopes_both_queries(self, db):
        day = datetime(2026, 5, 22, 9, 15)
        for sym in ("RELIANCE", "INFY"):
            await db.upsert_ohlcv(sym, "5minute", _bars(day, 3, 5), "kite")
            await db.upsert_ohlcv(sym, "1m", _bars(day, 6, 1), "kite")

        ds = await db.get_intraday_training_dataset(symbols=["RELIANCE"])

        assert {b["symbol"] for b in ds["decision_bars"]} == {"RELIANCE"}
        assert set(ds["minute_bars"].keys()) == {"RELIANCE"}

    async def test_max_days_window_excludes_old_bars(self, db):
        recent = datetime(2026, 5, 22, 9, 15)
        old = datetime(2020, 1, 2, 9, 15)
        await db.upsert_ohlcv("RELIANCE", "5minute", _bars(recent, 3, 5), "kite")
        await db.upsert_ohlcv("RELIANCE", "5minute", _bars(old, 3, 5), "kite")

        ds = await db.get_intraday_training_dataset(max_days=365)

        # Only the recent session survives a 1-year window.
        assert len(ds["decision_bars"]) == 3
        assert all(b["timestamp"] >= "2025" for b in ds["decision_bars"])

    async def test_empty_when_no_intraday_bars(self, db):
        ds = await db.get_intraday_training_dataset()
        assert ds["decision_bars"] == []
        assert ds["minute_bars"] == {}
