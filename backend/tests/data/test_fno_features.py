"""Tests for F&O option-chain derived features."""
from __future__ import annotations

import pytest

from yolovest.data.fno_features import (
    FNO_FEATURE_KEYS,
    _classify_buildup,
    compute_fno_features,
)


def _row(
    pcr_oi: float = 1.0,
    pcr_volume: float = 1.0,
    futures_oi: float = 1_000_000.0,
    futures_volume: float = 100_000.0,
    futures_close: float = 100.0,
) -> dict[str, float]:
    return {
        "pcr_oi": pcr_oi,
        "pcr_volume": pcr_volume,
        "futures_oi": futures_oi,
        "futures_volume": futures_volume,
        "futures_close": futures_close,
    }


def test_empty_timeline_returns_neutral() -> None:
    feats = compute_fno_features([], "2025-06-15")
    assert feats == {k: 0.0 for k in FNO_FEATURE_KEYS}


def test_keys_always_present() -> None:
    feats = compute_fno_features([], "2025-06-15")
    assert set(feats.keys()) == set(FNO_FEATURE_KEYS)


def test_is_fno_stock_flag_on_match() -> None:
    timeline = [("2025-06-10", _row())]
    feats = compute_fno_features(timeline, "2025-06-10")
    assert feats["is_fno_stock"] == 1.0


def test_is_fno_stock_zero_when_no_data() -> None:
    feats = compute_fno_features([], "2025-06-10")
    assert feats["is_fno_stock"] == 0.0


def test_pcr_values_passed_through() -> None:
    timeline = [("2025-06-10", _row(pcr_oi=1.35, pcr_volume=0.85))]
    feats = compute_fno_features(timeline, "2025-06-10")
    assert feats["pcr_oi"] == pytest.approx(1.35)
    assert feats["pcr_volume"] == pytest.approx(0.85)


def test_oi_change_zero_without_prior() -> None:
    timeline = [("2025-06-10", _row(futures_oi=1_000_000))]
    feats = compute_fno_features(timeline, "2025-06-10")
    assert feats["oi_change_pct_1d"] == 0.0


def test_oi_change_positive() -> None:
    timeline = [
        ("2025-06-09", _row(futures_oi=1_000_000)),
        ("2025-06-10", _row(futures_oi=1_200_000)),
    ]
    feats = compute_fno_features(timeline, "2025-06-10")
    assert feats["oi_change_pct_1d"] == pytest.approx(0.20)


def test_future_rows_excluded_from_cutoff() -> None:
    timeline = [
        ("2025-06-09", _row(futures_oi=1_000_000)),
        ("2025-06-10", _row(futures_oi=1_200_000)),
        ("2025-06-11", _row(futures_oi=2_000_000)),  # future — must be ignored
    ]
    feats_truncated = compute_fno_features(timeline[:2], "2025-06-10")
    feats_full = compute_fno_features(timeline, "2025-06-10")
    assert feats_full == feats_truncated


def test_classify_buildup_long_buildup() -> None:
    """Price up + OI up → long buildup."""
    assert _classify_buildup(0.02, 0.05) == 1.0


def test_classify_buildup_short_buildup() -> None:
    """Price down + OI up → short buildup."""
    assert _classify_buildup(-0.02, 0.05) == -1.0


def test_classify_buildup_short_covering() -> None:
    """Price up + OI down → short covering."""
    assert _classify_buildup(0.02, -0.05) == 0.5


def test_classify_buildup_long_unwinding() -> None:
    """Price down + OI down → long unwinding."""
    assert _classify_buildup(-0.02, -0.05) == -0.5


def test_classify_buildup_deadband() -> None:
    """Sub-deadband moves return 0 (no conviction)."""
    assert _classify_buildup(0.0005, 0.05) == 0.0
    assert _classify_buildup(0.05, 0.0005) == 0.0


def test_buildup_uses_supplied_stock_close_over_futures() -> None:
    """Equity close pair is canonical; futures can diverge near expiry."""
    timeline = [
        ("2025-06-09", _row(futures_oi=1_000_000, futures_close=99.0)),
        ("2025-06-10", _row(futures_oi=1_200_000, futures_close=99.5)),
    ]
    # Futures changed barely (+0.5%) but stock moved sharply (+2%).
    # Use stock pair → +2% price × +20% OI → long buildup = 1.0.
    feats = compute_fno_features(
        timeline, "2025-06-10",
        prior_stock_close=100.0, current_stock_close=102.0,
    )
    assert feats["oi_buildup_signal"] == 1.0


def test_buildup_falls_back_to_futures_close_without_stock_pair() -> None:
    timeline = [
        ("2025-06-09", _row(futures_oi=1_000_000, futures_close=100.0)),
        ("2025-06-10", _row(futures_oi=1_200_000, futures_close=102.0)),
    ]
    feats = compute_fno_features(timeline, "2025-06-10")
    assert feats["oi_buildup_signal"] == 1.0


def test_zero_call_oi_pcr_safe() -> None:
    """Undefined PCR (zero call OI) must not crash — return 0.0."""
    # We pass pcr_oi=0.0 directly, which is what fno_provider stores
    # when sum(CE OI) == 0. compute_fno_features treats it as neutral.
    timeline = [("2025-06-10", _row(pcr_oi=0.0))]
    feats = compute_fno_features(timeline, "2025-06-10")
    assert feats["pcr_oi"] == 0.0


def test_as_of_before_any_row_returns_neutral() -> None:
    timeline = [("2025-06-10", _row())]
    feats = compute_fno_features(timeline, "2025-06-05")
    assert feats["is_fno_stock"] == 0.0
    assert feats["pcr_oi"] == 0.0
