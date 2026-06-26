"""Tests for llm-review skill."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from yolovest.skills.llm_review import LLMReviewSkill


@pytest.fixture
def llm_skill(app_context):
    return LLMReviewSkill(app_context)


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
        "expected_holding_period": "intraday",
        "model_version": "v1",
        "features_snapshot": {},
    }


class TestLLMReviewApprove:
    async def test_approve_signal(self, llm_skill, base_signal):
        review = MagicMock()
        review.decision = "APPROVE"
        review.reasoning = "Strong momentum"
        review.adjusted_size = None
        llm_skill.ctx.llm.review_trade = AsyncMock(return_value=review)

        result = await llm_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["approved"]
        assert result.data["llm_reasoning"] == "Strong momentum"
        llm_skill.ctx.db.log_llm_review.assert_awaited_once()

    async def test_resize_signal(self, llm_skill, base_signal):
        review = MagicMock()
        review.decision = "RESIZE"
        review.reasoning = "Reduce position due to volatility"
        review.adjusted_size = 5
        llm_skill.ctx.llm.review_trade = AsyncMock(return_value=review)

        result = await llm_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["approved"]
        assert result.data["resized"]
        assert result.data["adjusted_size"] == 5
        assert result.data["signal"]["position_size"] == 5

    async def test_resize_up_is_clamped_to_risk_checked_size(self, llm_skill, base_signal):
        """The LLM may only TRIM size, never inflate it. risk-check already
        sized the position against every cap (risk budget, single-stock
        exposure, margin) before llm-review runs, and the review prompt
        embeds externally-scraped news sentiment — a prompt-injection
        surface. An up-resize must be clamped to the risk-checked size."""
        review = MagicMock()
        review.decision = "RESIZE"
        review.reasoning = "High conviction — size up"
        review.adjusted_size = 50  # > risk-checked size of 10
        llm_skill.ctx.llm.review_trade = AsyncMock(return_value=review)

        result = await llm_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["approved"]
        # Clamped down to the risk-checked size (10), never the LLM's 50.
        assert result.data["adjusted_size"] == 10
        assert result.data["signal"]["position_size"] == 10

    async def test_resize_equal_to_risk_checked_size_is_unchanged(
        self, llm_skill, base_signal,
    ):
        """A resize exactly at the risk-checked size is not treated as an
        up-resize and passes through untouched."""
        review = MagicMock()
        review.decision = "RESIZE"
        review.reasoning = "Hold size"
        review.adjusted_size = 10  # == risk-checked size
        llm_skill.ctx.llm.review_trade = AsyncMock(return_value=review)

        result = await llm_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["adjusted_size"] == 10
        assert result.data["signal"]["position_size"] == 10


class TestLLMReviewReject:
    async def test_reject_signal(self, llm_skill, base_signal):
        review = MagicMock()
        review.decision = "REJECT"
        review.reasoning = "Market conditions unfavorable"
        review.adjusted_size = None
        llm_skill.ctx.llm.review_trade = AsyncMock(return_value=review)

        result = await llm_skill.execute(signal=base_signal)

        assert result.success
        assert not result.data["approved"]
        assert "unfavorable" in result.data["llm_reasoning"]


class TestLLMReviewFallback:
    async def test_fallback_to_rules_on_llm_error(self, llm_skill, base_signal):
        llm_skill.ctx.llm.review_trade = AsyncMock(side_effect=Exception("API down"))

        result = await llm_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["approved"]
        assert result.data["auto_approved"]

    async def test_raises_when_no_fallback(self, llm_skill, base_signal):
        llm_skill.ctx.config.risk.llm_fallback_to_rules = False
        llm_skill.ctx.llm.review_trade = AsyncMock(side_effect=Exception("API down"))

        with pytest.raises(Exception, match="API down"):
            await llm_skill.execute(signal=base_signal)

    async def test_auto_approve_when_disabled(self, llm_skill, base_signal):
        llm_skill.ctx.config.risk.llm_review_enabled = False

        result = await llm_skill.execute(signal=base_signal)

        assert result.success
        assert result.data["approved"]
        assert result.data["auto_approved"]


class TestLLMReviewContext:
    async def test_build_review_context(self, llm_skill, base_signal):
        llm_skill.ctx.db.get_latest_sentiment = AsyncMock(
            return_value={
                "symbol": "RELIANCE",
                "sentiment": "bullish",
                "confidence": 0.8,
                "key_drivers": ["earnings beat"],
            }
        )
        llm_skill.ctx.db.get_latest_premarket = AsyncMock(
            return_value={
                "gift_nifty_change_pct": 0.5,
                "us_sp500_change_pct": 0.3,
                "market_bias": "bullish",
            }
        )
        llm_skill.ctx.db.get_portfolio_state = AsyncMock(
            return_value={
                "total_capital": 100000,
                "available_cash": 80000,
                "exposure_pct": 0.2,
                "open_positions": 1,
                "stock_exposures": {},
                "sector_counts": {},
                "daily_pnl_pct": 0.0,
                "weekly_pnl_pct": 0.0,
                "trades_today": 0,
                "minutes_since_last_loss": 60,
            }
        )
        llm_skill.ctx.db.get_sector_rotation = AsyncMock(return_value={})
        llm_skill.ctx.db.get_todays_trades = AsyncMock(return_value=[])

        context = await llm_skill._build_review_context(base_signal)

        # Now returns a TradeContext model, not a dict
        assert context.signal.symbol == "RELIANCE"
        assert context.sentiment.sentiment == "bullish"
        assert context.premarket.market_bias == "bullish"
        assert context.portfolio.total_capital == 100000
