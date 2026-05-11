"""Tests for square-off skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.square_off import SquareOffSkill


@pytest.fixture
def square_off_skill(app_context):
    return SquareOffSkill(app_context)


@pytest.fixture
def mis_position():
    return {
        "id": 1,
        "trade_id": "T-001",
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "quantity": 10,
        "product": "MIS",
        "sl_order_id": "SL-001",
    }


@pytest.fixture
def cnc_position():
    return {
        "id": 2,
        "trade_id": "T-002",
        "symbol": "TCS",
        "signal_type": "BUY",
        "entry_price": 3500.0,
        "quantity": 5,
        "product": "CNC",
        "sl_order_id": "SL-002",
    }


class TestSquareOff:
    async def test_square_off_mis_only(self, square_off_skill, mis_position, cnc_position):
        square_off_skill.ctx.db.get_open_positions = AsyncMock(
            return_value=[mis_position, cnc_position]
        )
        square_off_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"average_price": 2520.0}
        )

        result = await square_off_skill.execute()

        assert result.success
        assert len(result.data["squared_off"]) == 1
        assert result.data["squared_off"][0]["symbol"] == "RELIANCE"
        # Gross PnL = (2520 - 2500) * 10 = 200, minus transaction costs
        pnl = result.data["squared_off"][0]["pnl"]
        assert pnl < 200.0  # reduced by transaction costs
        assert pnl > 150.0  # costs ~₹25 on ₹25k trade

    async def test_force_closes_all(self, square_off_skill, mis_position, cnc_position):
        square_off_skill.ctx.db.get_open_positions = AsyncMock(
            return_value=[mis_position, cnc_position]
        )
        square_off_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"average_price": 2520.0}
        )

        result = await square_off_skill.execute(force=True)

        assert result.success
        assert len(result.data["squared_off"]) == 2
        assert result.data["force"]

    async def test_cancel_sl_before_exit(self, square_off_skill, mis_position):
        square_off_skill.ctx.db.get_open_positions = AsyncMock(
            return_value=[mis_position]
        )
        square_off_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"average_price": 2480.0}
        )

        await square_off_skill.execute()

        square_off_skill.ctx.broker.cancel_order.assert_awaited_with("SL-001")

    async def test_sell_pnl_computed_correctly(self, square_off_skill):
        sell_pos = {
            "id": 1,
            "trade_id": "T-001",
            "symbol": "RELIANCE",
            "signal_type": "SELL",
            "entry_price": 2500.0,
            "quantity": 10,
            "product": "MIS",
            "sl_order_id": "SL-001",
        }
        square_off_skill.ctx.db.get_open_positions = AsyncMock(return_value=[sell_pos])
        square_off_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"average_price": 2480.0}
        )

        result = await square_off_skill.execute()

        # Gross PnL = (2500 - 2480) * 10 = 200, minus transaction costs
        pnl = result.data["squared_off"][0]["pnl"]
        assert pnl < 200.0
        assert pnl > 150.0  # costs ~₹25 on ₹25k trade

    async def test_failure_handling(self, square_off_skill, mis_position):
        square_off_skill.ctx.db.get_open_positions = AsyncMock(
            return_value=[mis_position]
        )
        square_off_skill.ctx.broker.place_order = AsyncMock(
            side_effect=Exception("broker unavailable")
        )

        result = await square_off_skill.execute()

        assert not result.success
        assert len(result.data["failures"]) == 1

    async def test_telegram_notification(self, square_off_skill, mis_position):
        square_off_skill.ctx.db.get_open_positions = AsyncMock(
            return_value=[mis_position]
        )
        square_off_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"average_price": 2520.0}
        )

        await square_off_skill.execute()

        square_off_skill.ctx.notify.send.assert_awaited()

    async def test_no_positions_to_close(self, square_off_skill):
        square_off_skill.ctx.db.get_open_positions = AsyncMock(return_value=[])

        result = await square_off_skill.execute()

        assert result.success
        assert len(result.data["squared_off"]) == 0
