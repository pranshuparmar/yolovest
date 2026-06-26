"""Integration tests for the model-retrain execute sequence (Milestone 0.3).

These drive `ModelRetrainSkill.execute` end-to-end against a REAL
`XGBoostSignalModel` (tiny tree counts) and synthetic daily bars, pinning
the integration seams the per-helper unit tests can't see:

  - the post-train production-path guard must actually evaluate rows
    (train() consumes the caller's X in place, which used to leave the
    guard scoring an empty matrix and swallowing the crash);
  - registry-honouring deployment: the candidate goes to the SHADOW slot
    and the registry's production model is restored to the live slots
    (first-ever train bootstraps: candidate promoted directly);
  - a retrain in which no model ships returns success=False and alerts.
"""

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import joblib
import pytest

from yolovest.skills.model_retrain import ModelRetrainSkill
from yolovest.strategy.ml_signal import XGBoostSignalModel


def _sync_ml_methods(ml: AsyncMock) -> None:
    """Mark the model's synchronous methods (clear_model / clear_shadow /
    get_shadow_version / predict_labels_batch) as sync Mocks. Production calls
    these without await, so an AsyncMock would leave unawaited coroutines and
    emit RuntimeWarnings."""
    ml.clear_model = MagicMock()
    ml.clear_shadow = MagicMock()
    ml.get_shadow_version = MagicMock(return_value=None)
    ml.predict_labels_batch = MagicMock(return_value=[0, 1, 2])


class _StubModel:
    """Minimal picklable stand-in for an incumbent production model."""

    def predict(self, X):  # noqa: N803
        return [1]


def _make_bars(symbol: str, n: int, seed: int) -> list[dict[str, Any]]:
    """Seeded random-walk daily bars with enough volatility that the
    path-aware labeler emits a mix of BUY / HOLD / SELL."""
    rng = random.Random(seed)
    d = datetime(2024, 1, 2)
    close = 100.0 * (1 + rng.random())
    out: list[dict[str, Any]] = []
    for _ in range(n):
        d += timedelta(days=1)
        if d.weekday() >= 5:  # skip weekends so dates look like sessions
            d += timedelta(days=7 - d.weekday())
        o = close * (1 + rng.gauss(0, 0.004))
        c = max(1.0, close * (1 + rng.gauss(0.0005, 0.018)))
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
        out.append({
            "symbol": symbol,
            "timestamp": d.isoformat(),
            "open": o, "high": hi, "low": lo, "close": c,
            "volume": int(1_000_000 + rng.random() * 100_000),
            "delivery_pct": None,
        })
        close = c
    return out


@pytest.fixture
def retrain_ctx(app_context, tmp_path):
    """app_context wired with a REAL XGBoost model + synthetic training
    data sized for a fast but genuine train. 12 symbols × 320 bars →
    ~1,300 samples: enough for the 200-sample floor AND for the
    cross-sectional relative label (needs ≥10 names per date)."""
    ctx = app_context
    cfg = ctx.config
    cfg.retraining.xgb.n_estimators = 20
    cfg.retraining.xgb.early_stopping_rounds = 0
    cfg.strategy.class_balance_min_pct = 0.0
    cfg.strategy.post_train_min_signal_rate = 0.0  # guard observes, never refuses
    cfg.strategy.feedback.enabled = False

    ctx.ml = XGBoostSignalModel(model_dir=str(tmp_path), db=None, config=cfg)

    bars = []
    for i in range(12):
        bars.extend(_make_bars(f"SYM{i:02d}", 320, seed=100 + i))
    ctx.db.get_training_dataset = AsyncMock(return_value={"bars": bars})
    # No 1-min path coverage → the intraday lane skips with
    # insufficient_features, exactly like a daily-data-only install.
    ctx.db.get_distinct_ohlcv_symbols = AsyncMock(return_value=[])
    ctx.db.get_production_model = AsyncMock(return_value=None)
    ctx.db.save_model_version = AsyncMock()
    ctx.db.promote_model = AsyncMock()
    return ctx, tmp_path


class TestPostTrainGuardIntegration:
    async def test_guard_evaluates_production_path_rows(self, retrain_ctx):
        """The silent-model guard must score a non-empty slice of recent
        training rows through the full production path. Regression for
        the bug where train()'s in-place X.clear() left the guard with
        zero rows and a swallowed exception ("saving anyway")."""
        ctx, _ = retrain_ctx
        result = await ModelRetrainSkill(ctx).execute()

        swing = result.data["models"]["swing"]
        # Guard output lands in metrics on success, or top-level on a
        # guard refusal — either proves the guard ran on real rows.
        container = swing.get("metrics", swing)
        assert "post_train_pred_dist" in container, (
            "post-train guard never evaluated the trained model"
        )
        assert sum(container["post_train_pred_dist"].values()) > 0
        assert "post_train_signal_rate" in container

    async def test_survivorship_caveat_stamped(self, retrain_ctx):
        ctx, _ = retrain_ctx
        result = await ModelRetrainSkill(ctx).execute()
        metrics = result.data["models"]["swing"]["metrics"]
        assert "survivor_universe" in metrics.get("data_caveats", [])


class TestRegistryHonouringDeployment:
    async def test_first_train_bootstraps_candidate_to_production(self, retrain_ctx):
        """No production row in the registry → the candidate is promoted
        directly, PROVIDED it clears the honest-edge gate (disabled here
        so the lifecycle assertion doesn't depend on the random fixture
        corpus's argmax sign — the gate itself is covered by
        TestBootstrapEdgeGate)."""
        ctx, _ = retrain_ctx
        ctx.config.retraining.min_argmax_sharpe_for_promotion = -100.0
        result = await ModelRetrainSkill(ctx).execute()

        swing = result.data["models"]["swing"]
        assert "version" in swing
        ctx.db.promote_model.assert_any_await("swing", swing["version"])
        # Candidate occupies the production slot; no shadow needed.
        assert ctx.ml._swing_version == swing["version"]
        assert not ctx.ml.has_shadow("swing")

    async def test_incumbent_restored_and_candidate_shadowed(self, retrain_ctx):
        """With a production model in the registry, the retrain must NOT
        hijack the live slots: the incumbent is restored and the fresh
        candidate goes to the shadow slot for its A/B trial."""
        ctx, tmp_path = retrain_ctx
        incumbent = "swing_v20200101_000000"
        joblib.dump(
            {"model": _StubModel(), "version": incumbent, "feature_names": ["f0"]},
            Path(tmp_path) / f"{incumbent}.pkl",
        )
        ctx.db.get_production_model = AsyncMock(
            return_value={"model_type": "swing", "version": incumbent,
                          "status": "production", "sharpe_ratio": 0.1},
        )

        result = await ModelRetrainSkill(ctx).execute()

        swing = result.data["models"]["swing"]
        assert "version" in swing
        # Production slot = incumbent, NOT the unvetted candidate.
        assert ctx.ml._swing_version == incumbent
        # Candidate starts its shadow trial immediately (no restart needed).
        assert ctx.ml.has_shadow("swing")
        assert ctx.ml.get_shadow_version("swing") == swing["version"]
        ctx.db.promote_model.assert_not_awaited()


class TestBootstrapEdgeGate:
    """'No incumbent' means the lane is PARKED — bootstrap promotion must
    still clear the honest-edge gate, or a negative-argmax candidate goes
    live the moment the user re-enables the lane via strategy.mode."""

    def _mock_ml(self, ctx, argmax: float) -> None:
        ctx.ml = AsyncMock()
        _sync_ml_methods(ctx.ml)
        ctx.ml.train = AsyncMock(return_value={
            "sharpe": 1.5, "sharpe_lower": 1.2, "argmax_sharpe": argmax,
            "win_rate": 0.5,
        })
        ctx.ml.save_model = AsyncMock(return_value="swing_vCAND")

    async def test_negative_edge_candidate_is_refused(self, retrain_ctx):
        ctx, _ = retrain_ctx
        self._mock_ml(ctx, argmax=-0.61)

        result = await ModelRetrainSkill(ctx).execute()

        ctx.db.promote_model.assert_not_awaited()
        # Candidate parked as shadow; the live slot train() filled is
        # cleared so a mode flip can't trade it.
        ctx.ml.load_shadow_model.assert_awaited_with("swing", "swing_vCAND")
        ctx.ml.clear_model.assert_called_with("swing")
        swing = result.data["models"]["swing"]
        assert "bootstrap refused" in swing["deployed_as"]

    async def test_positive_edge_candidate_bootstraps(self, retrain_ctx):
        ctx, _ = retrain_ctx
        self._mock_ml(ctx, argmax=1.2)

        result = await ModelRetrainSkill(ctx).execute()

        ctx.db.promote_model.assert_any_await("swing", "swing_vCAND")
        assert result.data["models"]["swing"]["deployed_as"] == (
            "production (bootstrap)"
        )


class TestRetrainFailureVisibility:
    async def test_all_models_failed_returns_failure_and_alerts(self, retrain_ctx):
        """When no model ships at all, the skill result must say so and
        an error alert must go out — a silently 'successful' empty
        retrain leaves a stale model trading with nobody told."""
        ctx, _ = retrain_ctx
        ctx.ml = AsyncMock()
        _sync_ml_methods(ctx.ml)
        ctx.ml.train = AsyncMock(side_effect=RuntimeError("boom"))

        result = await ModelRetrainSkill(ctx).execute()

        assert result.success is False
        assert any(
            kwargs.get("alert_type") == "errors"
            for _, kwargs in ctx.notify.send.await_args_list
        )

    async def test_partial_failure_still_succeeds(self, retrain_ctx):
        """Intraday lane short on data + swing trained fine = a normal
        daily-data-only install; the run is a success."""
        ctx, _ = retrain_ctx
        result = await ModelRetrainSkill(ctx).execute()
        assert result.success is True
        assert "version" in result.data["models"]["swing"]
        assert "error" in result.data["models"]["intraday"]
