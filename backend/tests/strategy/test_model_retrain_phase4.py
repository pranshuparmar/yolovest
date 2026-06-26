"""Tests for model-retrain shadow promotion."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from yolovest.skills.model_retrain import (
    ModelRetrainSkill,
    _decision_sharpe,
    passes_edge_gate,
)


class TestEdgeGate:
    """The honest-edge gate blocks promoting a model whose untuned
    (argmax) Sharpe is below the floor, regardless of how good its
    threshold-tuned headline number looks."""

    def test_blocks_negative_argmax_edge(self):
        ok, reason = passes_edge_gate({"argmax_sharpe": -7.0, "sharpe": 5.3}, 0.0)
        assert ok is False
        assert "argmax Sharpe -7.00" in reason

    def test_passes_positive_argmax_edge(self):
        ok, _ = passes_edge_gate({"argmax_sharpe": 0.97}, 0.0)
        assert ok is True

    def test_skips_when_argmax_absent(self):
        # Legacy / synthetic-payoff artifacts don't carry argmax_sharpe —
        # don't spuriously block them.
        ok, reason = passes_edge_gate({"sharpe": 2.0}, 0.0)
        assert ok is True
        assert "skipped" in reason

    def test_disabled_when_floor_negative(self):
        ok, _ = passes_edge_gate({"argmax_sharpe": -50.0}, -1.0)
        assert ok is True

    def test_honors_custom_floor(self):
        assert passes_edge_gate({"argmax_sharpe": 0.4}, 0.5)[0] is False
        assert passes_edge_gate({"argmax_sharpe": 0.6}, 0.5)[0] is True

    def test_unparseable_argmax_skips(self):
        ok, _ = passes_edge_gate({"argmax_sharpe": "n/a"}, 0.0)
        assert ok is True


class TestDecisionSharpe:
    """Deploy/promote must compare on the robust bootstrapped lower bound,
    with a fair point-vs-point fallback for legacy incumbents."""

    def test_prefers_lower_bound_when_both_present(self):
        # Candidate looks better on point (8 vs 5) but worse on the robust
        # lower bound (3 vs 4) — the lower bound must be what's compared.
        assert _decision_sharpe(
            {"sharpe": 8.0, "sharpe_lower": 3.0},
            {"sharpe_ratio": 5.0, "sharpe_lower": 4.0},
        ) == (3.0, 4.0)

    def test_falls_back_to_point_for_legacy_incumbent(self):
        # Incumbent predates sharpe_lower → compare point-vs-point so the
        # candidate's conservative lower bound isn't pitted against the
        # incumbent's optimistic point.
        assert _decision_sharpe(
            {"sharpe": 5.0, "sharpe_lower": 3.0},
            {"sharpe_ratio": 7.0},
        ) == (5.0, 7.0)

    def test_no_incumbent_returns_zero_baseline(self):
        assert _decision_sharpe({"sharpe": 5.0, "sharpe_lower": 3.0}, None) == (5.0, 0.0)


def _make_bars(n: int, symbol: str = "RELIANCE") -> list[dict]:
    """Generate n realistic OHLCV bar dicts for testing."""
    base = datetime(2025, 1, 1)
    bars = []
    close = 100.0
    for i in range(n):
        # Add small variation so labels aren't all HOLD
        close = close * (1 + (0.01 if i % 3 == 0 else -0.008 if i % 3 == 1 else 0.002))
        bars.append({
            "symbol": symbol,
            "timestamp": (base + timedelta(days=i)).isoformat(),
            "open": close * 0.998,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1_000_000 + i * 100,
        })
    return bars


@pytest.fixture
def retrain_skill(app_context):
    app_context.ml = AsyncMock()
    app_context.ml.train = AsyncMock(return_value={"sharpe_ratio": 1.5, "win_rate": 0.6})
    app_context.ml.save_model = AsyncMock(return_value="v2.0")
    app_context.ml.deploy_shadow = AsyncMock()
    app_context.ml.load_model = AsyncMock()
    # Synchronous model methods — keep them sync Mocks so production calls that
    # don't await them (e.g. the shadow-slot hygiene get_shadow_version /
    # clear_shadow) don't leave unawaited coroutines.
    app_context.ml.get_shadow_version = MagicMock(return_value=None)
    app_context.ml.clear_shadow = MagicMock()
    app_context.ml.clear_model = MagicMock()
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

    async def test_promotion_judged_on_lower_bound_not_point(self, retrain_skill):
        # Shadow wins on point Sharpe (9 > 5) but loses on the robust
        # lower bound (2 < 4) → must be retired, proving the decision
        # keys off sharpe_lower, not the inflatable point estimate.
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[
            {
                "model_type": "intraday", "version": "v2.0",
                "sharpe_ratio": 9.0, "sharpe_lower": 2.0, "status": "shadow",
            }
        ])
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value={
            "version": "v1.0", "sharpe_ratio": 5.0, "sharpe_lower": 4.0,
        })
        retrain_skill.ctx.db.get_live_metrics_for_model = AsyncMock(
            return_value={"scored": 0}
        )

        promotions = await retrain_skill._check_shadow_promotions()

        assert promotions[0]["action"] == "retired"
        retrain_skill.ctx.db.promote_model.assert_not_awaited()

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

    async def test_negative_edge_shadow_retired_despite_winning_sharpe(
        self, retrain_skill,
    ):
        # Shadow beats production on tuned Sharpe and has no live data to
        # gate on — but its argmax edge is negative, so the honest-edge
        # gate must retire it rather than promote a net-losing model.
        # This is the exact case that put a -7 argmax model live.
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[
            {
                "model_type": "intraday", "version": "v2.0",
                "sharpe_ratio": 5.3, "sharpe_lower": 4.7,
                "argmax_sharpe": -7.0, "status": "shadow",
            }
        ])
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value=None)
        retrain_skill.ctx.db.get_live_metrics_for_model = AsyncMock(
            return_value={"scored": 0}
        )

        promotions = await retrain_skill._check_shadow_promotions()

        assert promotions[0]["action"] == "retired"
        assert any("edge:" in p for p in promotions[0]["failed_gates"])
        retrain_skill.ctx.db.promote_model.assert_not_awaited()
        retrain_skill.ctx.db.retire_model.assert_awaited_with("intraday", "v2.0")

    async def test_positive_edge_shadow_promotes(self, retrain_skill):
        # Same setup but a positive argmax edge → promotion proceeds.
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[
            {
                "model_type": "swing", "version": "v2.0",
                "sharpe_ratio": 3.5, "sharpe_lower": 3.1,
                "argmax_sharpe": 0.97, "status": "shadow",
            }
        ])
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value=None)
        retrain_skill.ctx.db.get_live_metrics_for_model = AsyncMock(
            return_value={"scored": 0}
        )

        promotions = await retrain_skill._check_shadow_promotions()

        assert promotions[0]["action"] == "promoted"
        retrain_skill.ctx.db.promote_model.assert_awaited_with("swing", "v2.0")


class TestFullRetrain:
    async def test_retrain_with_insufficient_data(self, retrain_skill):
        retrain_skill.ctx.db.get_training_dataset = AsyncMock(
            return_value={"bars": _make_bars(50)}
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
            return_value={"bars": _make_bars(300)}
        )
        retrain_skill.ctx.db.get_prediction_outcomes = AsyncMock(return_value=[
            {"direction_correct": False, "symbol": "RELIANCE"},
            {"direction_correct": True, "symbol": "TCS"},
        ])
        retrain_skill.ctx.db.get_feedback_data = AsyncMock(return_value={})
        retrain_skill.ctx.db.get_production_model = AsyncMock(return_value=None)
        retrain_skill.ctx.db.get_shadow_models_ready = AsyncMock(return_value=[])
        retrain_skill.ctx.market_hours.is_market_hours = lambda: False

        result = await retrain_skill.execute()

        # Both lanes are short on data here, so no model ships and the
        # run reports failure — but the prediction-failure analysis must
        # still run on the accumulated outcomes.
        assert result.success is False
        retrain_skill.ctx.llm.analyze_prediction_failures.assert_awaited_once()
