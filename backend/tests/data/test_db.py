"""Tests for the database layer and migration system."""

from datetime import datetime

import pytest

from yolovest.data.db import Database
from yolovest.models.schemas import OHLCVBar


@pytest.fixture
async def db(tmp_path):
    """Create a temporary database with migrations applied."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


class TestMigrationSystem:
    async def test_schema_version_created(self, db):
        version = await db.get_schema_version()
        assert version >= 1

    async def test_migration_is_idempotent(self, db):
        """Running initialize() twice should not fail or re-apply migrations."""
        version_before = await db.get_schema_version()
        await db._run_migrations()
        version_after = await db.get_schema_version()
        assert version_after == version_before

    async def test_tables_created(self, db):
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in await cursor.fetchall()}
        expected = {
            "schema_version", "ohlcv", "watchlist", "trades", "signals",
            "predictions", "sentiment", "premarket", "system_state",
            "llm_reviews", "audit_log",
        }
        assert expected.issubset(tables)

    async def test_wal_mode_enabled(self, db):
        cursor = await db.conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0] == "wal"

    async def test_incremental_migration(self, tmp_path):
        """Test that a new migration file gets applied on second init."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        # Write first migration
        (migrations_dir / "001_initial.sql").write_text(
            "CREATE TABLE test_one (id INTEGER PRIMARY KEY);"
        )

        db_path = str(tmp_path / "test.db")
        database = Database(db_path, migrations_dir=migrations_dir)
        await database.initialize()

        assert await database.get_schema_version() == 1

        # Add a second migration
        (migrations_dir / "002_add_table.sql").write_text(
            "CREATE TABLE test_two (id INTEGER PRIMARY KEY);"
        )

        # Re-run migrations
        await database._run_migrations()
        assert await database.get_schema_version() == 2

        # Verify both tables exist
        cursor = await database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'test_%'"
        )
        tables = {row[0] for row in await cursor.fetchall()}
        assert tables == {"test_one", "test_two"}

        await database.close()


class TestMigrationAtomicity:
    async def test_failed_migration_rolls_back(self, tmp_path):
        """Test that a partially failing migration is rolled back entirely."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        # First migration succeeds
        (migrations_dir / "001_initial.sql").write_text(
            "CREATE TABLE test_one (id INTEGER PRIMARY KEY)"
        )

        db_path = str(tmp_path / "test.db")
        database = Database(db_path, migrations_dir=migrations_dir)
        await database.initialize()

        assert await database.get_schema_version() == 1

        # Second migration: first statement valid, second invalid
        (migrations_dir / "002_bad.sql").write_text(
            "CREATE TABLE test_two (id INTEGER PRIMARY KEY);\n"
            "INVALID SQL THAT WILL FAIL"
        )

        with pytest.raises(Exception):  # noqa: B017
            await database._run_migrations()

        # Version should still be 1 (rolled back)
        assert await database.get_schema_version() == 1

        # test_two should NOT exist (rolled back)
        cursor = await database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='test_two'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 0

        await database.close()

    async def test_missing_migrations_dir_skips(self, tmp_path):
        """Test that a missing migrations dir logs warning and returns."""
        db_path = str(tmp_path / "test.db")
        nonexistent = tmp_path / "no_such_dir"
        database = Database(db_path, migrations_dir=nonexistent)
        await database.initialize()
        # Should initialize without error, version 0 (no migrations applied)
        assert await database.get_schema_version() == 0
        await database.close()


class TestHealthCheck:
    async def test_health_check_returns_true(self, db):
        assert await db.health_check() is True

    async def test_health_check_after_close(self, tmp_path):
        database = Database(str(tmp_path / "test.db"))
        await database.initialize()
        await database.close()
        # conn property raises RuntimeError after close
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = database.conn


class TestSystemState:
    async def test_set_and_get(self, db):
        await db.set_system_state("test_key", "test_value")
        assert await db.get_system_state("test_key") == "test_value"

    async def test_get_missing_key(self, db):
        assert await db.get_system_state("nonexistent") is None

    async def test_upsert_overwrites(self, db):
        await db.set_system_state("key", "v1")
        await db.set_system_state("key", "v2")
        assert await db.get_system_state("key") == "v2"

    async def test_kill_switch_inactive_by_default(self, db):
        assert await db.is_kill_switch_active() is False

    async def test_kill_switch_active(self, db):
        await db.set_system_state("kill_switch", "active")
        assert await db.is_kill_switch_active() is True

    async def test_kill_switch_inactive(self, db):
        await db.set_system_state("kill_switch", "inactive")
        assert await db.is_kill_switch_active() is False


class TestOHLCV:
    def _make_bars(self, n: int = 3) -> list[OHLCVBar]:
        return [
            OHLCVBar(
                timestamp=datetime(2026, 3, 20 + i, 10, 0),
                open=100.0 + i,
                high=105.0 + i,
                low=95.0 + i,
                close=102.0 + i,
                volume=1000 * (i + 1),
            )
            for i in range(n)
        ]

    async def test_upsert_and_get(self, db):
        bars = self._make_bars()
        count = await db.upsert_ohlcv("RELIANCE", "daily", bars, "jugaad")
        assert count == 3

        result = await db.get_ohlcv("RELIANCE", "daily", days=30)
        assert len(result) == 3
        assert result[0].open == 100.0
        assert result[2].close == 104.0

    async def test_upsert_empty_list(self, db):
        count = await db.upsert_ohlcv("TCS", "daily", [], "jugaad")
        assert count == 0

    async def test_upsert_idempotent(self, db):
        bars = self._make_bars(1)
        await db.upsert_ohlcv("INFY", "daily", bars, "jugaad")
        # Upsert same bar with different source — should update, not duplicate
        await db.upsert_ohlcv("INFY", "daily", bars, "yfinance")

        result = await db.get_ohlcv("INFY", "daily", days=30)
        assert len(result) == 1

    async def test_different_intervals_stored_separately(self, db):
        bars = self._make_bars(1)
        await db.upsert_ohlcv("RELIANCE", "daily", bars, "jugaad")
        await db.upsert_ohlcv("RELIANCE", "5minute", bars, "tvdatafeed")

        daily = await db.get_ohlcv("RELIANCE", "daily", days=30)
        intraday = await db.get_ohlcv("RELIANCE", "5minute", days=30)
        assert len(daily) == 1
        assert len(intraday) == 1

    async def test_get_ohlcv_returns_ascending(self, db):
        bars = self._make_bars(5)
        await db.upsert_ohlcv("TCS", "daily", bars, "jugaad")
        result = await db.get_ohlcv("TCS", "daily", days=30)
        timestamps = [r.timestamp for r in result]
        assert timestamps == sorted(timestamps)


class TestWatchlist:
    async def test_upsert_and_get(self, db):
        stocks = [
            {"symbol": "RELIANCE", "composite_score": 0.9, "sector": "Energy"},
            {"symbol": "TCS", "composite_score": 0.8, "sector": "IT"},
        ]
        await db.upsert_watchlist(stocks)
        result = await db.get_watchlist()
        assert len(result) == 2
        assert result[0]["symbol"] == "RELIANCE"  # higher score first

    async def test_upsert_replaces(self, db):
        await db.upsert_watchlist([{"symbol": "RELIANCE", "composite_score": 0.9}])
        await db.upsert_watchlist([{"symbol": "TCS", "composite_score": 0.7}])
        result = await db.get_watchlist()
        assert len(result) == 1
        assert result[0]["symbol"] == "TCS"

    async def test_shadow_prediction_stores_mode(self, db):
        """Regression: insert_shadow_prediction must store the trading mode.
        Previously omitted the mode column, so all shadow predictions
        defaulted to 'paper' in the DB even when generated in live mode —
        polluting paper analytics and confusing bulk delete by mode."""
        pred_id = await db.insert_shadow_prediction({
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "predicted_target": 100.0,
            "predicted_stop_loss": 90.0,
            "expected_holding_period": "intraday",
            "model_version": "swing_v1",
            "mode": "live",
        })
        row = await db.read_conn.execute(
            "SELECT mode, is_shadow FROM predictions WHERE prediction_id = ?",
            (pred_id,),
        )
        result = await row.fetchone()
        assert result is not None
        assert result[0] == "live"
        assert result[1] == 1  # is_shadow

    async def test_shadow_prediction_defaults_to_paper(self, db):
        """If caller doesn't pass mode (legacy code path), default to paper."""
        pred_id = await db.insert_shadow_prediction({
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "predicted_target": 100.0,
            "predicted_stop_loss": 90.0,
            "expected_holding_period": "intraday",
            "model_version": "swing_v1",
        })
        row = await db.read_conn.execute(
            "SELECT mode FROM predictions WHERE prediction_id = ?",
            (pred_id,),
        )
        result = await row.fetchone()
        assert result[0] == "paper"

    async def test_score_prediction_sets_scored_at(self, db):
        """Regression: feedback queries filter on predictions.scored_at;
        score_prediction must populate it."""
        pred_id = await db.insert_prediction({
            "symbol": "RELIANCE",
            "trade_id": None,
            "predicted_direction": "BUY",
            "predicted_target": 100.0,
            "predicted_stop_loss": 90.0,
            "expected_holding_period": "intraday",
            "model_version": "swing_v1",
            "mode": "live",
        })
        await db.score_prediction(
            prediction_id=pred_id,
            actual_price=105.0,
            direction_correct=True,
            target_hit=True,
            actual_pnl_pct=5.0,
        )
        row = await db.read_conn.execute(
            "SELECT scored_at FROM predictions WHERE prediction_id = ?",
            (pred_id,),
        )
        result = await row.fetchone()
        assert result[0] is not None  # populated with datetime('now')

    async def test_get_feedback_data_does_not_raise(self, db):
        """Regression: 'no such column: p.scored_at' from feedback query."""
        # Empty DB — should still execute the query without error
        data = await db.get_feedback_data(lookback_days=14)
        assert isinstance(data, dict)


class TestBulkDelete:
    """Regression: bulk_delete([paper|live]) previously wiped ALL rows
    from signals / pending_trades — there was no mode column on those
    tables, so the code just deleted everything. Now those tables are
    skipped in paper/live groups and have their own dedicated groups."""

    async def _insert_prediction(self, db, mode: str) -> str:
        return await db.insert_prediction({
            "symbol": "RELIANCE",
            "trade_id": None,
            "predicted_direction": "BUY",
            "predicted_target": 100.0,
            "predicted_stop_loss": 90.0,
            "expected_holding_period": "intraday",
            "model_version": "swing_v1",
            "mode": mode,
        })

    async def test_paper_delete_preserves_live_predictions(self, db):
        live_pred = await self._insert_prediction(db, "live")
        paper_pred = await self._insert_prediction(db, "paper")

        result = await db.bulk_delete("paper")

        assert result.get("predictions", 0) == 1
        # Live prediction must survive
        cur = await db.read_conn.execute(
            "SELECT prediction_id FROM predictions WHERE mode = 'live'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == live_pred
        # Paper prediction gone
        cur = await db.read_conn.execute(
            "SELECT prediction_id FROM predictions WHERE prediction_id = ?",
            (paper_pred,),
        )
        assert await cur.fetchone() is None

    async def test_paper_delete_does_not_touch_signals(self, db):
        """Signals share both modes with no mode column. Mode-scoped
        delete must leave them alone."""
        await db.insert_signal({
            "symbol": "RELIANCE", "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
        })

        await db.bulk_delete("paper")

        cur = await db.read_conn.execute("SELECT COUNT(*) FROM signals")
        assert (await cur.fetchone())[0] == 1

    async def test_signals_group_clears_signals(self, db):
        for _ in range(3):
            await db.insert_signal({
                "symbol": "RELIANCE", "signal_type": "BUY",
                "entry_price": 100.0, "target_price": 105.0,
                "stop_loss_price": 95.0, "position_size": 1,
                "confidence_score": 0.7, "model_version": "v1",
            })

        result = await db.bulk_delete("signals")

        assert result["signals"] == 3
        cur = await db.read_conn.execute("SELECT COUNT(*) FROM signals")
        assert (await cur.fetchone())[0] == 0

    async def test_pending_trades_group_exists(self, db):
        """Dedicated bulk group for clearing pending trades."""
        result = await db.bulk_delete("pending_trades")
        # Empty DB — just verify the group is recognized
        assert "pending_trades" in result

    async def test_unknown_group_raises(self, db):
        import pytest
        with pytest.raises(ValueError):
            await db.bulk_delete("not_a_group")

    async def test_upsert_watchlist_concurrent_with_other_write(self, db):
        """Regression: upsert_watchlist must not raise
        'cannot start a transaction within a transaction' when another
        coro is writing on the same connection. Previously the explicit
        BEGIN clashed with the implicit auto-begin from a concurrent DML.
        """
        import asyncio
        from datetime import datetime
        from yolovest.models.schemas import OHLCVBar

        bars = [
            OHLCVBar(
                timestamp=datetime(2026, 5, 1),
                open=100, high=101, low=99, close=100.5, volume=1000,
            ),
        ]

        async def writer_a():
            for _ in range(5):
                await db.upsert_ohlcv("RELIANCE", "daily", bars, "test")

        async def writer_b():
            for i in range(5):
                await db.upsert_watchlist([
                    {"symbol": f"SYM{i}", "composite_score": 0.5, "sector": "Test"},
                ])

        # Should complete without raising 'transaction within a transaction'
        await asyncio.gather(writer_a(), writer_b())


class TestOpenPositions:
    async def test_no_open_positions(self, db):
        result = await db.get_open_positions()
        assert result == []

    async def test_open_positions_returned(self, db):
        await db.conn.execute(
            "INSERT INTO trades (trade_id, symbol, signal_type, entry_price, quantity, "
            "stop_loss_price, target_price, product, mode, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("T1", "RELIANCE", "BUY", 2500, 10, 2450, 2600, "MIS", "paper", "open",
             datetime.now().isoformat()),
        )
        await db.conn.commit()
        result = await db.get_open_positions()
        assert len(result) == 1
        assert result[0]["trade_id"] == "T1"


class TestAuditLog:
    async def test_log_audit_entry(self, db):
        await db.log_audit(
            action_type="skill_run",
            skill_name="health-check",
            input_summary={"checks": ["broker", "db"]},
            output_summary={"success": True},
            duration_ms=42.5,
        )
        cursor = await db.conn.execute("SELECT * FROM audit_log")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["action_type"] == "skill_run"
        assert rows[0]["skill_name"] == "health-check"

    async def test_log_audit_minimal(self, db):
        await db.log_audit(action_type="test")
        cursor = await db.conn.execute("SELECT * FROM audit_log")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["skill_name"] is None

    async def test_log_audit_batch_mode(self, db):
        """Test that auto_commit=False defers commits until flush_audit."""
        await db.log_audit(action_type="batch1", auto_commit=False)
        await db.log_audit(action_type="batch2", auto_commit=False)
        await db.log_audit(action_type="batch3", auto_commit=False)
        await db.flush_audit()

        cursor = await db.conn.execute("SELECT * FROM audit_log")
        rows = await cursor.fetchall()
        assert len(rows) == 3
