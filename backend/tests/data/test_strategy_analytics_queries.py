"""Strategy / execution analytics: sector lookup and IST entry-hour buckets."""

import pytest

from yolovest.data.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def _closed_trade(db, trade_id, symbol, created_at, pnl=100.0):
    await db.conn.execute(
        "INSERT INTO trades (trade_id, symbol, signal_type, entry_price, fill_price, "
        "quantity, stop_loss_price, target_price, product, mode, status, slippage, "
        "pnl, exit_price, created_at, closed_at) "
        "VALUES (?, ?, 'BUY', 100, 100.1, 10, 95, 110, 'CNC', 'paper', 'closed', 0.1, "
        "?, 110, ?, ?)",
        (trade_id, symbol, pnl, created_at, created_at),
    )
    await db.conn.commit()


async def test_by_sector_uses_symbol_sectors(db):
    # Traded symbol is NOT on the scan watchlist — the sector must still
    # resolve from symbol_sectors (populated by ingest-universe).
    await db.conn.execute(
        "INSERT INTO symbol_sectors (symbol, sector, industry) VALUES ('TCS', 'IT', 'IT')",
    )
    await _closed_trade(db, "T-1", "TCS", "2026-09-14T03:50:00+00:00")
    await _closed_trade(db, "T-2", "NOSECTOR", "2026-09-14T03:50:00+00:00")

    perf = await db.get_strategy_performance(mode="paper")

    sectors = {r["sector"]: r["cnt"] for r in perf["by_sector"]}
    assert sectors == {"IT": 1, "Unknown": 1}


async def test_by_hour_is_ist(db):
    # 09:20 IST and 14:50 IST, stored as UTC.
    await _closed_trade(db, "T-1", "TCS", "2026-09-14T03:50:00+00:00")
    await _closed_trade(db, "T-2", "INFY", "2026-09-14T09:20:00.123456+00:00")

    perf = await db.get_strategy_performance(mode="paper")

    assert [r["hour"] for r in perf["by_hour"]] == [9, 14]
