"""Tests for report-generate skill (Phase 4, FR-8.4, FR-8.5)."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.report_generate import ReportGenerateSkill


@pytest.fixture
def report_skill(app_context):
    return ReportGenerateSkill(app_context)


class TestDailyReport:
    async def test_daily_report_basic(self, report_skill):
        report_skill.ctx.db.get_todays_trades = AsyncMock(return_value=[
            {"symbol": "RELIANCE", "pnl": 500, "slippage": 2.5},
            {"symbol": "TCS", "pnl": -200, "slippage": 1.0},
        ])
        report_skill.ctx.db.get_todays_predictions = AsyncMock(return_value=[])
        report_skill.ctx.llm.summarize_market_day = AsyncMock(return_value="Good day")

        result = await report_skill.execute(type="daily")

        assert result.success
        assert result.data["type"] == "daily"
        assert result.data["total_trades"] == 2
        assert result.data["total_pnl"] == 300
        assert result.data["wins"] == 1
        assert result.data["losses"] == 1
        assert result.data["win_rate"] == 0.5
        report_skill.ctx.db.store_report.assert_awaited_once()

    async def test_daily_report_no_trades(self, report_skill):
        result = await report_skill.execute(type="daily")

        assert result.success
        assert result.data["total_trades"] == 0
        assert result.data["total_pnl"] == 0
        assert result.data["win_rate"] == 0

    async def test_daily_report_with_predictions(self, report_skill):
        report_skill.ctx.db.get_todays_trades = AsyncMock(return_value=[])
        report_skill.ctx.db.get_todays_predictions = AsyncMock(return_value=[
            {"direction_correct": 1},
            {"direction_correct": 1},
            {"direction_correct": 0},
        ])

        result = await report_skill.execute(type="daily")

        assert result.data["prediction_accuracy"] == pytest.approx(2 / 3)
        assert result.data["predictions_scored"] == 3

    async def test_daily_sends_telegram(self, report_skill):
        report_skill.ctx.config.notifications.telegram.alerts.daily_summary = True

        await report_skill.execute(type="daily")

        report_skill.ctx.notify.send.assert_awaited()

    async def test_daily_llm_failure_handled(self, report_skill):
        report_skill.ctx.llm.summarize_market_day = AsyncMock(
            side_effect=Exception("API down")
        )

        result = await report_skill.execute(type="daily")

        assert result.success
        assert result.data["market_summary"] is None


class TestWeeklyReport:
    async def test_weekly_report_basic(self, report_skill):
        report_skill.ctx.db.get_weekly_trades = AsyncMock(return_value=[
            {"symbol": "RELIANCE", "pnl": 1000, "trade_id": "T-1"},
            {"symbol": "TCS", "pnl": -300, "trade_id": "T-2"},
            {"symbol": "INFY", "pnl": 500, "trade_id": "T-3"},
        ])
        report_skill.ctx.db.get_weekly_predictions = AsyncMock(return_value=[])
        report_skill.ctx.db.get_weekly_llm_reviews = AsyncMock(return_value=[
            {"decision": "APPROVE", "trade_pnl": 1000},
            {"decision": "APPROVE", "trade_pnl": -300},
            {"decision": "REJECT", "trade_pnl": None},
        ])

        result = await report_skill.execute(type="weekly")

        assert result.success
        assert result.data["type"] == "weekly"
        assert result.data["total_trades"] == 3
        assert result.data["total_pnl"] == 1200
        assert result.data["llm_approvals"] == 2
        assert result.data["llm_rejections"] == 1
        assert result.data["llm_approved_pnl"] == 700
        assert result.data["best_trade"]["symbol"] == "RELIANCE"
        assert result.data["worst_trade"]["symbol"] == "TCS"

    async def test_weekly_sends_telegram(self, report_skill):
        report_skill.ctx.config.notifications.telegram.alerts.weekly_summary = True

        await report_skill.execute(type="weekly")

        report_skill.ctx.notify.send.assert_awaited()


class TestReportFormatting:
    def test_format_daily_report(self):
        report = {
            "total_trades": 5,
            "total_pnl": 1500.50,
            "win_rate": 0.6,
            "wins": 3,
            "losses": 2,
            "avg_slippage": 1.25,
            "prediction_accuracy": 0.75,
            "predictions_scored": 4,
        }
        formatted = ReportGenerateSkill._format_daily_report(report)

        assert "Daily Report" in formatted
        assert "1,500.50" in formatted
        assert "60%" in formatted
        assert "75%" in formatted

    def test_format_weekly_report(self):
        report = {
            "total_trades": 15,
            "total_pnl": -500,
            "win_rate": 0.4,
            "llm_approvals": 10,
            "llm_rejections": 3,
            "llm_approved_pnl": -200,
            "best_trade": {"symbol": "RELIANCE", "pnl": 800},
            "worst_trade": {"symbol": "TCS", "pnl": -600},
        }
        formatted = ReportGenerateSkill._format_weekly_report(report)

        assert "Weekly Report" in formatted
        assert "RELIANCE" in formatted
        assert "TCS" in formatted
