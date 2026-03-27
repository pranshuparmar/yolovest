"""Tests for risk-check skill (Phase 3, FR-5.1 to FR-5.18)."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.risk_check import RiskCheckSkill


@pytest.fixture
def risk_skill(app_context):
    return RiskCheckSkill(app_context)


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

        result = await risk_skill.execute(signal=base_signal)

        # max risk = 100000 * 0.02 = 2000
        # risk per share = |2500 - 2450| = 50
        # raw position_size = 2000 / 50 = 40
        # BUT capped by single stock exposure: int(0.25 * 100000 / 2500) = 10
        assert result.data["adjusted_size"] == 10


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
