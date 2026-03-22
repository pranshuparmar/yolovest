"""Tests for llm-review skill (Phase 3, FR-4.4, FR-5.11, FR-5.12)."""

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
            return_value={"sentiment": "bullish", "confidence": 0.8}
        )
        llm_skill.ctx.db.get_latest_premarket = AsyncMock(
            return_value={"market_bias": "bullish"}
        )

        context = await llm_skill._build_review_context(base_signal)

        assert context["signal"] == base_signal
        assert context["sentiment"]["sentiment"] == "bullish"
        assert context["premarket"]["market_bias"] == "bullish"
