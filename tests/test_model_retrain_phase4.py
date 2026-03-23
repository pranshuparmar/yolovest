"""Tests for model-retrain shadow promotion (Phase 4, FR-7.5)."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.model_retrain import ModelRetrainSkill


@pytest.fixture
def retrain_skill(app_context):
    app_context.ml = AsyncMock()
    app_context.ml.train = AsyncMock(return_value={"sharpe_ratio": 1.5, "win_rate": 0.6})
    app_context.ml.save_model = AsyncMock(return_value="v2.0")
    app_context.ml.deploy_shadow = AsyncMock()
    app_context.ml.load_model = AsyncMock()
    return ModelRetrainSkill(app_context)


class TestShadowPromotion:
    async def test_promote_better_shadow(self, retrain_skill):
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[
            {
                "model_type": "intraday",
                "version": "v2.0",
                "sharpe_ratio": 1.8,
                "status": "shadow",
            }
        ])
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value={
            "sharpe_ratio": 1.2,
            "version": "v1.0",
        })

        promotions = await retrain_skill._check_shadow_promotions()

        assert len(promotions) == 1
        assert promotions[0]["action"] == "promoted"
        assert promotions[0]["version"] == "v2.0"
        retrain_skill.ctx.db.promote_model.assert_awaited_with("intraday", "v2.0")
        retrain_skill.ctx.ml.load_model.assert_awaited_with("intraday", "v2.0")

    async def test_retire_worse_shadow(self, retrain_skill):
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[
            {
                "model_type": "swing",
                "version": "v2.0",
                "sharpe_ratio": 0.8,
                "status": "shadow",
            }
        ])
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value={
            "sharpe_ratio": 1.5,
            "version": "v1.0",
        })

        promotions = await retrain_skill._check_shadow_promotions()

        assert len(promotions) == 1
        assert promotions[0]["action"] == "retired"
        retrain_skill.ctx.db.retire_model.assert_awaited_with("swing", "v2.0")

    async def test_promote_when_no_production(self, retrain_skill):
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[
            {
                "model_type": "intraday",
                "version": "v1.0",
                "sharpe_ratio": 1.0,
                "status": "shadow",
            }
        ])
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value=None)

        promotions = await retrain_skill._check_shadow_promotions()

        assert len(promotions) == 1
        assert promotions[0]["action"] == "promoted"

    async def test_no_shadow_models_ready(self, retrain_skill):
        promotions = await retrain_skill._check_shadow_promotions()
        assert promotions == []


class TestFullRetrain:
    async def test_retrain_with_insufficient_data(self, retrain_skill):
        retrain_skill.ctx.db.get_training_dataset = AsyncMock(
            return_value={"bars": [{"close": 100}] * 50}
        )
        retrain_skill.ctx.market_hours.is_market_hours = lambda: False

        result = await retrain_skill.execute()

        assert result.success
        assert result.data["reason"] == "insufficient_data"

    async def test_retrain_no_ml_provider(self, retrain_skill):
        retrain_skill.ctx.ml = None

        result = await retrain_skill.execute()

        assert result.success
        assert result.data["reason"] == "no_ml_provider"

    async def test_retrain_with_failure_analysis(self, retrain_skill):
        retrain_skill.ctx.db.get_training_dataset = AsyncMock(
            return_value={"bars": [{"close": 100}] * 300}
        )
        retrain_skill.ctx.db.get_prediction_outcomes = AsyncMock(return_value=[
            {"direction_correct": False, "symbol": "RELIANCE"},
            {"direction_correct": True, "symbol": "TCS"},
        ])
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value=None)
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[])
        retrain_skill.ctx.market_hours.is_market_hours = lambda: False

        result = await retrain_skill.execute()

        assert result.success
        retrain_skill.ctx.llm.analyze_prediction_failures.assert_awaited_once()
