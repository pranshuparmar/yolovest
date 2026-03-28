"""Tests for ghost position recovery in position-monitor."""

from unittest.mock import AsyncMock, patch

import pytest

from yolovest.skills.position_monitor import PositionMonitorSkill


@pytest.fixture
def monitor(app_context):
    return PositionMonitorSkill(app_context)


@pytest.fixture
def live_position():
    """A live-mode open position."""
    return {
        "trade_id": "T-live-001",
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "stop_loss_price": 2450.0,
        "target_price": 2600.0,
        "quantity": 10,
        "product": "MIS",
        "mode": "live",
        "sl_order_id": "SL-001",
    }


@pytest.fixture
def paper_position():
    """A paper-mode open position."""
    return {
        "trade_id": "T-paper-001",
        "symbol": "TCS",
        "signal_type": "BUY",
        "entry_price": 3500.0,
        "stop_loss_price": 3450.0,
        "target_price": 3600.0,
        "quantity": 5,
        "product": "MIS",
        "mode": "paper",
    }


class TestGhostRecovery:
    async def test_detects_ghost_position(self, monitor, live_position):
        """Position in DB but not on broker → should be closed."""
        monitor.ctx.config.mode = "live"
        monitor.ctx.db.get_open_positions = AsyncMock(return_value=[live_position])
        # Broker returns empty — position was closed broker-side
        monitor.ctx.broker.get_positions = AsyncMock(return_value=[])
        # LTP for PnL calculation
        monitor.ctx.market_data.get_ltp = AsyncMock(return_value=2480.0)

        result = await monitor.execute()

        monitor.ctx.db.close_position.assert_awaited_once()
        call_args = monitor.ctx.db.close_position.call_args
        assert call_args[0][0] == "T-live-001"  # trade_id
        assert call_args[0][1] == 2480.0  # exit_price
        assert "RELIANCE" in result.data.get("ghost_recovered", [])

    async def test_ghost_uses_sl_price_when_ltp_unavailable(self, monitor, live_position):
        """If LTP fetch fails, use stop-loss price as exit estimate."""
        monitor.ctx.config.mode = "live"
        monitor.ctx.db.get_open_positions = AsyncMock(return_value=[live_position])
        monitor.ctx.broker.get_positions = AsyncMock(return_value=[])
        # LTP fails
        monitor.ctx.market_data.get_ltp = AsyncMock(side_effect=Exception("API down"))

        result = await monitor.execute()

        monitor.ctx.db.close_position.assert_awaited_once()
        call_args = monitor.ctx.db.close_position.call_args
        # Should use SL price as fallback
        assert call_args[0][1] == 2450.0
        assert "RELIANCE" in result.data.get("ghost_recovered", [])

    async def test_skips_paper_positions(self, monitor, paper_position):
        """Paper mode positions should not be recovered as ghosts."""
        monitor.ctx.config.mode = "live"
        monitor.ctx.db.get_open_positions = AsyncMock(return_value=[paper_position])
        monitor.ctx.broker.get_positions = AsyncMock(return_value=[])

        recovered = await monitor._recover_ghost_positions(
            [paper_position], [],
        )

        assert recovered == []
        monitor.ctx.db.close_position.assert_not_awaited()

    async def test_skips_when_paper_mode(self, monitor, live_position):
        """Entire ghost recovery is skipped in paper mode."""
        monitor.ctx.config.mode = "paper"

        recovered = await monitor._recover_ghost_positions(
            [live_position], [],
        )

        assert recovered == []

    async def test_no_ghost_when_broker_has_position(self, monitor, live_position):
        """Position exists on both DB and broker → not a ghost."""
        monitor.ctx.config.mode = "live"
        broker_pos = {
            "tradingsymbol": "RELIANCE",
            "quantity": 10,
        }

        recovered = await monitor._recover_ghost_positions(
            [live_position], [broker_pos],
        )

        assert recovered == []
        monitor.ctx.db.close_position.assert_not_awaited()

    async def test_ghost_when_broker_qty_zero(self, monitor, live_position):
        """Broker shows qty=0 for the symbol → ghost."""
        monitor.ctx.config.mode = "live"
        broker_pos = {
            "tradingsymbol": "RELIANCE",
            "quantity": 0,
        }
        monitor.ctx.market_data.get_ltp = AsyncMock(return_value=2520.0)

        recovered = await monitor._recover_ghost_positions(
            [live_position], [broker_pos],
        )

        assert "RELIANCE" in recovered
        monitor.ctx.db.close_position.assert_awaited_once()

    async def test_recovered_positions_skipped_in_monitor_loop(self, monitor, live_position):
        """Recovered ghosts should not be checked for SL/target."""
        monitor.ctx.config.mode = "live"
        monitor.ctx.db.get_open_positions = AsyncMock(return_value=[live_position])
        monitor.ctx.broker.get_positions = AsyncMock(return_value=[])
        monitor.ctx.market_data.get_ltp = AsyncMock(return_value=2480.0)

        result = await monitor.execute()

        # close_position called once (from ghost recovery), not twice
        assert monitor.ctx.db.close_position.await_count == 1
        # No targets/stops hit (position was recovered, not monitored)
        assert result.data["targets_hit"] == []
        assert result.data["stops_hit"] == []

    async def test_sends_exit_alert_for_ghost(self, monitor, live_position):
        """Ghost recovery should send an exit alert."""
        monitor.ctx.config.mode = "live"
        monitor.ctx.db.get_open_positions = AsyncMock(return_value=[live_position])
        monitor.ctx.broker.get_positions = AsyncMock(return_value=[])
        monitor.ctx.market_data.get_ltp = AsyncMock(return_value=2480.0)

        await monitor.execute()

        monitor.ctx.notify.send_exit_alert.assert_awaited_once()
        call_args = monitor.ctx.notify.send_exit_alert.call_args
        assert call_args[0][0] == "RELIANCE"
        assert "auto-recovered" in call_args[0][1].lower()

    async def test_pnl_computed_correctly_for_buy(self, monitor, live_position):
        """PnL for recovered BUY: (exit - entry) * qty - costs."""
        monitor.ctx.config.mode = "live"
        monitor.ctx.db.get_open_positions = AsyncMock(return_value=[live_position])
        monitor.ctx.broker.get_positions = AsyncMock(return_value=[])
        monitor.ctx.market_data.get_ltp = AsyncMock(return_value=2520.0)

        await monitor.execute()

        call_args = monitor.ctx.db.close_position.call_args
        pnl = call_args[0][2]
        # Gross: (2520 - 2500) * 10 = 200, minus transaction costs
        assert pnl < 200.0
        assert pnl > 150.0  # costs should be small on ₹25k trade
