"""Tests for database methods (dashboard queries)."""

import pytest

from yolovest.data.db import Database


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


async def _insert_trade(db, trade_id, symbol, pnl=None, status="open", closed_at=None):
    """Helper to insert a trade."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
    await db.conn.execute(
        "INSERT INTO trades (trade_id, symbol, signal_type, entry_price, fill_price, "
        "quantity, stop_loss_price, target_price, product, mode, status, pnl, "
        "created_at, closed_at) VALUES (?, ?, 'BUY', 2500, 2501, 10, 2450, 2600, "
        "'MIS', 'paper', ?, ?, ?, ?)",
        (trade_id, symbol, status, pnl, now, closed_at),
    )
    await db.conn.commit()


class TestGetTradesHistory:
    async def test_basic_query(self, db):
        await _insert_trade(db, "T-H1", "RELIANCE")
        await _insert_trade(db, "T-H2", "TCS")

        result = await db.get_trades_history()
        assert len(result) == 2

    async def test_filter_by_symbol(self, db):
        await _insert_trade(db, "T-H3", "RELIANCE")
        await _insert_trade(db, "T-H4", "TCS")

        result = await db.get_trades_history(symbol="RELIANCE")
        assert len(result) == 1
        assert result[0]["symbol"] == "RELIANCE"

    async def test_limit(self, db):
        for i in range(5):
            await _insert_trade(db, f"T-L{i}", "RELIANCE")

        result = await db.get_trades_history(limit=3)
        assert len(result) == 3


class TestGetEquityCurve:
    async def test_equity_curve(self, db):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.strftime("%Y-%m-%dT%H:%M:%S")

        await _insert_trade(db, "T-EC1", "RELIANCE", pnl=500, status="closed", closed_at=today)
        await _insert_trade(db, "T-EC2", "TCS", pnl=-200, status="closed", closed_at=today)

        curve = await db.get_equity_curve(days=7)
        assert len(curve) == 1
        assert curve[0]["daily_pnl"] == 300
        assert curve[0]["cumulative_pnl"] == 300
        assert curve[0]["trade_count"] == 2

    async def test_empty_equity_curve(self, db):
        curve = await db.get_equity_curve()
        assert curve == []


class TestGetTradeDetail:
    async def test_trade_detail_found(self, db):
        await _insert_trade(db, "T-DET1", "RELIANCE")

        detail = await db.get_trade_detail("T-DET1")
        assert detail is not None
        assert detail["symbol"] == "RELIANCE"
        assert detail["llm_review"] is None  # no linked review
        assert detail["prediction"] is None
        assert detail["signal"] is None

    async def test_trade_detail_not_found(self, db):
        detail = await db.get_trade_detail("NONEXISTENT")
        assert detail is None

    async def test_trade_detail_with_llm_review(self, db):
        await _insert_trade(db, "T-DET2", "RELIANCE")
        await db.conn.execute(
            "INSERT INTO llm_reviews (trade_id, decision, reasoning) VALUES (?, ?, ?)",
            ("T-DET2", "APPROVE", "Strong momentum"),
        )
        await db.conn.commit()

        detail = await db.get_trade_detail("T-DET2")
        assert detail["llm_review"] is not None
        assert detail["llm_review"]["decision"] == "APPROVE"


class TestGetReportsHistory:
    async def test_basic_query(self, db):
        await db.store_report({"type": "daily", "total_pnl": 500})
        await db.store_report({"type": "weekly", "total_pnl": 2000})

        result = await db.get_reports_history()
        assert len(result) == 2

    async def test_filter_by_type(self, db):
        await db.store_report({"type": "daily", "total_pnl": 500})
        await db.store_report({"type": "weekly", "total_pnl": 2000})

        result = await db.get_reports_history(report_type="daily")
        assert len(result) == 1
        assert result[0]["content"]["type"] == "daily"


class TestGetAuditLog:
    async def test_get_audit_log(self, db):
        await db.log_audit("trade_executed", skill_name="trade-execute")
        await db.log_audit("risk_check", skill_name="risk-check")

        result = await db.get_audit_log(limit=10)
        assert len(result) == 2

    async def test_filter_by_action_type(self, db):
        await db.log_audit("trade_executed")
        await db.log_audit("risk_check")

        result = await db.get_audit_log(action_type="risk_check")
        assert len(result) == 1
        assert result[0]["action_type"] == "risk_check"
