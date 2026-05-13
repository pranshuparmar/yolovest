"""Tests for position-monitor skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.position_monitor import PositionMonitorSkill


@pytest.fixture
def monitor_skill(app_context):
    return PositionMonitorSkill(app_context)


@pytest.fixture
def open_position():
    return {
        "id": 1,
        "trade_id": "T-001",
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "stop_loss_price": 2450.0,
        "target_price": 2600.0,
        "quantity": 10,
        "sl_order_id": "SL-001",
        "mode": "paper",
    }


class TestPositionMonitoring:
    async def test_no_positions(self, monitor_skill):
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])

        result = await monitor_skill.execute()

        assert result.success
        assert result.data["positions_monitored"] == 0

    async def test_target_hit_detected(self, monitor_skill, open_position):
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2610.0)

        result = await monitor_skill.execute()

        assert result.success
        assert "RELIANCE" in result.data["targets_hit"]

    async def test_early_exit_buffer_fires_just_below_target(
        self, monitor_skill, open_position,
    ):
        """LTP within target_early_exit_pct of target should still trigger."""
        monitor_skill.ctx.config.risk.target_early_exit_pct = 0.0015  # 0.15%
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        # target=2600, buffer 0.15% → trigger at 2596.10. LTP 2597 should fire.
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2597.0)

        result = await monitor_skill.execute()

        assert result.success
        assert "RELIANCE" in result.data["targets_hit"]

    async def test_early_exit_buffer_does_not_fire_beyond_buffer(
        self, monitor_skill, open_position,
    ):
        """LTP outside the buffer band should NOT trigger target exit."""
        monitor_skill.ctx.config.risk.target_early_exit_pct = 0.0015  # 0.15%
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        # target=2600, trigger=2596.10. LTP 2590 is below the band, no fire.
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2590.0)

        result = await monitor_skill.execute()

        assert result.success
        assert "RELIANCE" not in result.data["targets_hit"]

    async def test_zero_buffer_preserves_exact_target_behaviour(
        self, monitor_skill, open_position,
    ):
        """With buffer=0 the check collapses to the original `>= target`."""
        monitor_skill.ctx.config.risk.target_early_exit_pct = 0.0
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2599.99)

        result = await monitor_skill.execute()

        assert result.success
        assert "RELIANCE" not in result.data["targets_hit"]

    async def test_stop_loss_hit_detected(self, monitor_skill, open_position):
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2440.0)

        result = await monitor_skill.execute()

        assert result.success
        assert "RELIANCE" in result.data["stops_hit"]


class TestTrailingSL:
    async def test_trailing_sl_triggered(self, monitor_skill, open_position):
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        # Price at 2580: profit=80, risk=50, multiple=1.6 > trigger(1.5)
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2580.0)

        result = await monitor_skill.execute()

        assert result.success
        assert result.data["trails_modified"] == 1
        monitor_skill.ctx.broker.modify_sl_order.assert_awaited_once()
        monitor_skill.ctx.db.update_position_sl.assert_awaited_once()

    async def test_trailing_sl_not_triggered_below_multiple(self, monitor_skill, open_position):
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        # Price at 2520: profit=20, risk=50, multiple=0.4 < trigger(1.5)
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2520.0)

        result = await monitor_skill.execute()

        assert result.data["trails_modified"] == 0

    async def test_trailing_sl_disabled(self, monitor_skill, open_position):
        monitor_skill.ctx.config.risk.trailing_sl_enabled = False
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[open_position])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[])
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=2580.0)

        result = await monitor_skill.execute()

        assert result.data["trails_modified"] == 0


class TestReconciliation:
    def test_reconcile_no_discrepancies(self, monitor_skill):
        local = [{"symbol": "RELIANCE", "quantity": 10, "mode": "paper"}]
        broker = []  # paper mode, no broker positions expected

        discrepancies = monitor_skill._reconcile(local, broker)
        assert discrepancies == []

    def test_reconcile_qty_mismatch(self, monitor_skill):
        local = [{"symbol": "RELIANCE", "quantity": 10, "mode": "live"}]
        broker = [{"symbol": "RELIANCE", "quantity": 5}]

        discrepancies = monitor_skill._reconcile(local, broker)
        assert len(discrepancies) == 1
        assert "qty mismatch" in discrepancies[0]

    def test_reconcile_missing_on_broker(self, monitor_skill):
        local = [{"symbol": "RELIANCE", "quantity": 10, "mode": "live"}]
        broker = []

        discrepancies = monitor_skill._reconcile(local, broker)
        assert len(discrepancies) == 1
        assert "not on broker" in discrepancies[0]

    def test_reconcile_extra_on_broker(self, monitor_skill):
        local = []
        broker = [{"symbol": "TCS", "quantity": 5}]

        discrepancies = monitor_skill._reconcile(local, broker)
        assert len(discrepancies) == 1
        assert "not in local DB" in discrepancies[0]

    def test_reconcile_ignores_zero_qty_broker(self, monitor_skill):
        local = []
        broker = [{"symbol": "TCS", "quantity": 0}]

        discrepancies = monitor_skill._reconcile(local, broker)
        assert discrepancies == []


class TestIsBetterSL:
    def test_buy_higher_sl_is_better(self, monitor_skill):
        assert monitor_skill._is_better_sl("BUY", 2460, 2450)

    def test_buy_lower_sl_is_not_better(self, monitor_skill):
        assert not monitor_skill._is_better_sl("BUY", 2440, 2450)

    def test_sell_lower_sl_is_better(self, monitor_skill):
        assert monitor_skill._is_better_sl("SELL", 2540, 2550)

    def test_sell_higher_sl_is_not_better(self, monitor_skill):
        assert not monitor_skill._is_better_sl("SELL", 2560, 2550)
