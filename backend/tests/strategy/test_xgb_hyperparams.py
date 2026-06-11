"""XGBoost hyperparameter resolution + early stopping.

Precedence: explicit train `params` > config.retraining.xgb > literal
default. Early stopping probes the tree count on a purged validation tail
then refits the deployed model on all data at that count.
"""

import random
from types import SimpleNamespace

import pytest

from yolovest.config import XGBoostConfig
from yolovest.strategy.ml_signal import XGBoostSignalModel


def _separable(n: int, n_feat: int = 6):
    """Cleanly separable 3-class data so a tree count is easy to learn and
    early stopping plateaus quickly."""
    random.seed(11)
    X, y = [], []
    for _ in range(n):
        feats = [random.gauss(0, 1) for _ in range(n_feat)]
        s = feats[0]
        y.append(2 if s > 0.5 else 0 if s < -0.5 else 1)
        X.append(feats)
    return X, y


def _cfg(xgb: XGBoostConfig) -> SimpleNamespace:
    return SimpleNamespace(
        retraining=SimpleNamespace(xgb=xgb, cv_embargo_frac=0.0),
        risk=SimpleNamespace(
            tuned_threshold_max_value=0.60, tuned_threshold_max_diff=0.05,
            buy_threshold_override=None, sell_threshold_override=None,
            tuned_min_signal_rate=0.0,
        ),
    )


class TestHyperparamResolution:
    async def test_config_n_estimators_honored_when_es_off(self, tmp_path):
        cfg = _cfg(XGBoostConfig(n_estimators=70, early_stopping_rounds=0))
        sm = XGBoostSignalModel(model_dir=str(tmp_path), config=cfg)
        X, y = _separable(400)
        await sm.train("intraday", X, y, {})
        assert sm._intraday_model.n_estimators == 70
        # Regularization knobs flowed through too.
        assert sm._intraday_model.subsample == pytest.approx(0.8)
        assert sm._intraday_model.min_child_weight == pytest.approx(5.0)

    async def test_params_override_beats_config(self, tmp_path):
        cfg = _cfg(XGBoostConfig(n_estimators=300, early_stopping_rounds=0))
        sm = XGBoostSignalModel(model_dir=str(tmp_path), config=cfg)
        X, y = _separable(400)
        await sm.train("intraday", X, y, {"n_estimators": 33})
        assert sm._intraday_model.n_estimators == 33


class TestEarlyStopping:
    async def test_early_stopping_reduces_tree_count(self, tmp_path):
        # High ceiling so the validation-logloss plateau (the data has an
        # irreducible HOLD band) is what stops boosting, not the cap.
        cfg = _cfg(XGBoostConfig(
            n_estimators=1500, early_stopping_rounds=10,
            early_stopping_min_samples=400,
        ))
        sm = XGBoostSignalModel(model_dir=str(tmp_path), config=cfg)
        X, y = _separable(800)
        await sm.train("intraday", X, y, {})
        # Early stopping picked a data-driven count strictly below the
        # 1500-tree ceiling.
        assert 50 <= sm._intraday_model.n_estimators < 1500

    async def test_small_corpus_skips_early_stopping(self, tmp_path):
        cfg = _cfg(XGBoostConfig(
            n_estimators=60, early_stopping_rounds=10,
            early_stopping_min_samples=5000,  # above our sample count
        ))
        sm = XGBoostSignalModel(model_dir=str(tmp_path), config=cfg)
        X, y = _separable(400)
        await sm.train("intraday", X, y, {})
        # Below the min-samples floor → no probe, configured count kept.
        assert sm._intraday_model.n_estimators == 60
