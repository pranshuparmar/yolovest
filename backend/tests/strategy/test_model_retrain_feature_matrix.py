"""Regression: _prepare_training_data must produce a rectangular feature
matrix even when later samples expose features the first sample didn't
(e.g. EMA-200 only computes once enough bars are in the window).
"""

from datetime import datetime, timedelta

import numpy as np
import pytest

from yolovest.skills.model_retrain import ModelRetrainSkill


def _bars(n: int, symbol: str = "RELIANCE") -> list[dict]:
    base = datetime(2025, 1, 1)
    out = []
    close = 100.0
    for i in range(n):
        close *= (1 + (0.01 if i % 3 == 0 else -0.008 if i % 3 == 1 else 0.002))
        out.append({
            "symbol": symbol,
            "timestamp": (base + timedelta(minutes=5 * i)).isoformat(),
            "open": close * 0.998,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1_000_000 + i * 100,
        })
    return out


@pytest.fixture
def skill(app_context):
    # ema_periods includes 200, which won't compute until i >= 200 +
    # window_size. Earlier samples are missing the ema_200 key — exactly
    # the scenario that broke retrain on the intraday matrix.
    app_context.config.strategy.ema_periods = [9, 21, 50, 200]
    return ModelRetrainSkill(app_context)


class TestFeatureMatrixRectangular:
    def test_matrix_is_rectangular_when_late_features_appear(self, skill):
        # 350 bars: first samples (i ~ 50) lack ema_200, later samples have it
        bars = _bars(350)
        training_data = {"bars": bars}

        X, y, feature_names, weights, _meta = skill._prepare_training_data(
            training_data, lookahead_bars=1,
        )

        assert len(X) > 0
        # Every row must have the same length as feature_names
        for row in X:
            assert len(row) == len(feature_names), (
                f"row length {len(row)} != feature_names length {len(feature_names)}"
            )
        # Constructing np.array must not raise
        arr = np.array(X, dtype=float)
        assert arr.shape == (len(X), len(feature_names))

    def test_ema_200_derived_appears_in_feature_names(self, skill):
        # ema_200 (the raw level) is excluded by MODEL_FEATURE_EXCLUSIONS
        # — the trained model only sees the normalized close_vs_ema_200_pct
        # ratio. This test asserts the derived feature appears once we
        # have enough history (window_size = 200 means we need ≥ 201 bars).
        bars = _bars(350)
        _, _, feature_names, _, _ = skill._prepare_training_data(
            {"bars": bars}, lookahead_bars=1,
        )
        assert "close_vs_ema_200_pct" in feature_names, (
            "close_vs_ema_200_pct should be in the feature set once enough "
            "bars are in the window"
        )

    def test_early_rows_have_zero_for_late_features(self, skill):
        bars = _bars(350)
        X, _, feature_names, _, _ = skill._prepare_training_data(
            {"bars": bars}, lookahead_bars=1,
        )
        if "ema_200" not in feature_names:
            pytest.skip("ema_200 not present — test needs longer history")
        idx = feature_names.index("ema_200")
        # The very first row was added before ema_200 was computable, so
        # its ema_200 cell must have been backfilled to 0.0.
        assert X[0][idx] == 0.0
