"""Milestone-3 policy tests: shadow/production decision parity, the
date-snapped tune/report split, purged calibration folds, and the swing
horizon cap."""

from datetime import date

import numpy as np
import pytest

from yolovest.strategy.ml_signal import (
    XGBoostSignalModel,
    _choose_calibrated,
    _purged_time_series_splits,
    _snap_to_date_boundary,
    _threshold_label,
)
from yolovest.strategy.signal_evaluator import _capped_days_range
from yolovest.strategy.walk_forward_backtest import BarMeta


class _FakeModel:
    def __init__(self, probas):
        self._p = np.array(probas, dtype=float)

    def predict(self, X):  # noqa: N803
        return [int(np.argmax(self._p[0]))]

    def predict_proba(self, X):  # noqa: N803
        return self._p


class TestSharedDecisionHelpers:
    def test_choose_calibrated_adopts_on_agreement_and_higher_confidence(self):
        raw = [0.1, 0.2, 0.7]
        cal = [0.05, 0.15, 0.8]  # same argmax (BUY), more confident
        assert _choose_calibrated(raw, cal) is cal

    def test_choose_calibrated_keeps_raw_on_disagreement(self):
        raw = [0.1, 0.3, 0.6]   # BUY
        cal = [0.1, 0.6, 0.3]   # HOLD — the over-correction case
        assert _choose_calibrated(raw, cal) is raw

    def test_choose_calibrated_keeps_raw_on_compression(self):
        raw = [0.1, 0.2, 0.7]
        cal = [0.2, 0.3, 0.5]   # same argmax, lower confidence
        assert _choose_calibrated(raw, cal) is raw

    def test_argmax_is_invariant_under_choice(self):
        # The adoption rule can never flip the argmax — only magnitudes.
        for cal in ([0.05, 0.15, 0.8], [0.1, 0.6, 0.3], None):
            chosen = _choose_calibrated([0.1, 0.2, 0.7], cal)
            assert int(np.argmax(chosen)) == 2

    def test_threshold_label_gates_and_falls_back_to_argmax(self):
        th = {"buy": 0.55, "sell": 0.60}
        assert _threshold_label([0.05, 0.15, 0.80], th) == 2
        assert _threshold_label([0.10, 0.40, 0.50], th) == 1
        assert _threshold_label([0.65, 0.30, 0.05], th) == 0
        assert _threshold_label([0.10, 0.40, 0.50], None) == 2  # argmax


class TestShadowRunsProductionPolicy:
    """The shadow's scored predictions feed the promotion live-accuracy
    gate — it must run the SAME decision policy (agreement-gated
    calibration + tuned thresholds under config caps) the artifact would
    run in production after promotion."""

    def _shadow(self, probas, thresholds):
        m = XGBoostSignalModel(model_dir="/tmp/shadow_policy_test")
        m._shadow_swing_model = _FakeModel(probas)
        m._shadow_swing_thresholds = thresholds
        m._shadow_swing_version = "swing_vTEST"
        m._config = None  # default caps (max_diff 0.05, max_value 0.60)
        return m

    async def test_shadow_applies_tuned_thresholds(self):
        # Raw argmax is BUY at 0.50, but the tuned buy threshold is 0.55
        # → the production policy says HOLD; the old shadow path
        # (argmax, no thresholds) said BUY.
        m = self._shadow([[0.10, 0.40, 0.50]], {"buy": 0.55, "sell": 0.60})
        pred = await m.predict_shadow_swing(
            "RELIANCE", {"close": 100.0}, current_price=100.0,
        )
        assert pred is not None
        assert pred.signal_type == "HOLD"

    async def test_shadow_signals_above_thresholds(self):
        m = self._shadow([[0.05, 0.15, 0.80]], {"buy": 0.55, "sell": 0.60})
        pred = await m.predict_shadow_swing(
            "RELIANCE", {"close": 100.0}, current_price=100.0,
        )
        assert pred is not None
        assert pred.signal_type == "BUY"
        assert pred.confidence == pytest.approx(0.80)

    async def test_shadow_without_thresholds_uses_argmax(self):
        m = self._shadow([[0.10, 0.40, 0.50]], None)
        pred = await m.predict_shadow_swing(
            "RELIANCE", {"close": 100.0}, current_price=100.0,
        )
        assert pred is not None
        assert pred.signal_type == "BUY"


class TestDateSnappedSplit:
    def _metas(self, dates):
        return [
            BarMeta(symbol="S", entry_close=100, exit_close=101,
                    entry_date=d)
            for d in dates
        ]

    def test_split_advances_past_same_day_rows(self):
        metas = self._metas(
            ["2026-01-01"] * 3 + ["2026-01-02"] * 4 + ["2026-01-03"] * 3
        )
        # Raw midpoint (5) lands inside the 01-02 block → snaps to 7.
        assert _snap_to_date_boundary(metas, 5) == 7
        assert metas[6].entry_date != metas[7].entry_date

    def test_boundary_index_is_unchanged(self):
        metas = self._metas(["2026-01-01"] * 3 + ["2026-01-02"] * 3)
        assert _snap_to_date_boundary(metas, 3) == 3

    def test_snap_consuming_tail_keeps_raw_index(self):
        metas = self._metas(["2026-01-01"] * 2 + ["2026-01-02"] * 8)
        # Snapping from 5 would run off the end → keep 5.
        assert _snap_to_date_boundary(metas, 5) == 5

    def test_out_of_range_passthrough(self):
        metas = self._metas(["2026-01-01"] * 4)
        assert _snap_to_date_boundary(metas, 0) == 0
        assert _snap_to_date_boundary(metas, 4) == 4


class TestPurgedCalibrationSplits:
    def test_train_tail_is_purged_by_label_window(self):
        # 100 samples, ~4/day over 25 days.
        dates = [date(2026, 1, 1 + i // 4) for i in range(100)]
        splits = _purged_time_series_splits(
            100, n_splits=3, sample_dates=dates, purge_days=5,
        )
        assert len(splits) == 3
        for tr, te in splits:
            test_start = dates[int(te[0])]
            assert all(
                (test_start - dates[int(i)]).days >= 5 for i in tr
            ), "train row within the purge window of the test start"
            # Folds still chronological.
            assert max(int(i) for i in tr) < min(int(i) for i in te)

    def test_zero_purge_matches_plain_splits(self):
        dates = [date(2026, 1, 1 + i // 4) for i in range(60)]
        splits = _purged_time_series_splits(
            60, n_splits=3, sample_dates=dates, purge_days=0,
        )
        from sklearn.model_selection import TimeSeriesSplit

        plain = list(TimeSeriesSplit(n_splits=3).split(np.arange(60)))
        for (tr_a, te_a), (tr_b, te_b) in zip(splits, plain, strict=True):
            assert list(tr_a) == list(tr_b)
            assert list(te_a) == list(te_b)


class TestSwingHorizonCap:
    def test_caps_long_horizon_range(self):
        assert _capped_days_range((5, 66), 15) == (5, 15)

    def test_inside_cap_unchanged(self):
        assert _capped_days_range((0, 15), 15) == (0, 15)

    def test_zero_disables(self):
        assert _capped_days_range((5, 66), 0) == (5, 66)

    def test_none_passthrough(self):
        assert _capped_days_range(None, 15) is None

    def test_config_default_is_fifteen(self):
        from yolovest.config import AppConfig

        cfg = AppConfig(broker={"api_key": "k", "api_secret": "s"})
        assert cfg.strategy.swing_horizon_cap_days == 15
