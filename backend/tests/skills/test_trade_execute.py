"""Tests for trade-execute skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.trade_execute import TradeExecuteSkill


@pytest.fixture
def trade_skill(app_context):
    skill = TradeExecuteSkill(app_context)
    # _find_matching_recent_order calls asyncio.to_thread on
    # broker._kite.orders. The default AsyncMock for _kite doesn't
    # behave like a sync object, so the to_thread call returns a
    # coroutine that crashes iteration. Pin _kite to None — the
    # try/except in the reconciler handles missing kite cleanly
    # by returning None (= no matching order, treat as new attempt).
    skill.ctx.broker._kite = None
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
            return_value={"status": "filled", "average_price": 2501.0, "filled_quantity": 10}
        )

        result = await trade_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["mode"] == "live"
        # MIS OCO: entry + SL + resting target LIMIT.
        # The target leg was added when broker-side MIS OCO replaced
        # the client-side-only target detection — see CLAUDE.md
        # "Broker-side MIS OCO" for the rationale.
        assert trade_skill.ctx.broker.place_order.await_count == 3

    async def test_live_slippage_tracked(self, trade_skill, base_signal):
        trade_skill.ctx.config.mode = "live"
        trade_skill.ctx.broker.get_order_status = AsyncMock(
            return_value={"status": "filled", "average_price": 2503.0, "filled_quantity": 10}
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
            return_value={"status": "filled", "average_price": 2500.0, "filled_quantity": 10}
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


class TestLivePreconditions:
    """The dedup and price-drift early exits must return (SkillResult,
    order_price) tuples — a bare SkillResult would crash the caller's
    unpack the moment either path fires (regression: caught by mypy
    during the _execute_live decomposition)."""

    async def test_duplicate_signal_skips_with_tuple_return(
        self, app_context, sample_config,
    ):
        from unittest.mock import AsyncMock

        app_context.config.mode = "live"
        app_context.broker._mode = "live"
        memory = AsyncMock()
        memory.get = AsyncMock(return_value="in_flight")
        app_context.memory = memory

        skill = TradeExecuteSkill(app_context)
        signal = {
            "symbol": "RELIANCE", "signal_type": "BUY", "entry_price": 2500.0,
            "stop_loss_price": 2450.0, "target_price": 2600.0,
            "position_size": 10, "product": "MIS",
        }
        result = await skill._execute_live(signal)
        assert result.success
        assert result.data["skipped"] is True
        assert result.data["reason"] == "duplicate_signal"
        app_context.broker.place_order.assert_not_called()

    async def test_price_drift_rejects_with_tuple_return(
        self, app_context,
    ):
        from unittest.mock import AsyncMock

        app_context.config.mode = "live"
        app_context.broker._mode = "live"
        app_context.memory = None
        # LTP 5% away from the signal entry — beyond price_drift_max_pct.
        app_context.market_data.get_ltp = AsyncMock(return_value=2625.0)

        skill = TradeExecuteSkill(app_context)
        signal = {
            "symbol": "RELIANCE", "signal_type": "BUY", "entry_price": 2500.0,
            "stop_loss_price": 2450.0, "target_price": 2600.0,
            "position_size": 10, "product": "MIS",
        }
        result = await skill._execute_live(signal)
        assert result.success
        assert result.data["rejected"] is True
        assert result.data["reason"].startswith("price_drift")
        app_context.broker.place_order.assert_not_called()
