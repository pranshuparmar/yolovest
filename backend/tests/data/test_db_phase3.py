"""Tests for database methods (portfolio state, trades, LLM reviews)."""

import pytest

from yolovest.data.db import Database


@pytest.fixture
async def db(tmp_path):
    """Create a fresh database with migrations applied."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


class TestInsertTrade:
    async def test_insert_trade(self, db):
        trade = {
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "fill_price": 2501.0,
            "quantity": 10,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "product": "MIS",
            "mode": "paper",
            "status": "filled",
            "slippage": 1.0,
        }
        await db.insert_trade(trade)

        # Verify it was inserted
        cursor = await db.conn.execute("SELECT * FROM trades")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["symbol"] == "RELIANCE"
        assert row["entry_price"] == 2500.0
        assert row["trade_id"].startswith("T-")

    async def test_insert_trade_with_custom_id(self, db):
        trade = {
            "trade_id": "CUSTOM-001",
            "symbol": "TCS",
            "signal_type": "SELL",
            "entry_price": 3500.0,
            "quantity": 5,
            "stop_loss_price": 3550.0,
            "target_price": 3400.0,
        }
        await db.insert_trade(trade)

        cursor = await db.conn.execute(
            "SELECT trade_id FROM trades WHERE symbol = 'TCS'"
        )
        row = await cursor.fetchone()
        assert row[0] == "CUSTOM-001"


class TestClosePosition:
    async def test_close_position(self, db):
        trade = {
            "trade_id": "T-CLOSE-001",
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "quantity": 10,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "status": "open",
        }
        await db.insert_trade(trade)

        closed = await db.close_position("T-CLOSE-001", exit_price=2550.0, pnl=500.0)
        assert closed is True

        cursor = await db.conn.execute(
            "SELECT status, exit_price, pnl, closed_at FROM trades WHERE trade_id = ?",
            ("T-CLOSE-001",),
        )
        row = await cursor.fetchone()
        assert row[0] == "closed"
        assert row[1] == 2550.0
        assert row[2] == 500.0
        assert row[3] is not None

    async def test_close_position_idempotent_double_close(self, db):
        """A second close must be a no-op: it must not overwrite the recorded
        exit price / PnL with a different value, and must not write a second
        audit row. Guards the manual-close-vs-position-monitor double-close
        race (and duplicate postbacks)."""
        trade = {
            "trade_id": "T-CLOSE-DUP",
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "quantity": 10,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "status": "open",
        }
        await db.insert_trade(trade)

        first = await db.close_position("T-CLOSE-DUP", exit_price=2600.0, pnl=1000.0)
        # Second close arrives with a DIFFERENT exit/pnl (e.g. a stale exit
        # order fill) — it must be rejected as a no-op.
        second = await db.close_position("T-CLOSE-DUP", exit_price=2400.0, pnl=-1000.0)

        assert first is True
        assert second is False

        cursor = await db.conn.execute(
            "SELECT exit_price, pnl FROM trades WHERE trade_id = ?",
            ("T-CLOSE-DUP",),
        )
        row = await cursor.fetchone()
        # First close wins; the second did not overwrite it.
        assert row[0] == 2600.0
        assert row[1] == 1000.0

        cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type = 'position_closed' "
            "AND input_summary LIKE '%T-CLOSE-DUP%'",
        )
        assert (await cursor.fetchone())[0] == 1


class TestUpdatePositionSL:
    async def test_update_sl(self, db):
        trade = {
            "trade_id": "T-SL-001",
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "quantity": 10,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "status": "open",
        }
        await db.insert_trade(trade)

        await db.update_position_sl("T-SL-001", 2480.0)

        cursor = await db.conn.execute(
            "SELECT stop_loss_price FROM trades WHERE trade_id = ?",
            ("T-SL-001",),
        )
        row = await cursor.fetchone()
        assert row[0] == 2480.0


class TestGetPortfolioState:
    async def test_empty_portfolio(self, db):
        state = await db.get_portfolio_state()

        assert state["total_capital"] == 100000  # default
        assert state["open_positions"] == 0
        assert state["trades_today"] == 0
        assert state["daily_pnl_pct"] == 0.0

    async def test_portfolio_with_open_positions(self, db):
        trade = {
            "trade_id": "T-PF-001",
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "quantity": 10,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "status": "open",
        }
        await db.insert_trade(trade)

        state = await db.get_portfolio_state()

        assert state["open_positions"] == 1
        assert state["trades_today"] == 1

    async def test_portfolio_with_initial_capital_set(self, db):
        await db.set_system_state("initial_capital", "200000")

        state = await db.get_portfolio_state()

        assert state["total_capital"] == 200000


class TestGetStockSector:
    async def test_get_sector_from_watchlist(self, db):
        await db.upsert_watchlist([
            {"symbol": "RELIANCE", "composite_score": 0.8, "sector": "Energy"},
        ])

        sector = await db.get_stock_sector("RELIANCE")
        assert sector == "Energy"

    async def test_no_sector(self, db):
        sector = await db.get_stock_sector("UNKNOWN")
        assert sector is None


class TestLogLLMReview:
    async def test_log_review(self, db):
        signal = {"symbol": "RELIANCE", "signal_type": "BUY"}
        await db.log_llm_review(
            signal=signal,
            decision="APPROVE",
            reasoning="Strong momentum",
        )

        cursor = await db.conn.execute("SELECT * FROM llm_reviews")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["decision"] == "APPROVE"

    async def test_log_resize_review(self, db):
        signal = {"symbol": "TCS"}
        await db.log_llm_review(
            signal=signal,
            decision="RESIZE",
            reasoning="Reduce exposure",
            adjusted_size=5,
        )

        cursor = await db.conn.execute("SELECT adjusted_size FROM llm_reviews")
        row = await cursor.fetchone()
        assert row[0] == 5


class TestGetSectorRotation:
    async def test_sector_rotation(self, db):
        await db.upsert_watchlist([
            {"symbol": "RELIANCE", "composite_score": 0.9, "sector": "Energy"},
            {"symbol": "ONGC", "composite_score": 0.85, "sector": "Energy"},
            {"symbol": "TCS", "composite_score": 0.3, "sector": "IT"},
            {"symbol": "INFY", "composite_score": 0.25, "sector": "IT"},
            {"symbol": "HDFCBANK", "composite_score": 0.6, "sector": "Banking"},
        ])

        rotation = await db.get_sector_rotation()

        assert "strong" in rotation
        assert "weak" in rotation
        assert "sectors" in rotation
        assert "Energy" in rotation["sectors"]

    async def test_empty_rotation(self, db):
        rotation = await db.get_sector_rotation()
        assert rotation == {"strong": [], "weak": [], "sectors": {}}


class TestGetTodaysTrades:
    async def test_gets_todays_trades(self, db):
        trade = {
            "trade_id": "T-TODAY-001",
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "quantity": 10,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
        }
        await db.insert_trade(trade)

        trades = await db.get_todays_trades()
        assert len(trades) == 1
        assert trades[0]["symbol"] == "RELIANCE"
