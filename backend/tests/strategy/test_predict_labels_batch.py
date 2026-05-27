"""Tests for XGBoostSignalModel.predict_labels_batch — the production-path
batch predictor (calibration-agreement + tuned-threshold gate) the
post-train guard uses to verify a model isn't silent at its thresholds."""

import numpy as np

from yolovest.strategy.ml_signal import XGBoostSignalModel


class _FakeModel:
    """Returns canned class probabilities [P(SELL), P(HOLD), P(BUY)] per row,
    ignoring the inputs."""

    def __init__(self, probas):
        self._probas = np.array(probas, dtype=float)

    def predict_proba(self, X):  # noqa: N803
        return self._probas


def _model(tmp_path, probas, thresholds, calibrator=None):
    m = XGBoostSignalModel(model_dir=str(tmp_path / "m"))
    m._swing_model = _FakeModel(probas)
    m._swing_thresholds = thresholds
    m._swing_calibrator = calibrator
    m._config = None  # → default caps (max_diff 0.05, max_value 0.60), no overrides
    return m


class TestPredictLabelsBatch:
    def test_threshold_gate_decides_labels(self, tmp_path):
        # buy=0.55 / sell=0.60 effective gate.
        probas = [
            [0.05, 0.15, 0.80],  # BUY 0.80 >= 0.55 and >= SELL → BUY (2)
            [0.10, 0.40, 0.50],  # BUY 0.50 < 0.55, SELL 0.10 < 0.60 → HOLD (1)
            [0.65, 0.30, 0.05],  # SELL 0.65 >= 0.60 and > BUY → SELL (0)
            [0.30, 0.45, 0.25],  # neither crosses → HOLD (1)
        ]
        m = _model(tmp_path, probas, {"buy": 0.55, "sell": 0.60})
        assert m.predict_labels_batch([[0]] * 4, "swing") == [2, 1, 0, 1]

    def test_silent_model_all_hold(self, tmp_path):
        # All rows below the gate → all HOLD (the silent-model case the
        # post-train guard must catch via signal_rate == 0).
        probas = [[0.30, 0.45, 0.25]] * 5
        m = _model(tmp_path, probas, {"buy": 0.55, "sell": 0.60})
        labels = m.predict_labels_batch([[0]] * 5, "swing")
        assert labels == [1, 1, 1, 1, 1]
        assert sum(1 for x in labels if x != 1) == 0  # zero signal rate

    def test_no_thresholds_falls_back_to_argmax(self, tmp_path):
        probas = [
            [0.10, 0.20, 0.70],  # argmax BUY
            [0.60, 0.30, 0.10],  # argmax SELL
        ]
        m = _model(tmp_path, probas, None)
        assert m.predict_labels_batch([[0]] * 2, "swing") == [2, 0]

    def test_calibration_adopted_only_when_agrees_and_more_confident(self, tmp_path):
        # Raw says BUY @0.58 (clears 0.55). Calibrator AGREES (BUY) and is
        # MORE confident (0.62) → adopt calibrated; still BUY.
        raw = _FakeModel([[0.04, 0.38, 0.58]])
        cal = _FakeModel([[0.03, 0.35, 0.62]])
        m = XGBoostSignalModel(model_dir=str(tmp_path / "m2"))
        m._swing_model = raw
        m._swing_calibrator = cal
        m._swing_thresholds = {"buy": 0.55, "sell": 0.60}
        m._config = None
        assert m.predict_labels_batch([[0]], "swing") == [2]

    def test_calibration_ignored_when_it_compresses(self, tmp_path):
        # Raw BUY @0.58 (clears 0.55); calibrator agrees on BUY but is LESS
        # confident (0.50) → keep raw (0.58) → still BUY. (If calibrated
        # 0.50 were used it'd drop below 0.55 → HOLD; the rule prevents that.)
        raw = _FakeModel([[0.04, 0.38, 0.58]])
        cal = _FakeModel([[0.10, 0.40, 0.50]])
        m = XGBoostSignalModel(model_dir=str(tmp_path / "m3"))
        m._swing_model = raw
        m._swing_calibrator = cal
        m._swing_thresholds = {"buy": 0.55, "sell": 0.60}
        m._config = None
        assert m.predict_labels_batch([[0]], "swing") == [2]
