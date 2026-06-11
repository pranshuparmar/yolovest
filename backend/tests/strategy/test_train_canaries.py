"""Leakage canaries + determinism golden run for ml_signal.train
(Milestone 0.1 / 0.2).

Three properties any honest training pipeline must hold, checked on a
synthetic panel large enough to trigger the final-scale-holdout path
(the production code path for real corpora):

  1. NULL canary — labels carrying no signal must train to ~0.5 OOS AUC.
     If this fails, future information is leaking across the holdout
     boundary (purge/embargo regression, shuffled split, etc.).
  2. PLANTED canary — a deliberately injected feature→label relationship
     must be detected (high OOS AUC). Proves the null canary isn't
     passing because the diagnostics are dead.
  3. DETERMINISM — identical inputs reproduce identical metrics
     (random_state=42 everywhere, seeded bootstrap, single-thread hist).
     This is the lightweight golden run: with pinned library versions a
     metrics drift means the pipeline changed, not the data.
"""

import random
from datetime import date, timedelta

from yolovest.strategy.ml_signal import XGBoostSignalModel

_TRAIN_PARAMS = {
    "n_estimators": 30,
    "max_depth": 3,
    "lookahead_bars": 1,
}


def _panel(n: int = 1500, planted: bool = True, seed: int = 7):
    """Synthetic cross-sectional panel: ~6 samples/day across 25 symbols.

    planted=True → label is a deterministic function of feature 0 and the
    per-sample payoff (bars_meta exit) is consistent with the label, so a
    real edge exists. planted=False → labels are random noise.
    """
    rng = random.Random(seed)
    X: list[list[float]] = []
    y: list[int] = []
    meta: list[dict] = []
    d0 = date(2022, 1, 3)
    for i in range(n):
        f0 = rng.gauss(0, 1)
        feats = [f0] + [rng.gauss(0, 1) for _ in range(5)]
        entry = 100.0 + rng.random() * 5.0
        day = d0 + timedelta(days=i // 6)
        if planted:
            label = 2 if f0 > 0.8 else 0 if f0 < -0.8 else 1
        else:
            label = rng.choice([0, 1, 2])
        tgt_pct, sl_pct = 0.02, 0.01
        if label == 2:
            exit_price = entry * (1 + tgt_pct)
        elif label == 0:
            exit_price = entry * (1 - tgt_pct)
        else:
            exit_price = entry * (1 + rng.gauss(0, 0.002))
        X.append(feats)
        y.append(label)
        meta.append({
            "symbol": f"S{i % 25}",
            "entry_close": entry,
            "exit_close": exit_price,
            "path_highs": [],  # no path → backtest exits at exit_close
            "path_lows": [],
            "target_pct": tgt_pct,
            "sl_pct": sl_pct,
            "entry_date": day.isoformat(),
        })
    return X, y, meta


async def _train(tmp_path, sub: str, planted: bool, seed: int = 7):
    X, y, meta = _panel(planted=planted, seed=seed)
    ml = XGBoostSignalModel(model_dir=str(tmp_path / sub))
    return await ml.train(
        "swing", X, y, {**_TRAIN_PARAMS, "bars_meta": meta},
        feature_names=[f"f{i}" for i in range(6)],
    )


class TestLeakageCanaries:
    async def test_null_labels_show_no_oos_discrimination(self, tmp_path):
        metrics = await _train(tmp_path, "null", planted=False, seed=11)
        auc = metrics.get("oos_auc_buy")
        assert auc is not None, "holdout diagnostics did not run"
        # Signal-free labels must score ~coin-flip out of sample. A value
        # far above 0.5 means the holdout saw training-period information.
        assert 0.38 <= auc <= 0.62, f"leakage suspected: null-label OOS AUC={auc}"

    async def test_planted_signal_is_detected_oos(self, tmp_path):
        metrics = await _train(tmp_path, "planted", planted=True, seed=7)
        auc = metrics.get("oos_auc_buy")
        assert auc is not None, "holdout diagnostics did not run"
        # Sensitivity check: proves the null canary passes because there
        # is no leak, not because the diagnostics are broken.
        assert auc > 0.90, f"planted signal not detected: OOS AUC={auc}"


class TestGoldenDeterminism:
    async def test_training_is_deterministic(self, tmp_path):
        r1 = await _train(tmp_path, "a", planted=True, seed=7)
        r2 = await _train(tmp_path, "b", planted=True, seed=7)
        for key in (
            "sharpe", "sharpe_lower", "argmax_sharpe", "tuned_sharpe",
            "tuned_buy_threshold", "tuned_sell_threshold",
            "win_rate", "total_trades", "oos_auc_buy", "oos_logloss",
        ):
            assert r1.get(key) == r2.get(key), (
                f"non-deterministic metric {key}: {r1.get(key)} != {r2.get(key)}"
            )
