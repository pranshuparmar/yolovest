"""Tests for risk-check skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.risk_check import RiskCheckSkill


@pytest.fixture
def risk_skill(app_context):
    # Disable conviction sizing for predictable test sizing
    app_context.config.risk.conviction_sizing.enabled = False
    skill = RiskCheckSkill(app_context)
    # Default mocks for DB methods that risk-check calls but aren't
    # the focus of individual tests. Without these the AsyncMock
    # auto-magic returns another AsyncMock, which then crashes on
    # numeric comparisons / iterations downstream.
    skill.ctx.db.minutes_since_last_loss_for_symbol = AsyncMock(return_value=1e9)
    skill.ctx.db.get_pending_trades = AsyncMock(return_value=[])
    skill.ctx.db.get_earnings_events = AsyncMock(return_value=[])
    skill.ctx.db.get_open_positions = AsyncMock(return_value=[])
    skill.ctx.db.compute_symbol_beta = AsyncMock(return_value=None)
    return skill


@pytest.fixture
def base_signal():
    return {
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "target_price": 2600.0,
        "stop_loss_price": 2450.0,
        "position_size": 10,
        "confidence_score": 0.85,
    }


@pytest.fixture
def healthy_portfolio():
    return {
        "total_capital": 100000,
        "available_cash": 80000,
        "exposure_pct": 0.20,
        "open_positions": 1,
        "stock_exposures": {},
        "sector_counts": {},
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "trades_today": 0,
        "minutes_since_last_loss": 60,
    }


class TestRiskCheckApproval:
    """Test that valid signals pass risk checks."""

    async def test_approve_valid_signal(self, risk_skill, base_signal, healthy_portfolio):
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["approved"]
        assert result.data["adjusted_size"] > 0

    async def test_position_size_computed_from_risk(
        self, risk_skill, base_signal, healthy_portfolio,
    ):
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True
        # This test verifies the max-single-stock cap, so lift the
        # per-signal pacing cap (max_pct_per_signal) out of the way —
        # otherwise it binds first at 10% < 25% and the assertion
        # would measure the wrong rule.
        risk_skill.ctx.config.risk.max_pct_per_signal = 0.5

        result = await risk_skill.execute(signal=base_signal)

        # max risk = 100000 * 0.02 = 2000
        # risk per share = |2500 - 2450| = 50
        # raw position_size = 2000 / 50 = 40
        # BUT capped by single stock exposure: int(0.25 * 100000 / 2500) = 10
        assert result.data["adjusted_size"] == 10


class TestRiskCheckAffordabilityClamp:
    """The post-multiplier affordability clamp is the last word on size.

    Conviction / regime / institutional up-multipliers each clamp only to
    max_by_exposure (derived from capital, not available cash), so a hot
    multiplier stack can grow a position past what the account can fund.
    The affordability clamp re-applies the cash/margin ceiling afterwards.
    """

    async def test_affordability_clamp_caps_multiplier_inflation(self, risk_skill):
        from yolovest.skills.risk_check import _Sizing

        risk_skill._get_slippage_penalty = AsyncMock(return_value=0.0)
        cfg = risk_skill.ctx.config.risk
        cfg.conviction_sizing.enabled = False
        cfg.institutional_flow.enabled = False
        cfg.regime_gate.enabled = True
        signal = {"symbol": "RELIANCE", "confidence_score": 0.85}
        sizing = _Sizing(
            position_size=100,
            base_position_size=100,
            max_by_exposure=1000,
            risk_per_share=0.0,  # skip the effective-risk clamp, isolate affordability
            capital=100_000,
            affordable_size=150,
        )

        size, _penalty = await risk_skill._apply_size_multipliers(
            signal, cfg, sizing,
            depth_size_multiplier=1.0, regime_size_multiplier=3.0,
        )

        # regime x3 -> 300 (<= max_by_exposure 1000), then affordability -> 150
        assert size == 150

    async def test_no_affordable_size_means_no_clamp(self, risk_skill):
        from yolovest.skills.risk_check import _Sizing

        risk_skill._get_slippage_penalty = AsyncMock(return_value=0.0)
        cfg = risk_skill.ctx.config.risk
        cfg.conviction_sizing.enabled = False
        cfg.institutional_flow.enabled = False
        cfg.regime_gate.enabled = True
        signal = {"symbol": "RELIANCE", "confidence_score": 0.85}
        sizing = _Sizing(
            position_size=100,
            base_position_size=100,
            max_by_exposure=1000,
            risk_per_share=0.0,
            capital=100_000,
            affordable_size=None,  # no margin/cash info -> clamp must not fire
        )

        size, _penalty = await risk_skill._apply_size_multipliers(
            signal, cfg, sizing,
            depth_size_multiplier=1.0, regime_size_multiplier=3.0,
        )

        # regime x3 -> 300, capped only by max_by_exposure (1000), no clamp
        assert size == 300


class TestRiskCheckRejections:
    """Test all rejection scenarios."""

    async def test_reject_kill_switch_active(self, risk_skill, base_signal, healthy_portfolio):
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.db.is_kill_switch_active = AsyncMock(return_value=True)

        result = await risk_skill.execute(signal=base_signal)

        assert result.success  # skill ran fine
        assert not result.data["approved"]
        assert "Kill switch" in result.data["rejection_reason"]

    async def test_reject_outside_order_window(self, risk_skill, base_signal, healthy_portfolio):
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: False

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "order window" in result.data["rejection_reason"].lower()

    async def test_reject_daily_loss_limit(self, risk_skill, base_signal, healthy_portfolio):
        healthy_portfolio["daily_pnl_pct"] = -0.04  # exceeds 3% limit
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "Daily loss limit" in result.data["rejection_reason"]

    async def test_reject_max_trades_per_day(self, risk_skill, base_signal, healthy_portfolio):
        healthy_portfolio["trades_today"] = 10  # at max
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "Max trades" in result.data["rejection_reason"]

    async def test_reject_loss_cooldown(self, risk_skill, base_signal, healthy_portfolio):
        healthy_portfolio["minutes_since_last_loss"] = 5  # within 15min cooldown
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "cooldown" in result.data["rejection_reason"].lower()

    async def test_reject_max_open_positions(self, risk_skill, base_signal, healthy_portfolio):
        healthy_portfolio["open_positions"] = 3  # at max
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "Max open positions" in result.data["rejection_reason"]

    async def test_reject_max_portfolio_exposure(self, risk_skill, base_signal, healthy_portfolio):
        healthy_portfolio["exposure_pct"] = 0.65  # exceeds 60%
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "exposure" in result.data["rejection_reason"].lower()

    async def test_pending_count_in_max_trades_per_day(
        self, risk_skill, base_signal, healthy_portfolio,
    ):
        """Regression: max_trades_per_day must count pending+executed.
        Previously only counted executed; a single heartbeat that emitted
        N signals (all with trades_today=0) would queue all N and the
        user could approve past the daily cap."""
        risk_skill.ctx.config.execution.transaction_mode = "manual"
        risk_skill.ctx.config.risk.max_trades_per_day = 2

        healthy_portfolio["trades_today"] = 0
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True
        # 2 pending awaiting approval already
        risk_skill.ctx.db.get_pending_trades = AsyncMock(return_value=[
            {"symbol": "A", "entry_price": 100, "position_size": 1},
            {"symbol": "B", "entry_price": 100, "position_size": 1},
        ])

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        reason = result.data["rejection_reason"].lower()
        assert "max trades/day" in reason
        assert "pending" in reason

    async def test_pending_notional_counts_toward_exposure_cap(
        self, risk_skill, base_signal, healthy_portfolio,
    ):
        """In manual mode, queued pending trades must count toward the
        portfolio exposure cap. Otherwise the cap is leaky: open=0, pending
        could be 100% of capital, and the gate still passes."""
        # Manual mode is what triggers pending fetch
        risk_skill.ctx.config.execution.transaction_mode = "manual"
        # Lift the position-count cap so this test isolates the
        # *exposure* cap behaviour. Without this the test trips
        # max_open_positions first (1 system + 3 pending > default 3).
        risk_skill.ctx.config.risk.max_open_positions = 20
        risk_skill.ctx.config.risk.max_trades_per_day = 20

        # Open positions only at 10% — by themselves not over the 60% cap
        healthy_portfolio["exposure_pct"] = 0.10
        healthy_portfolio["total_capital"] = 100_000
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        # Pending notional adds another ~55% (3 × ~18.3k) — total ~65% > 60% cap
        risk_skill.ctx.db.get_pending_trades = AsyncMock(return_value=[
            {"symbol": "A", "entry_price": 1830.0, "position_size": 10},
            {"symbol": "B", "entry_price": 1830.0, "position_size": 10},
            {"symbol": "C", "entry_price": 1830.0, "position_size": 10},
        ])

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        reason = result.data["rejection_reason"].lower()
        assert "exposure" in reason
        # The new message should call out the open vs pending split
        assert "pending" in reason

    async def test_pending_notional_ignored_in_auto_mode(
        self, risk_skill, base_signal, healthy_portfolio,
    ):
        """Auto mode doesn't queue pending trades — exposure check should
        only consider open positions in that path."""
        risk_skill.ctx.config.execution.transaction_mode = "auto"
        healthy_portfolio["exposure_pct"] = 0.10
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True
        # Even if pending exists (shouldn't in auto, but defensive):
        risk_skill.ctx.db.get_pending_trades = AsyncMock(return_value=[
            {"symbol": "A", "entry_price": 1830.0, "position_size": 100},
        ])

        result = await risk_skill.execute(signal=base_signal)

        # 10% open + 0 pending (auto mode skips pending fetch) -> approved
        assert result.data["approved"]

    async def test_reject_single_stock_exposure(self, risk_skill, base_signal, healthy_portfolio):
        healthy_portfolio["stock_exposures"] = {"RELIANCE": 0.30}  # exceeds 25%
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "Single stock" in result.data["rejection_reason"]

    async def test_reject_sector_correlation(self, risk_skill, base_signal, healthy_portfolio):
        healthy_portfolio["sector_counts"] = {"Energy": 1}  # max_same_sector_positions=1
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.db.get_stock_sector = AsyncMock(return_value="Energy")
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=base_signal)

        assert not result.data["approved"]
        assert "Sector" in result.data["rejection_reason"]

    async def test_reject_no_stop_loss(self, risk_skill, healthy_portfolio):
        signal = {
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "target_price": 2600.0,
            "position_size": 10,
        }
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=signal)

        assert not result.data["approved"]
        assert "stop-loss" in result.data["rejection_reason"].lower()

    async def test_reject_invalid_stop_loss(self, risk_skill, healthy_portfolio):
        signal = {
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "target_price": 2600.0,
            "stop_loss_price": 2500.0,  # same as entry
            "position_size": 10,
        }
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True

        result = await risk_skill.execute(signal=signal)

        assert not result.data["approved"]
        assert "risk_per_share" in result.data["rejection_reason"]


class TestRiskCheckWeeklyBreaker:
    """Test weekly circuit breaker sizing reduction."""

    async def test_weekly_breaker_reduces_size(self, risk_skill, healthy_portfolio):
        # Use a low-priced stock with tight SL so single stock cap doesn't bind
        signal = {
            "symbol": "IDEA",
            "signal_type": "BUY",
            "entry_price": 10.0,
            "target_price": 12.0,
            "stop_loss_price": 9.0,
            "position_size": 100,
            "confidence_score": 0.85,
        }
        healthy_portfolio["weekly_pnl_pct"] = -0.06  # exceeds 5% weekly limit
        risk_skill.ctx.db.get_portfolio_state = AsyncMock(return_value=healthy_portfolio)
        risk_skill.ctx.market_hours.is_order_window = lambda: True
        risk_skill.ctx.market_data.get_ltp = AsyncMock(return_value=10.0)

        result = await risk_skill.execute(signal=signal)

        assert result.data["approved"]
        assert result.data["weekly_breaker_active"]
        # max risk = 100000 * 0.02 = 2000
        # risk per share = |10 - 9| = 1
        # raw position_size = 2000 / 1 = 2000
        # weekly reduction: int(2000 * 0.5) = 1000
        # cap by exposure: int(0.25 * 100000 / 10) = 2500
        # min(1000, 2500) = 1000
        assert result.data["adjusted_size"] == 1000
