"""depth-snapshot: best-effort order-book archiving for the watchlist.

Collection only — the skill must gate correctly (market hours, config,
Kite availability), persist what the batch quote returns, self-prune,
and NEVER fail the pipeline (every error path returns success with a
reason)."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.depth_snapshot import DepthSnapshotSkill


def _quote(ltp: float = 100.0) -> dict:
    return {
        "ltp": ltp, "bid": ltp - 0.05, "ask": ltp + 0.05,
        "total_buy_qty": 5000, "total_sell_qty": 4000,
        "top5_buy_qty": 900, "top5_sell_qty": 700, "volume": 123456,
    }


@pytest.fixture
def skill(app_context):
    ctx = app_context
    ctx.config.market_data.kite_data_enabled = True
    ctx.config.market_data.depth_snapshots_enabled = True
    ctx.db.get_combined_watchlist = AsyncMock(
        return_value=[{"symbol": "RELIANCE"}, {"symbol": "TCS"}],
    )
    ctx.db.insert_depth_snapshots = AsyncMock(return_value=2)
    ctx.db.prune_depth_snapshots = AsyncMock(return_value=0)
    ctx.market_data.get_quotes_batch = AsyncMock(
        return_value={"RELIANCE": _quote(), "TCS": _quote(3500.0)},
    )
    return DepthSnapshotSkill(ctx)


class TestGates:
    def test_runs_only_with_kite_and_toggle(self, skill):
        skill.ctx.market_hours.is_market_hours = lambda: True
        assert skill.should_run() is True
        skill.ctx.config.market_data.depth_snapshots_enabled = False
        assert skill.should_run() is False
        skill.ctx.config.market_data.depth_snapshots_enabled = True
        skill.ctx.config.market_data.kite_data_enabled = False
        assert skill.should_run() is False

    def test_never_runs_off_hours(self, skill):
        skill.ctx.market_hours.is_market_hours = lambda: False
        assert skill.should_run() is False


class TestCollection:
    async def test_archives_batch_and_prunes(self, skill):
        result = await skill.execute()
        assert result.success
        assert result.data["snapshots"] == 2
        ts, rows = skill.ctx.db.insert_depth_snapshots.await_args.args
        assert set(rows) == {"RELIANCE", "TCS"}
        assert rows["RELIANCE"]["total_buy_qty"] == 5000
        skill.ctx.db.prune_depth_snapshots.assert_awaited_with(400)

    async def test_open_positions_are_included(self, skill):
        skill.ctx.db.get_open_positions = AsyncMock(
            return_value=[{"symbol": "INFY"}],
        )
        await skill.execute()
        called_symbols = skill.ctx.market_data.get_quotes_batch.await_args.args[0]
        assert "INFY" in called_symbols

    async def test_quote_failure_is_best_effort(self, skill):
        skill.ctx.market_data.get_quotes_batch = AsyncMock(
            side_effect=RuntimeError("kite down"),
        )
        result = await skill.execute()
        assert result.success
        assert result.data["reason"] == "quote_failed"
        skill.ctx.db.insert_depth_snapshots.assert_not_awaited()

    async def test_no_batch_capability_is_noop(self, skill):
        del skill.ctx.market_data.get_quotes_batch
        result = await skill.execute()
        assert result.success
        assert result.data["reason"] == "no_batch_quotes"


class TestDbRoundtrip:
    @pytest.fixture
    async def db(self, tmp_path):
        from yolovest.data.db import Database

        database = Database(str(tmp_path / "test.db"))
        await database.initialize()
        yield database
        await database.close()

    async def test_insert_get_back_and_prune(self, db):
        n = await db.insert_depth_snapshots(
            "2026-06-12T10:30:00+05:30",
            {"RELIANCE": _quote(), "TCS": _quote(3500.0)},
        )
        assert n == 2
        rows = await db.read_conn.execute_fetchall(
            "SELECT symbol, ltp, total_buy_qty FROM depth_snapshots ORDER BY symbol",
        )
        assert [r[0] for r in rows] == ["RELIANCE", "TCS"]
        assert rows[0][2] == 5000
        # Idempotent on the same instant.
        await db.insert_depth_snapshots(
            "2026-06-12T10:30:00+05:30", {"RELIANCE": _quote(101.0)},
        )
        count = (await db.read_conn.execute_fetchall(
            "SELECT COUNT(*) FROM depth_snapshots",
        ))[0][0]
        assert count == 2
        # Ancient rows prune.
        await db.insert_depth_snapshots(
            "2020-01-01T10:30:00+05:30", {"OLD": _quote()},
        )
        assert await db.prune_depth_snapshots(keep_days=400) == 1
