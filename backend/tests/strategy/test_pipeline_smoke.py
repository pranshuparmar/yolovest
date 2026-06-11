"""End-to-end in-memory smoke test of the modified training pipeline:
cost-floored triple-barrier labels -> embargoed + regularized + early-
stopped XGBoost training -> Deflated-Sharpe-reported threshold sweep ->
production-path inference. Guards that all the moving parts compose.
"""

import math
import random

import pytest

from yolovest.costs import round_trip_cost_floor_pct
from yolovest.skills.model_retrain import ModelRetrainSkill


def _walk_bars(symbol: str, n: int, drift: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    close = 100.0
    base_ts = 1_700_000_000  # epoch secs; spacing is daily
    from datetime import datetime, timedelta
    base = datetime(2018, 1, 1)
    for i in range(n):
        close *= math.exp(rng.gauss(drift, 0.015))
        hi = close * (1 + abs(rng.gauss(0, 1)) * 0.006)
        lo = close * (1 - abs(rng.gauss(0, 1)) * 0.006)
        op = lo + (hi - lo) * rng.random()
        out.append({
            "symbol": symbol,
            "timestamp": (base + timedelta(days=i)).isoformat(),
            "open": op, "high": hi, "low": lo, "close": close,
            "volume": 500_000 + i * 13,
        })
    return out


@pytest.fixture
def skill(app_context):
    # Attach a real XGBoost model wired to the real (post-change) config.
    import tempfile

    from yolovest.strategy.ml_signal import XGBoostSignalModel
    app_context.ml = XGBoostSignalModel(
        model_dir=tempfile.mkdtemp(), config=app_context.config,
    )
    return ModelRetrainSkill(app_context)


class TestPipelineSmoke:
    async def test_train_and_infer_end_to_end(self, skill):
        # Mixed-trend universe so all three classes appear.
        drifts = [0.0015, -0.0015, 0.0008, -0.0008, 0.0, 0.0012,
                  -0.0012, 0.0005, -0.0005, 0.0009]
        bars: list[dict] = []
        for k, d in enumerate(drifts):
            bars += _walk_bars(f"SYM{k}", 360, d, seed=100 + k)
        training_data = {"bars": bars}

        # 1) Cost-floored swing labels (CNC round-trip).
        floor = round_trip_cost_floor_pct("CNC", skill.ctx.config.transaction_costs)
        X, y, feat_names, weights, bars_meta = skill._prepare_training_data(
            training_data, lookahead_bars=10,
            target_atr_mult=1.5, sl_atr_mult=0.75, cost_floor_pct=floor,
        )
        assert len(X) > 1334  # enough for the final-scale holdout path
        assert len(feat_names) > 10
        # Stored exit barrier never sits below the cost floor.
        assert all(m["target_pct"] >= floor - 1e-9 for m in bars_meta)
        # All three classes represented.
        assert {0, 1, 2} <= set(y)

        # Snapshot rows BEFORE train (train clears the input X to free RAM).
        probe = [row[:] for row in X[-40:]]

        # 2) Train through the full path: embargo + regularized xgb +
        #    early stopping + threshold sweep with Deflated Sharpe.
        metrics = await skill.ctx.ml.train(
            "swing", X, y, {
                "bars_meta": bars_meta, "lookahead_bars": 10,
                "backtest_product": "CNC", "backtest_max_positions": 5,
                # speed: lower the early-stopping floor + tree ceiling
                "n_estimators": 120, "early_stopping_rounds": 10,
                "early_stopping_min_samples": 500,
            }, feature_names=feat_names,
        )

        # 3) Metrics from the modified path are all populated.
        assert metrics["backtest_source"].startswith("walk_forward")
        assert "sharpe_lower" in metrics
        assert "deflated_sharpe" in metrics  # key present (float or None)
        assert metrics["deflated_sharpe"] is None or (
            0.0 <= metrics["deflated_sharpe"] <= 1.0
        )
        # Discrimination diagnostics are persisted (not just logged) so model
        # edge is legible from the stored record.
        for k in ("oos_auc_buy", "oos_auc_sell", "oos_logloss",
                  "oos_buy_separation"):
            assert k in metrics
        assert 0.0 <= metrics["tuned_buy_threshold"] <= 1.0
        # Early stopping kept the deployed tree count within the ceiling.
        assert skill.ctx.ml._swing_model.n_estimators <= 120
        # Tuned cutoffs are DEPLOYED iff the tuned variant beat argmax;
        # otherwise the model runs argmax (no cutoffs), so the saved headline
        # Sharpe matches live behaviour.
        deployed_thresholds = skill.ctx.ml._get_thresholds("swing") is not None
        tuning_won = metrics["backtest_source"] == "walk_forward_threshold_tuned"
        assert deployed_thresholds == tuning_won

        # 4) Production-path inference runs on real feature vectors.
        labels = skill.ctx.ml.predict_labels_batch(probe, "swing")
        assert len(labels) == len(probe)
        assert all(lbl in (0, 1, 2) for lbl in labels)
