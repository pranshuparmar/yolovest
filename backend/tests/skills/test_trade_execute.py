"""Tests for trade-execute skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.trade_execute import TradeExecuteSkill


@pytest.fixture
def trade_skill(app_context):
    return TradeExecuteSkill(app_context)


@pytest.fixture
def base_signal():
    return {
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "target_price": 2600.0,
        "stop_loss_price": 2450.0,
        "position_size": 10,
        "product": "MIS",
    }


class TestPaperTrading:
    async def test_paper_trade_basic(self, trade_skill, base_signal):
        result = await trade_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["mode"] == "paper"
        trade_skill.ctx.db.insert_trade.assert_awaited_once()
        trade_skill.ctx.notify.send_trade_alert.assert_awaited_once()

    async def test_paper_slippage_applied(self, trade_skill, base_signal):
        result = await trade_skill.execute(signal=base_signal)

        trade = result.data["trade"]
        # Default paper_slippage_pct = 0.001 (0.1%)
        # BUY fill = 2500 * 1.001 = 2502.5
        assert trade["fill_price"] == 2502.50
        assert trade["slippage"] == 2.50

    async def test_paper_sell_slippage(self, trade_skill, base_signal):
        base_signal["signal_type"] = "SELL"
        result = await trade_skill.execute(signal=base_signal)

        trade = result.data["trade"]
        # SELL fill = 2500 * 0.999 = 2497.5
        assert trade["fill_price"] == 2497.50

    async def test_paper_trade_has_product(self, trade_skill, base_signal):
        result = await trade_skill.execute(signal=base_signal)

        trade = result.data["trade"]
        assert trade["product"] == "MIS"


class TestLiveTrading:
    async def test_live_trade_basic(self, trade_skill, base_signal):
        trade_skill.ctx.config.mode = "live"
        trade_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"status": "filled", "average_price": 2501.0}
        )

        result = await trade_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["mode"] == "live"
        # Should place 2 orders: primary + SL
        assert trade_skill.ctx.broker.place_order.await_count == 2

    async def test_live_slippage_tracked(self, trade_skill, base_signal):
        trade_skill.ctx.config.mode = "live"
        trade_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"status": "filled", "average_price": 2503.0}
        )

        result = await trade_skill.execute(signal=base_signal)

        trade = result.data["trade"]
        assert trade["slippage"] == 3.0
        assert trade["fill_price"] == 2503.0

    async def test_live_retry_on_failure(self, trade_skill, base_signal):
        trade_skill.ctx.config.mode = "live"
        trade_skill.ctx.broker.place_order = AsyncMock(
            side_effect=[Exception("timeout"), "ORD-1", "SL-1"]
        )
        trade_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"status": "filled", "average_price": 2500.0}
        )

        result = await trade_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["attempts"] == 2

    async def test_live_all_retries_fail(self, trade_skill, base_signal):
        trade_skill.ctx.config.mode = "live"
        trade_skill.ctx.broker.place_order = AsyncMock(
            side_effect=Exception("broker down")
        )

        result = await trade_skill.execute(signal=base_signal)

        assert not result.success
        assert "broker down" in result.error


class TestShouldRun:
    async def test_should_run_paper_mode(self, trade_skill):
        trade_skill.ctx.config.mode = "paper"
        assert trade_skill.should_run()

    async def test_should_run_live_authenticated(self, trade_skill):
        trade_skill.ctx.config.mode = "live"
        trade_skill.ctx.broker.is_authenticated = AsyncMock(return_value=True)
        assert trade_skill.should_run()
