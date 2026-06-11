"""Extended-momentum / volatility-regime / fractional-difference features
(the swing-model Option-A enhancement). The contract that matters: these
must be price-invariant (transfer across the Nifty 500) and computed from
the TAIL of the bar list (so a 201-bar training window and a ~250-bar
inference fetch ending on the same bar produce identical values — no
train/serve skew).
"""

import math
import random

import pytest

from yolovest.data.features import (
    MODEL_SCHEMA_VERSION,
    IndicatorConfig,
    _fracdiff_logprice,
    _fracdiff_weights,
    _pct_return,
    _realized_vol,
    compute_features,
)
from yolovest.models.schemas import OHLCVBar

_NEW_KEYS = [
    "return_21d", "return_63d", "return_126d", "return_189d",
    "momentum_quality_63d", "vol_regime_ratio", "fracdiff_logprice",
]


def _bars(n: int, start: float, seed: int) -> list[OHLCVBar]:
    rng = random.Random(seed)
    out: list[OHLCVBar] = []
    c = start
    for i in range(n):
        c *= math.exp(rng.gauss(0.0006, 0.015))
        hi = c * (1 + abs(rng.gauss(0, 1)) * 0.005)
        lo = c * (1 - abs(rng.gauss(0, 1)) * 0.005)
        op = lo + (hi - lo) * rng.random()
        out.append(OHLCVBar(
            symbol="X", timestamp="2020-01-01T00:00:00",
            open=op, high=hi, low=lo, close=c, volume=100_000 + i,
        ))
    return out


class TestHelpers:
    def test_pct_return_math_and_guards(self):
        closes = [10.0, 11.0, 12.0, 13.2]
        assert _pct_return(closes, 1) == pytest.approx(13.2 / 12.0 - 1)
        assert _pct_return(closes, 3) == pytest.approx(13.2 / 10.0 - 1)
        assert _pct_return(closes, 4) is None  # not enough history
        assert _pct_return([0.0, 5.0], 1) is None  # non-positive base

    def test_realized_vol_zero_for_flat_series(self):
        assert _realized_vol([100.0] * 30, 20) == pytest.approx(0.0)
        assert _realized_vol([100.0] * 5, 20) is None  # insufficient

    def test_fracdiff_weights_recurrence(self):
        w = _fracdiff_weights(0.4, 4)
        assert w[0] == 1.0
        assert w[1] == pytest.approx(-0.4)
        assert w[2] == pytest.approx(-0.12)
        assert w[3] == pytest.approx(-0.064)

    def test_fracdiff_none_on_flat_or_short(self):
        assert _fracdiff_logprice([100.0] * 100) is None  # zero variance
        assert _fracdiff_logprice([100.0, 101.0]) is None  # too short


class TestFeatureEmission:
    def test_schema_version_bumped(self):
        assert MODEL_SCHEMA_VERSION >= 3

    def test_all_new_keys_present_with_enough_history(self):
        f = compute_features(_bars(260, 100.0, seed=1),
                             IndicatorConfig(extended_momentum=True))
        for k in _NEW_KEYS:
            assert k in f, f"missing {k}"

    def test_return_values_match_manual(self):
        bars = _bars(260, 100.0, seed=2)
        f = compute_features(bars, IndicatorConfig(extended_momentum=True))
        assert f["return_63d"] == pytest.approx(
            bars[-1].close / bars[-64].close - 1
        )
        assert f["return_189d"] == pytest.approx(
            bars[-1].close / bars[-190].close - 1
        )

    def test_toggle_off_removes_all_new_keys(self):
        f = compute_features(_bars(260, 100.0, seed=3),
                             IndicatorConfig(extended_momentum=False))
        assert not any(k in f for k in _NEW_KEYS)
        # The base technical features are unaffected.
        assert "rsi_14" in f and "atr_pct" in f


class TestPriceInvariance:
    def test_same_path_different_price_level_identical(self):
        # Identical % path seeded the same, one at ₹100 one at ₹3000.
        cfg = IndicatorConfig(extended_momentum=True)
        f_lo = compute_features(_bars(260, 100.0, seed=9), cfg)
        f_hi = compute_features(_bars(260, 3000.0, seed=9), cfg)
        for k in _NEW_KEYS:
            assert f_lo[k] == pytest.approx(f_hi[k], abs=1e-9), k


class TestWindowIndependence:
    def test_201_window_matches_longer_list(self):
        # The training window is bars[i-200:i+1] (201 bars); inference
        # fetches ~250. Both must yield identical features when they end on
        # the same bar — otherwise the model trains and serves on different
        # numbers. Compute on the full 260, then on just the last 201.
        cfg = IndicatorConfig(extended_momentum=True)
        full = _bars(260, 100.0, seed=4)
        f_full = compute_features(full, cfg)
        f_win = compute_features(full[-201:], cfg)
        for k in _NEW_KEYS:
            assert f_full[k] == pytest.approx(f_win[k], abs=1e-9), k


class TestNotInExclusions:
    def test_new_features_are_trained_on(self):
        # The new features are normalized/invariant, so they must NOT be in
        # MODEL_FEATURE_EXCLUSIONS — they are exactly what the model learns.
        from yolovest.data.features import MODEL_FEATURE_EXCLUSIONS
        for k in _NEW_KEYS:
            assert k not in MODEL_FEATURE_EXCLUSIONS, k
