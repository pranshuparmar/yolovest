"""Direct tests for XGBoostSignalModel._predict — the inference-assembly seam.

The decision *policies* (calibrated-vs-raw choice, threshold gating, purged CV)
are covered in test_m3_policies. This file covers the end-to-end assembly that
test_m3_policies and the skill tests don't: booster probabilities ->
label -> signal_type -> entry/target/SL geometry -> MLPrediction, plus the
no-model and no-price error paths. A stubbed booster keeps it fast and
deterministic (no training).
"""

import numpy as np
import pytest

from yolovest.strategy.ml_signal import XGBoostSignalModel


class _FakeBooster:
    """Minimal stand-in for the xgboost classifier: fixed class probabilities,
    argmax label. Ignores X (the assembly under test doesn't depend on it)."""

    def __init__(self, probas: list[float]) -> None:
        self._probas = probas

    def predict(self, X: object) -> object:  # noqa: N803
        return np.array([int(np.argmax(self._probas))])

    def predict_proba(self, X: object) -> object:  # noqa: N803
        return np.array([self._probas])


@pytest.fixture
def model(tmp_path):
    m = XGBoostSignalModel(model_dir=str(tmp_path))
    # Attribution needs a real fitted booster; it's not what these tests
    # exercise, so stub it to None (the to_thread call passes 4 positionals).
    m._compute_attribution = lambda *a, **k: None
    return m


def _load(model, model_type, probas, version="test-v1"):
    model._set_model(model_type, _FakeBooster(probas))
    model._set_version(model_type, version)


class TestPredictAssembly:
    async def test_buy_geometry_and_probabilities(self, model):
        _load(model, "swing", [0.1, 0.2, 0.7])  # argmax 2 -> BUY

        pred = await model.predict_swing(
            "RELIANCE", {"close": 2500.0, "atr_14": 50.0}, current_price=2500.0,
        )

        assert pred.signal_type == "BUY"
        assert pred.confidence == pytest.approx(0.7, abs=1e-4)
        assert pred.entry_price == 2500.0
        assert pred.target_price == 2600.0   # entry + 2*atr
        assert pred.stop_loss_price == 2450.0  # entry - 1*atr
        assert pred.model_version == "test-v1"
        assert pred.class_probabilities == {"SELL": 0.1, "HOLD": 0.2, "BUY": 0.7}

    async def test_sell_geometry_is_mirrored(self, model):
        _load(model, "swing", [0.7, 0.2, 0.1])  # argmax 0 -> SELL

        pred = await model.predict_swing(
            "RELIANCE", {"close": 2500.0, "atr_14": 50.0}, current_price=2500.0,
        )

        assert pred.signal_type == "SELL"
        assert pred.target_price == 2400.0   # entry - 2*atr
        assert pred.stop_loss_price == 2550.0  # entry + 1*atr

    async def test_hold_uses_symmetric_levels(self, model):
        _load(model, "swing", [0.2, 0.6, 0.2])  # argmax 1 -> HOLD

        pred = await model.predict_swing(
            "RELIANCE", {"close": 2500.0, "atr_14": 50.0}, current_price=2500.0,
        )

        assert pred.signal_type == "HOLD"
        assert pred.target_price == 2550.0   # entry + 1*atr
        assert pred.stop_loss_price == 2450.0  # entry - 1*atr

    async def test_intraday_slot_sets_holding_period(self, model):
        _load(model, "intraday", [0.1, 0.2, 0.7])

        pred = await model.predict_intraday(
            "RELIANCE", {"close": 2500.0, "atr_14": 50.0}, current_price=2500.0,
        )

        assert pred.signal_type == "BUY"
        assert pred.holding_period == "intraday"

    async def test_current_price_overrides_bar_close_for_entry(self, model):
        _load(model, "swing", [0.1, 0.2, 0.7])

        # Fresh LTP (2480) should be used for entry, not the stale bar close.
        pred = await model.predict_swing(
            "RELIANCE", {"close": 2500.0, "atr_14": 50.0}, current_price=2480.0,
        )

        assert pred.entry_price == 2480.0
        assert pred.target_price == 2580.0  # 2480 + 2*50


class TestCalibrationPath:
    async def test_agreeing_more_confident_calibrator_is_adopted(self, model):
        _load(model, "swing", [0.1, 0.2, 0.7])  # raw: BUY @ 0.70
        # Calibrator agrees (argmax BUY) and is more confident -> adopted.
        model._set_calibrator("swing", _FakeBooster([0.05, 0.15, 0.80]))

        pred = await model.predict_swing(
            "RELIANCE", {"close": 2500.0, "atr_14": 50.0}, current_price=2500.0,
        )

        assert pred.signal_type == "BUY"
        assert pred.confidence == pytest.approx(0.80, abs=1e-4)
        assert pred.class_probabilities == {"SELL": 0.05, "HOLD": 0.15, "BUY": 0.80}


class TestPredictErrorPaths:
    async def test_no_model_loaded_raises(self, model):
        with pytest.raises(RuntimeError, match="No swing model loaded"):
            await model.predict_swing(
                "RELIANCE", {"close": 2500.0}, current_price=2500.0,
            )

    async def test_no_usable_price_raises(self, model):
        _load(model, "swing", [0.1, 0.2, 0.7])

        # current_price None AND no bar close -> refuse to fabricate a price.
        with pytest.raises(ValueError, match="No usable price"):
            await model.predict_swing(
                "RELIANCE", {"atr_14": 50.0}, current_price=None,
            )
