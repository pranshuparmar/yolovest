"""Tests for the two-layer tuned-threshold safety net in
`XGBoostSignalModel._get_effective_thresholds`.

Covers:
- `risk.tuned_threshold_max_diff` symmetry cap (existing behaviour)
- `risk.tuned_threshold_max_value` absolute ceiling (added to address
  the swing-model class-collapse where the sweep saved (0.65, 0.70)
  but the calibrated probabilities rarely crossed 0.60).
- Override flags suppress both caps.
"""

from unittest.mock import MagicMock

from yolovest.strategy.ml_signal import XGBoostSignalModel


def _make_signal_model(
    tmp_path,
    *,
    tuned_buy: float,
    tuned_sell: float,
    max_diff: float = 0.05,
    max_value: float = 0.60,
    buy_override: float | None = None,
    sell_override: float | None = None,
):
    cfg = MagicMock()
    cfg.risk = MagicMock(
        tuned_threshold_max_diff=max_diff,
        tuned_threshold_max_value=max_value,
        buy_threshold_override=buy_override,
        sell_threshold_override=sell_override,
    )
    sm = XGBoostSignalModel(model_dir=str(tmp_path), config=cfg)
    sm._set_thresholds("intraday", {"buy": tuned_buy, "sell": tuned_sell})
    return sm


class TestThresholdCeiling:
    def test_below_ceiling_no_change(self, tmp_path):
        sm = _make_signal_model(tmp_path, tuned_buy=0.55, tuned_sell=0.55)
        assert sm._get_effective_thresholds("intraday") == {"buy": 0.55, "sell": 0.55}

    def test_ceiling_caps_both(self, tmp_path):
        """(0.70, 0.70) with ceiling 0.60 → both pulled to 0.60."""
        sm = _make_signal_model(tmp_path, tuned_buy=0.70, tuned_sell=0.70)
        out = sm._get_effective_thresholds("intraday")
        assert out == {"buy": 0.60, "sell": 0.60}

    def test_ceiling_caps_only_above(self, tmp_path):
        """sell over ceiling, buy below → only sell pulled down."""
        sm = _make_signal_model(tmp_path, tuned_buy=0.55, tuned_sell=0.65)
        out = sm._get_effective_thresholds("intraday")
        # diff was 0.10 > max_diff=0.05 so symmetry cap fires first,
        # producing buy=0.575, sell=0.625. Then ceiling caps sell at 0.60.
        assert out == {"buy": 0.575, "sell": 0.60}

    def test_ceiling_disabled_at_one(self, tmp_path):
        """max_value=1.0 leaves the saved thresholds alone."""
        sm = _make_signal_model(
            tmp_path, tuned_buy=0.80, tuned_sell=0.80, max_value=1.0,
        )
        out = sm._get_effective_thresholds("intraday")
        assert out == {"buy": 0.80, "sell": 0.80}

    def test_override_skips_ceiling(self, tmp_path):
        """Explicit override = user has spoken, bypass both caps."""
        sm = _make_signal_model(
            tmp_path, tuned_buy=0.65, tuned_sell=0.70,
            buy_override=0.75, sell_override=0.85,
        )
        assert sm._get_effective_thresholds("intraday") == {
            "buy": 0.75, "sell": 0.85,
        }


class TestThresholdDiffCapStillWorks:
    """Regression: the existing symmetry cap must still apply when
    the ceiling doesn't trigger.
    """

    def test_asymmetric_below_ceiling_gets_shrunk(self, tmp_path):
        sm = _make_signal_model(tmp_path, tuned_buy=0.40, tuned_sell=0.55)
        out = sm._get_effective_thresholds("intraday")
        # diff=0.15, max_diff=0.05 → midpoint 0.475, half_gap 0.025.
        # buy < sell so buy pulled up, sell pulled down: buy=0.45, sell=0.50.
        assert out == {"buy": 0.45, "sell": 0.50}


class TestNoSavedThresholds:
    def test_returns_none_when_unset(self, tmp_path):
        sm = _make_signal_model(tmp_path, tuned_buy=0.65, tuned_sell=0.65)
        sm._set_thresholds("intraday", None)
        assert sm._get_effective_thresholds("intraday") is None
