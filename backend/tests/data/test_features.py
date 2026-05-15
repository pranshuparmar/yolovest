"""Tests for feature engineering (technical indicators)."""

from datetime import datetime, timedelta

import pytest

from yolovest.data.features import (
    MODEL_FEATURE_EXCLUSIONS,
    IndicatorConfig,
    compute_atr,
    compute_bollinger_bands,
    compute_ema,
    compute_features,
    compute_macd,
    compute_obv,
    compute_rsi,
    compute_supertrend,
    compute_volume_profile,
    compute_vwap,
)
from yolovest.models.schemas import OHLCVBar


def _make_bars(n: int = 50) -> list[OHLCVBar]:
    """Generate synthetic OHLCV bars with a slight uptrend."""
    bars = []
    base_price = 100.0
    for i in range(n):
        c = base_price + i * 0.5 + (i % 3 - 1) * 0.3  # slight uptrend + noise
        o = c - 0.2
        h = c + 1.0
        lo = c - 1.0
        bars.append(
            OHLCVBar(
                timestamp=datetime(2026, 1, 1) + timedelta(days=i),
                open=o, high=h, low=lo, close=c,
                volume=1000 + i * 10,
            )
        )
    return bars


class TestRSI:
    def test_rsi_with_sufficient_data(self):
        closes = [44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
                  46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
                  46.22, 45.64]
        rsi = compute_rsi(closes, period=14)
        assert rsi is not None
        assert 0 <= rsi <= 100

    def test_rsi_insufficient_data(self):
        assert compute_rsi([100, 101, 102], period=14) is None

    def test_rsi_all_gains(self):
        closes = list(range(100, 120))
        rsi = compute_rsi(closes, period=14)
        assert rsi is not None
        assert rsi > 90  # Should be very high for all-gains

    def test_rsi_all_losses(self):
        closes = list(range(120, 100, -1))
        rsi = compute_rsi(closes, period=14)
        assert rsi is not None
        assert rsi < 10  # Should be very low for all-losses


class TestMACD:
    def test_macd_with_sufficient_data(self):
        closes = [float(100 + i * 0.5) for i in range(50)]
        result = compute_macd(closes)
        assert "macd_line" in result
        assert "macd_signal" in result
        assert "macd_histogram" in result

    def test_macd_insufficient_data(self):
        result = compute_macd([100.0] * 10)
        assert result == {}


class TestBollingerBands:
    def test_bollinger_bands(self):
        closes = [float(100 + i) for i in range(25)]
        result = compute_bollinger_bands(closes, period=20)
        assert "bb_upper" in result
        assert "bb_middle" in result
        assert "bb_lower" in result
        assert "bb_width" in result
        assert result["bb_upper"] > result["bb_middle"] > result["bb_lower"]

    def test_bollinger_insufficient_data(self):
        result = compute_bollinger_bands([100.0] * 5, period=20)
        assert result == {}

    def test_bollinger_constant_price(self):
        closes = [100.0] * 25
        result = compute_bollinger_bands(closes, period=20)
        # No variance → bands converge
        assert result["bb_upper"] == result["bb_lower"] == result["bb_middle"]


class TestVWAP:
    def test_vwap_basic(self):
        bars = _make_bars(10)
        vwap = compute_vwap(bars)
        assert vwap is not None
        assert vwap > 0

    def test_vwap_empty(self):
        assert compute_vwap([]) is None

    def test_vwap_zero_volume(self):
        bars = [OHLCVBar(
            timestamp=datetime.now(),
            open=100, high=105, low=95, close=100, volume=0,
        )]
        assert compute_vwap(bars) is None


class TestATR:
    def test_atr_basic(self):
        bars = _make_bars(20)
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        atr = compute_atr(highs, lows, closes, period=14)
        assert atr is not None
        assert atr > 0

    def test_atr_insufficient_data(self):
        assert compute_atr([100], [90], [95], period=14) is None


class TestVolumeProfile:
    def test_relative_volume(self):
        volumes = [1000, 1000, 1000, 2000]  # last bar is 2x average
        result = compute_volume_profile(volumes)
        assert result["relative_volume"] == pytest.approx(2.0)

    def test_single_bar(self):
        result = compute_volume_profile([1000])
        assert result == {}


class TestOBV:
    def test_obv_uptrend(self):
        closes = [100, 101, 102, 103, 104]
        volumes = [1000, 1000, 1000, 1000, 1000]
        obv = compute_obv(closes, volumes)
        assert obv is not None
        assert obv == 4000  # all up days

    def test_obv_downtrend(self):
        closes = [104, 103, 102, 101, 100]
        volumes = [1000, 1000, 1000, 1000, 1000]
        obv = compute_obv(closes, volumes)
        assert obv == -4000  # all down days

    def test_obv_insufficient(self):
        assert compute_obv([100], [1000]) is None


class TestSuperTrend:
    def test_supertrend_basic(self):
        bars = _make_bars(20)
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        result = compute_supertrend(highs, lows, closes, period=10)
        assert "supertrend_upper" in result
        assert "supertrend_lower" in result
        assert "supertrend_trend" in result
        assert result["supertrend_trend"] in (1.0, -1.0)

    def test_supertrend_insufficient(self):
        result = compute_supertrend([100], [90], [95], period=10)
        assert result == {}


class TestEMA:
    def test_ema_basic(self):
        values = [float(100 + i) for i in range(20)]
        ema = compute_ema(values, period=9)
        assert ema is not None
        # EMA should be close to the latest values for trending data
        assert abs(ema - values[-1]) < 5

    def test_ema_insufficient(self):
        assert compute_ema([100, 101], period=9) is None


class TestComputeFeatures:
    def test_all_features_computed(self):
        bars = _make_bars(50)
        features = compute_features(bars)
        assert "rsi_14" in features
        assert "macd_line" in features
        assert "bb_upper" in features
        assert "vwap" in features
        assert "atr_14" in features
        assert "relative_volume" in features
        assert "obv" in features
        assert "supertrend_trend" in features
        assert "ema_9" in features
        assert "ema_200" not in features  # not enough data for 200-period

    def test_selective_features(self):
        bars = _make_bars(50)
        config = IndicatorConfig(rsi=True, macd=False, bollinger_bands=False,
                                 vwap=False, atr=False, volume_profile=False,
                                 obv=False, supertrend=False, ema_periods=[])
        features = compute_features(bars, config)
        assert "rsi_14" in features
        assert "macd_line" not in features
        assert "bb_upper" not in features

    def test_empty_bars(self):
        assert compute_features([]) == {}


class TestNormalizedFeatures:
    """The model-facing features should be price-invariant ratios.

    These features are the ones the ML model actually sees after
    MODEL_FEATURE_EXCLUSIONS filters out raw absolute prices/levels.
    """

    def test_normalized_features_emitted(self) -> None:
        bars = _make_bars(50)
        features = compute_features(bars)
        # The new normalized derivatives must all be present.
        assert "range_pct" in features
        assert "body_pct" in features
        assert "gap_pct" in features
        assert "close_change_pct" in features
        assert "vwap_distance_pct" in features
        assert "bb_position" in features
        assert "macd_histogram_pct" in features
        assert "macd_line_pct" in features
        assert "close_vs_ema_9_pct" in features
        assert "ema_9_vs_21_pct" in features
        assert "supertrend_distance_pct" in features
        assert "volume_zscore_20d" in features

    def test_range_pct_matches_arithmetic(self) -> None:
        bars = _make_bars(50)
        features = compute_features(bars)
        last = bars[-1]
        expected = (last.high - last.low) / last.close
        assert features["range_pct"] == pytest.approx(expected)

    def test_features_scale_invariant_across_price_levels(self) -> None:
        """Same shape at ₹100 and ₹3000 → normalized features identical."""
        bars_cheap = _make_bars(50)
        # Build identically-shaped bars but at 30x the price level.
        bars_expensive = [
            OHLCVBar(
                timestamp=b.timestamp,
                open=b.open * 30, high=b.high * 30,
                low=b.low * 30, close=b.close * 30,
                volume=b.volume,
            )
            for b in bars_cheap
        ]
        f_cheap = compute_features(bars_cheap)
        f_exp = compute_features(bars_expensive)
        # Key invariant: every normalized feature is identical.
        for key in [
            "range_pct", "body_pct", "gap_pct", "close_change_pct",
            "vwap_distance_pct", "bb_position",
            "macd_histogram_pct", "macd_line_pct",
            "close_vs_ema_9_pct", "ema_9_vs_21_pct",
            "supertrend_distance_pct", "atr_pct",
        ]:
            assert f_cheap[key] == pytest.approx(f_exp[key], rel=1e-6), \
                f"feature {key} not scale-invariant"

    def test_bb_position_inside_band(self) -> None:
        bars = _make_bars(50)
        features = compute_features(bars)
        # bb_position can drift outside [0,1] in trending data but should
        # generally be within a sane band — sanity check, not exact.
        assert -2.0 <= features["bb_position"] <= 3.0

    def test_volume_zscore_zero_for_flat_volumes(self) -> None:
        bars = _make_bars(50)
        # Force all volumes equal so sigma=0 → guard returns 0.0
        flat_bars = [
            OHLCVBar(
                timestamp=b.timestamp, open=b.open, high=b.high,
                low=b.low, close=b.close, volume=1000,
            )
            for b in bars
        ]
        features = compute_features(flat_bars)
        assert features["volume_zscore_20d"] == 0.0

    def test_obv_change_5d_pct_emitted(self) -> None:
        bars = _make_bars(50)
        features = compute_features(bars)
        assert "obv_change_5d_pct" in features

    def test_raw_levels_still_in_features_dict(self) -> None:
        """Raw prices must remain — the inference layer reads `close`
        and `atr_14` for entry-price fallbacks."""
        bars = _make_bars(50)
        features = compute_features(bars)
        assert "close" in features
        assert "atr_14" in features

    def test_exclusion_set_covers_raw_levels(self) -> None:
        """All emitted raw-level features must be in the exclusion set,
        so the model never trains on absolute prices."""
        bars = _make_bars(50)
        features = compute_features(bars)
        for raw_key in [
            "close", "open", "high", "low",
            "vwap", "atr_14", "obv",
            "bb_upper", "bb_middle", "bb_lower",
            "ema_9", "ema_21", "ema_50",
            "macd_line", "macd_signal", "macd_histogram",
            "supertrend_upper", "supertrend_lower",
            "avg_volume",
        ]:
            if raw_key in features:
                assert raw_key in MODEL_FEATURE_EXCLUSIONS, \
                    f"raw level {raw_key} is emitted but not in MODEL_FEATURE_EXCLUSIONS"
