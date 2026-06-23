"""Tests for position-monitor skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.position_monitor import PositionMonitorSkill


@pytest.fixture
def monitor_skill(app_context):
    # Disable partial-profit booking by default so target-hit tests
    # measure full-exit behaviour. Individual tests that need partial-
    # profit behaviour re-enable it locally.
    app_context.config.risk.partial_profit.enabled = False
    skill = PositionMonitorSkill(app_context)
    # Default mocks for DB methods touched by position-monitor that
    # aren't the focus of individual tests.
    skill.ctx.db.get_locked_symbols = AsyncMock(return_value=set())
    return skill


@pytest.fixture
def open_position():
    # NOTE: deliberately no sl_order_id / target_order_id / gtt_id —
    # the client-side target / SL detection in PositionMonitorSkill
    # short-circuits when any broker-side exit is present. Tests that
    # specifically need to verify broker-OCO behaviour set those keys
    # locally on a copy of this fixture.
    return {
        "id": 1,
        "trade_id": "T-001",
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "stop_loss_price": 2450.0,
        "target_price": 2600.0,
        "quantity": 10,
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


class TestGttReconciler:
    """When a CNC trade has an attached GTT, position-monitor must wipe
    `gtt_id` if the GTT is no longer protecting the position (cancelled
    on Kite web, rejected at trigger, expired, or vanished entirely).
    Otherwise client-side detection stays disabled and the position is
    unprotected."""

    @pytest.fixture
    def cnc_with_gtt(self):
        return {
            "id": 1,
            "trade_id": "T-cnc-001",
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "fill_price": 2500.0,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "quantity": 10,
            "product": "CNC",
            "gtt_id": 42,
            "gtt_status": "active",
            "mode": "live",
        }

    async def test_active_gtt_keeps_gtt_id(self, monitor_skill, cnc_with_gtt):
        monitor_skill.ctx.broker.get_gtts = AsyncMock(return_value=[
            {"id": 42, "status": "active"},
        ])
        await monitor_skill._reconcile_gtts([cnc_with_gtt])
        assert cnc_with_gtt["gtt_id"] == 42
        assert cnc_with_gtt["gtt_status"] == "active"

    async def test_cancelled_gtt_clears_gtt_id(self, monitor_skill, cnc_with_gtt):
        monitor_skill.ctx.broker.get_gtts = AsyncMock(return_value=[
            {"id": 42, "status": "cancelled"},
        ])
        await monitor_skill._reconcile_gtts([cnc_with_gtt])
        assert cnc_with_gtt["gtt_id"] is None
        assert cnc_with_gtt["gtt_status"] == "cancelled"
        monitor_skill.ctx.db.set_trade_gtt.assert_awaited_with("T-cnc-001", None)

    async def test_missing_gtt_clears_gtt_id(self, monitor_skill, cnc_with_gtt):
        """GTT vanished from broker entirely → fall back to client side."""
        monitor_skill.ctx.broker.get_gtts = AsyncMock(return_value=[])
        await monitor_skill._reconcile_gtts([cnc_with_gtt])
        assert cnc_with_gtt["gtt_id"] is None
        assert cnc_with_gtt["gtt_status"] == "missing"

    async def test_triggered_gtt_clears_gtt_id(self, monitor_skill, cnc_with_gtt):
        """GTT fired (and presumably filled) — local row will be reconciled
        by ghost recovery, but gtt_id must clear so the OCO check below
        doesn't try to protect a closed position."""
        monitor_skill.ctx.broker.get_gtts = AsyncMock(return_value=[
            {"id": 42, "status": "triggered"},
        ])
        await monitor_skill._reconcile_gtts([cnc_with_gtt])
        assert cnc_with_gtt["gtt_id"] is None

    async def test_broker_error_keeps_state_unchanged(self, monitor_skill, cnc_with_gtt):
        """Network blip — don't pessimistically wipe gtt_id."""
        monitor_skill.ctx.broker.get_gtts = AsyncMock(side_effect=RuntimeError("kite down"))
        await monitor_skill._reconcile_gtts([cnc_with_gtt])
        assert cnc_with_gtt["gtt_id"] == 42
        assert cnc_with_gtt["gtt_status"] == "active"


class TestGttValidation:
    """Pre-flight checks on GTT params catch bugs before we waste API calls."""

    def test_long_exit_sl_above_ltp_rejected(self):
        from yolovest.skills.trade_execute import TradeExecuteSkill
        err = TradeExecuteSkill._validate_gtt_params(
            exit_side="SELL", sl_trig=2510.0, tgt_trig=2600.0,
            last_price=2500.0, quantity=10,
        )
        assert err is not None and "SL trigger" in err

    def test_long_exit_target_below_ltp_rejected(self):
        from yolovest.skills.trade_execute import TradeExecuteSkill
        err = TradeExecuteSkill._validate_gtt_params(
            exit_side="SELL", sl_trig=2450.0, tgt_trig=2490.0,
            last_price=2500.0, quantity=10,
        )
        assert err is not None and "target trigger" in err

    def test_valid_long_exit_passes(self):
        from yolovest.skills.trade_execute import TradeExecuteSkill
        err = TradeExecuteSkill._validate_gtt_params(
            exit_side="SELL", sl_trig=2450.0, tgt_trig=2600.0,
            last_price=2500.0, quantity=10,
        )
        assert err is None

    def test_zero_quantity_rejected(self):
        from yolovest.skills.trade_execute import TradeExecuteSkill
        err = TradeExecuteSkill._validate_gtt_params(
            exit_side="SELL", sl_trig=2450.0, tgt_trig=2600.0,
            last_price=2500.0, quantity=0,
        )
        assert err is not None and "quantity" in err

    def test_crossed_legs_rejected(self):
        from yolovest.skills.trade_execute import TradeExecuteSkill
        err = TradeExecuteSkill._validate_gtt_params(
            exit_side="SELL", sl_trig=2500.0, tgt_trig=2500.0,
            last_price=2500.0, quantity=10,
        )
        assert err is not None


class TestMisOcoEnforcement:
    """When a MIS trade has both a target LIMIT and an SL at the broker,
    position-monitor should let the broker enforce exits and only cancel
    the surviving leg when one fills."""

    @pytest.fixture
    def mis_position_with_oco(self):
        return {
            "id": 1,
            "trade_id": "T-mis-001",
            "symbol": "INDUSTOWER",
            "signal_type": "BUY",
            "entry_price": 402.45,
            "fill_price": 402.50,
            "stop_loss_price": 398.65,
            "target_price": 410.20,
            "quantity": 48,
            "product": "MIS",
            "sl_order_id": "SL-XYZ",
            "target_order_id": "TGT-XYZ",
            "mode": "live",
        }

    async def test_skips_client_side_target_when_oco_active(
        self, monitor_skill, mis_position_with_oco,
    ):
        """LTP at target shouldn't trigger client-side close — broker
        LIMIT is on the book and will fill itself."""
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[mis_position_with_oco])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[
            {"tradingsymbol": "INDUSTOWER", "quantity": 48},
        ])
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=410.25)
        # Both broker orders still open
        monitor_skill.ctx.broker.get_order_status = AsyncMock(return_value={"status": "OPEN"})

        result = await monitor_skill.execute()

        assert result.success
        assert "INDUSTOWER" not in result.data["targets_hit"]
        monitor_skill.ctx.db.close_position.assert_not_awaited()

    async def test_cancels_sl_when_target_fills(
        self, monitor_skill, mis_position_with_oco,
    ):
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[mis_position_with_oco])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[
            {"tradingsymbol": "INDUSTOWER", "quantity": 48},
        ])
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=410.25)
        # Target filled, SL still open
        async def status(oid):
            if oid == "TGT-XYZ":
                return {"status": "COMPLETE"}
            return {"status": "TRIGGER PENDING"}
        monitor_skill.ctx.broker.get_order_status = AsyncMock(side_effect=status)
        monitor_skill.ctx.broker.cancel_order = AsyncMock(return_value=True)

        await monitor_skill.execute()

        monitor_skill.ctx.broker.cancel_order.assert_awaited_once_with("SL-XYZ")
        monitor_skill.ctx.db.set_trade_sl_order_id.assert_awaited_once_with(
            "T-mis-001", None,
        )

    async def test_cancels_target_when_sl_fills(
        self, monitor_skill, mis_position_with_oco,
    ):
        monitor_skill.ctx.market_hours.is_market_hours = lambda: True
        monitor_skill.ctx.db.get_open_positions = AsyncMock(return_value=[mis_position_with_oco])
        monitor_skill.ctx.broker.get_positions = AsyncMock(return_value=[
            {"tradingsymbol": "INDUSTOWER", "quantity": 48},
        ])
        monitor_skill.ctx.market_data.get_ltp = AsyncMock(return_value=398.50)
        async def status(oid):
            if oid == "SL-XYZ":
                return {"status": "COMPLETE"}
            return {"status": "OPEN"}
        monitor_skill.ctx.broker.get_order_status = AsyncMock(side_effect=status)
        monitor_skill.ctx.broker.cancel_order = AsyncMock(return_value=True)

        await monitor_skill.execute()

        monitor_skill.ctx.broker.cancel_order.assert_awaited_once_with("TGT-XYZ")
        monitor_skill.ctx.db.set_trade_target_order_id.assert_awaited_once_with(
            "T-mis-001", None,
        )

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
        # Trailing-SL requires a broker-side SL order id to modify.
        open_position["sl_order_id"] = "SL-001"
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


class TestTrailingStepCurve:
    """The trailing-SL tighten step-up curve is shared by all three trailing
    paths (client / GTT / MIS), so a regression here loosens or over-tightens
    every managed stop. Pin the documented ramp with the default config."""

    def _cfg(self, **overrides):
        from yolovest.config import ExitTweaksConfig
        return ExitTweaksConfig(**overrides)

    def test_default_curve_matches_documented_ramp(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        cfg = self._cfg()
        mult = PositionMonitorSkill._trailing_step_multiplier
        # 50% → 100% target progress: 1.00 → 0.85 → 0.70 → 0.55 → 0.40 → 0.25 → 0.20
        assert mult(0.49, cfg) == 1.0          # below start: no tighten
        assert mult(0.50, cfg) == pytest.approx(0.85)
        assert mult(0.60, cfg) == pytest.approx(0.70)
        assert mult(0.70, cfg) == pytest.approx(0.55)
        assert mult(0.80, cfg) == pytest.approx(0.40)
        assert mult(0.90, cfg) == pytest.approx(0.25)
        assert mult(1.00, cfg) == pytest.approx(0.20)  # floored

    def test_floor_holds_beyond_target(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        cfg = self._cfg()
        assert PositionMonitorSkill._trailing_step_multiplier(2.0, cfg) == pytest.approx(0.20)

    def test_disabled_returns_full_step(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        cfg = self._cfg(tighten_trailing_enabled=False)
        assert PositionMonitorSkill._trailing_step_multiplier(0.99, cfg) == 1.0


class TestTargetProgressPct:
    def test_buy_halfway(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        assert PositionMonitorSkill._target_progress_pct("BUY", 100, 110, 105) == pytest.approx(0.5)

    def test_sell_halfway(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        assert PositionMonitorSkill._target_progress_pct("SELL", 100, 90, 95) == pytest.approx(0.5)

    def test_crossed_target_exceeds_one(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        assert PositionMonitorSkill._target_progress_pct("BUY", 100, 110, 115) == pytest.approx(1.5)

    def test_below_entry_clamps_to_zero(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        assert PositionMonitorSkill._target_progress_pct("BUY", 100, 110, 95) == 0.0

    def test_invalid_geometry_returns_zero(self):
        from yolovest.skills.position_monitor import PositionMonitorSkill
        assert PositionMonitorSkill._target_progress_pct("BUY", 100, 0, 105) == 0.0
        assert PositionMonitorSkill._target_progress_pct("BUY", 0, 110, 105) == 0.0
