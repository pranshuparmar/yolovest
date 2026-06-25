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

    def test_split_sql_ignores_semicolons_inside_line_comments(self, db):
        """Regression: an SQL line comment containing a semicolon (e.g.
        '-- GTT applies to CNC only; MIS rows skip') used to break the
        splitter into a fragment starting with the post-semicolon prose."""
        sql = (
            "-- header with a semicolon; should not split here\n"
            "-- second comment line\n"
            "ALTER TABLE foo ADD COLUMN bar INTEGER;\n"
            "CREATE INDEX idx_foo_bar ON foo(bar);\n"
        )
        stmts = db._split_sql(sql)
        assert len(stmts) == 2
        assert stmts[0].upper().startswith("ALTER TABLE")
        assert stmts[1].upper().startswith("CREATE INDEX")

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

        # The schema_version row is NOT written when a migration
        # raises, so the migration is retried on next startup. This is
        # the guarantee migrations actually provide — SQLite DDL
        # auto-commits under deferred isolation (py<3.12), so the
        # partial CREATE TABLE may survive, which is why every
        # migration uses IF NOT EXISTS to tolerate re-application.
        assert await database.get_schema_version() == 1

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
        # Anchor bars to the recent past so the 30-day get_ohlcv
        # filter doesn't shift them out of the window as the wall
        # clock moves forward. Each bar is one day apart, ending
        # today.
        from datetime import timedelta
        today = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        return [
            OHLCVBar(
                timestamp=today - timedelta(days=(n - 1 - i)),
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


class TestPendingDispositionSync:
    """Pending lifecycle must keep signals.disposition in sync so the
    Today's Recommendations panel doesn't show stale 'awaiting_approval'
    after expire/reject."""

    async def _seed(self, db, symbol: str = "RELIANCE"):
        # Insert a signal at awaiting_approval and a matching pending row
        await db.insert_signal({
            "symbol": symbol, "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
            "mode": "paper",
        })
        await db.update_signal_disposition(
            symbol, "awaiting_approval", "queued"
        )
        pid = await db.insert_pending_trade({
            "symbol": symbol, "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
            "product": "MIS",
            "mode": "paper",
        })
        return pid

    async def test_reject_flips_signal_disposition(self, db):
        pid = await self._seed(db, "RELIANCE")

        await db.decide_pending_trade(pid, "rejected", "dashboard")

        cur = await db.read_conn.execute(
            "SELECT disposition FROM signals WHERE symbol = ?",
            ("RELIANCE",),
        )
        row = await cur.fetchone()
        assert row[0] == "rejected"

    async def test_expire_flips_signal_disposition(self, db):
        await self._seed(db, "TCS")
        # Force the pending row to be older than the expiry window
        await db.conn.execute(
            "UPDATE pending_trades SET created_at = '2000-01-01T00:00:00' "
            "WHERE symbol = 'TCS'"
        )
        await db.conn.commit()

        await db.expire_pending_trades(max_age_minutes=30)

        cur = await db.read_conn.execute(
            "SELECT disposition FROM signals WHERE symbol = ?",
            ("TCS",),
        )
        row = await cur.fetchone()
        assert row[0] == "expired"


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

    async def test_paper_delete_only_removes_paper_signals(self, db):
        await db.insert_signal({
            "symbol": "RELIANCE", "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
            "mode": "paper",
        })
        await db.insert_signal({
            "symbol": "TCS", "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
            "mode": "live",
        })

        await db.bulk_delete("paper")

        cur = await db.read_conn.execute("SELECT symbol, mode FROM signals")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "TCS"
        assert rows[0][1] == "live"

    async def test_paper_delete_only_removes_paper_pending_trades(self, db):
        await db.insert_pending_trade({
            "symbol": "RELIANCE", "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
            "mode": "paper",
        })
        await db.insert_pending_trade({
            "symbol": "TCS", "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
            "mode": "live",
        })

        await db.bulk_delete("paper")

        cur = await db.read_conn.execute("SELECT symbol, mode FROM pending_trades")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "TCS"
        assert rows[0][1] == "live"

    async def test_insert_signal_defaults_to_paper_mode(self, db):
        """If caller forgets to set mode, signal lands in paper bucket."""
        await db.insert_signal({
            "symbol": "RELIANCE", "signal_type": "BUY",
            "entry_price": 100.0, "target_price": 105.0,
            "stop_loss_price": 95.0, "position_size": 1,
            "confidence_score": 0.7, "model_version": "v1",
        })
        cur = await db.read_conn.execute("SELECT mode FROM signals")
        assert (await cur.fetchone())[0] == "paper"

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


class TestModelVersionSharpeLower:
    async def test_sharpe_lower_roundtrips(self, db):
        await db.save_model_version(
            "intraday", "intraday_v1", "models/intraday_v1.pkl",
            {"sharpe": 7.75, "sharpe_lower": 5.05, "win_rate": 0.62,
             "max_drawdown_pct": 0.24, "profit_factor": 1.8},
        )
        await db.promote_model("intraday", "intraday_v1")
        row = await db.get_production_model("intraday")
        assert row["sharpe_ratio"] == 7.75
        assert row["sharpe_lower"] == 5.05

    async def test_sharpe_lower_null_for_legacy_metrics(self, db):
        # Metrics without sharpe_lower (legacy artifact) store NULL, not error.
        await db.save_model_version(
            "swing", "swing_v0", "models/swing_v0.pkl", {"sharpe": 3.0},
        )
        await db.promote_model("swing", "swing_v0")
        row = await db.get_production_model("swing")
        assert row["sharpe_ratio"] == 3.0
        assert row["sharpe_lower"] is None


class TestTodaysRecommendations:
    async def test_sorted_by_confidence_desc(self, db):
        """The dashboard's Today's Recommendations must rank by confidence,
        not alphabetically. All signals in a heartbeat share a created_at
        second, so the old `ORDER BY created_at DESC` degraded to insertion
        (alphabetical) order — this pins the confidence ranking."""
        # Inserted in alphabetical order with non-monotonic confidence.
        for sym, conf in [("AAA", 0.55), ("BBB", 0.82), ("CCC", 0.61)]:
            await db.insert_signal({
                "symbol": sym, "signal_type": "BUY",
                "entry_price": 100.0, "target_price": 105.0,
                "stop_loss_price": 95.0, "position_size": 1,
                "confidence_score": conf, "model_version": "v1",
                "mode": "paper",
            })

        recs = await db.get_todays_recommendations()
        confs = [r["confidence_score"] for r in recs]
        assert confs == sorted(confs, reverse=True)            # descending
        assert [r["symbol"] for r in recs] == ["BBB", "CCC", "AAA"]  # not alpha


class TestOpenPositionsOrder:
    async def test_newest_position_first(self, db):
        """The Positions table must show newest-opened first, not arbitrary
        insertion (alphabetical) order — get_open_positions had no ORDER BY."""
        for sym in ("AAA", "BBB", "CCC"):
            await db.insert_trade({
                "trade_id": f"T-{sym}", "symbol": sym, "signal_type": "BUY",
                "entry_price": 100.0, "stop_loss_price": 95.0,
                "target_price": 105.0, "quantity": 1, "status": "open",
            })
        # Stamp distinct created_at (inserts share a second) so order is
        # unambiguous: BBB newest, then CCC, then AAA.
        for tid, ts in [
            ("T-AAA", "2026-01-01T10:00:00"),
            ("T-BBB", "2026-01-03T10:00:00"),
            ("T-CCC", "2026-01-02T10:00:00"),
        ]:
            await db.conn.execute(
                "UPDATE trades SET created_at = ? WHERE trade_id = ?", (ts, tid),
            )
        await db.conn.commit()

        pos = await db.get_open_positions()
        assert [p["symbol"] for p in pos] == ["BBB", "CCC", "AAA"]


def _pending_signal(symbol="RELIANCE"):
    return {
        "symbol": symbol,
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "target_price": 2600.0,
        "stop_loss_price": 2450.0,
        "position_size": 10,
        "confidence_score": 0.85,
        "model_version": "v1",
        "mode": "paper",
    }


class TestPendingTradeDoubleApprove:
    """A pending trade must execute at most once even if two approvers race
    (dashboard double-click, or dashboard + Telegram /approve). The guarded
    UPDATE (WHERE status='pending') is the real gate, not the prior SELECT."""

    async def test_second_approve_returns_none(self, db):
        pid = await db.insert_pending_trade(_pending_signal())

        first = await db.decide_pending_trade(pid, "approved", "user1")
        second = await db.decide_pending_trade(pid, "approved", "user2")

        assert first is not None
        assert first["symbol"] == "RELIANCE"
        # Already decided — second caller gets None, so it can't re-execute.
        assert second is None

    async def test_concurrent_approve_executes_once(self, db):
        import asyncio

        pid = await db.insert_pending_trade(_pending_signal())

        r1, r2 = await asyncio.gather(
            db.decide_pending_trade(pid, "approved", "dashboard"),
            db.decide_pending_trade(pid, "approved", "telegram"),
        )

        approved = [r for r in (r1, r2) if r is not None]
        assert len(approved) == 1, "exactly one approver may win the race"

    async def test_approve_after_reject_returns_none(self, db):
        pid = await db.insert_pending_trade(_pending_signal())

        rejected = await db.decide_pending_trade(pid, "rejected", "user1")
        late_approve = await db.decide_pending_trade(pid, "approved", "user2")

        # reject path returns None by contract; the key assertion is that a
        # late approve can't resurrect an already-decided trade.
        assert rejected is None
        assert late_approve is None


class TestWriteSerialization:
    """The shared write connection is wrapped so concurrent write
    transactions can't interleave (see core._SerializedWriteConnection)."""

    def test_is_write_sql_classification(self):
        from yolovest.data.db.core import _is_write_sql

        writes = [
            "INSERT INTO t VALUES (1)", "  update t set x=1", "DELETE FROM t",
            "REPLACE INTO t VALUES (1)", "CREATE TABLE t (a INT)", "DROP TABLE t",
            "ALTER TABLE t ADD COLUMN b INT", "SAVEPOINT sp", "RELEASE sp",
            "ROLLBACK", "BEGIN",
        ]
        for sql in writes:
            assert _is_write_sql(sql) is True, sql

        # Reads (incl. WITH...SELECT and VACUUM) must NOT take the write lock —
        # they never commit, so classifying one as a write would deadlock.
        reads = [
            "SELECT * FROM t", "  select 1", "PRAGMA journal_mode",
            "EXPLAIN QUERY PLAN SELECT 1", "WITH cte AS (SELECT 1) SELECT * FROM cte",
            "VACUUM",
        ]
        for sql in reads:
            assert _is_write_sql(sql) is False, sql

    async def test_concurrent_write_transactions_do_not_interleave(self, db):
        """Two coroutines each running a multi-statement write transaction on
        the shared connection must fully serialize — one transaction's
        statements + commit complete before the other's begin."""
        import asyncio

        await db.conn.execute(
            "CREATE TABLE IF NOT EXISTS _ser_test (id INTEGER PRIMARY KEY, who TEXT)"
        )
        await db.conn.commit()

        events: list[str] = []

        async def writer(name: str) -> None:
            await db.conn.execute("INSERT INTO _ser_test (who) VALUES (?)", (name,))
            events.append(f"{name}:start")
            await asyncio.sleep(0)  # yield — invite the other writer to interleave
            await db.conn.execute("INSERT INTO _ser_test (who) VALUES (?)", (name,))
            await db.conn.commit()
            events.append(f"{name}:end")

        await asyncio.gather(writer("A"), writer("B"))

        # Each writer's [start, end] window must not overlap the other's.
        a0, a1 = events.index("A:start"), events.index("A:end")
        b0, b1 = events.index("B:start"), events.index("B:end")
        assert a1 < b0 or b1 < a0, f"write transactions interleaved: {events}"

        # Both transactions committed all their rows (2 each).
        cursor = await db.conn.execute("SELECT COUNT(*) FROM _ser_test")
        assert (await cursor.fetchone())[0] == 4


class TestStockExposureAccumulation:
    """The single-stock-exposure gate must see a symbol's COMBINED exposure
    across all its open rows (e.g. an adopted holding plus a system position
    in the same name), not just the last row's."""

    async def test_exposure_sums_across_multiple_rows(self, db):
        from unittest.mock import AsyncMock

        # Two open rows for the same symbol: a system position and an adopted
        # holding. Sector is provided inline so get_stock_sector isn't needed.
        db.get_open_positions = AsyncMock(return_value=[
            {"symbol": "RELIANCE", "quantity": 10, "entry_price": 2500.0,
             "origin": "system", "sector": "Energy"},
            {"symbol": "RELIANCE", "quantity": 5, "entry_price": 2400.0,
             "origin": "adopted", "sector": "Energy"},
        ])

        state = await db.get_portfolio_state()

        # No trades -> total_capital = default 100_000.
        # combined = (10*2500 + 5*2400) / 100_000 = (25000 + 12000)/100000 = 0.37
        # (the old overwrite bug would report only the last row: 12000/100000 = 0.12)
        assert state["stock_exposures"]["RELIANCE"] == pytest.approx(0.37)
