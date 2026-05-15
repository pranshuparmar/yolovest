"""Tests for India VIX-derived market regime features."""
from __future__ import annotations

import pytest

from yolovest.data.vix_features import VIX_FEATURE_KEYS, compute_vix_features


def _series(start: float, n: int, step: float = 0.0) -> list[tuple[str, float]]:
    """Build a (date_str, close) timeline with daily increments."""
    out: list[tuple[str, float]] = []
    for i in range(n):
        day = 1 + i
        date_str = f"2025-06-{day:02d}"
        out.append((date_str, start + step * i))
    return out


def test_empty_timeline_returns_neutral() -> None:
    feats = compute_vix_features([], "2025-06-15")
    assert feats == {k: 0.0 for k in VIX_FEATURE_KEYS}


def test_keys_always_present() -> None:
    feats = compute_vix_features([], "2025-06-15")
    assert set(feats.keys()) == set(VIX_FEATURE_KEYS)


def test_level_returns_latest_in_window() -> None:
    timeline = _series(start=12.0, n=10, step=0.5)
    feats = compute_vix_features(timeline, "2025-06-10")
    assert feats["vix_level"] == pytest.approx(12.0 + 0.5 * 9)


def test_level_respects_as_of_cutoff() -> None:
    timeline = _series(start=12.0, n=10, step=0.5)
    # cutoff falls on day 5 — must use that close, not the latest
    feats = compute_vix_features(timeline, "2025-06-05")
    assert feats["vix_level"] == pytest.approx(12.0 + 0.5 * 4)


def test_future_bars_excluded() -> None:
    """Bars dated after as_of_date must not leak into features."""
    timeline = _series(start=12.0, n=10, step=0.5)
    feats_truncated = compute_vix_features(timeline[:5], "2025-06-05")
    feats_full = compute_vix_features(timeline, "2025-06-05")
    assert feats_full == feats_truncated


def test_change_5d_pct_rising() -> None:
    timeline = _series(start=10.0, n=10, step=1.0)
    # day 10 close = 19; 6 sessions back (5d ago) = 14; (19-14)/14 ≈ 0.357
    feats = compute_vix_features(timeline, "2025-06-10")
    assert feats["vix_change_5d_pct"] == pytest.approx((19.0 - 14.0) / 14.0)


def test_change_5d_pct_neutral_when_short() -> None:
    timeline = _series(start=10.0, n=3, step=1.0)
    feats = compute_vix_features(timeline, "2025-06-03")
    assert feats["vix_change_5d_pct"] == 0.0


def test_zscore_high_after_spike() -> None:
    """A sharp recent jump should produce a positive z-score."""
    timeline = _series(start=12.0, n=19, step=0.0) + [("2025-06-20", 25.0)]
    feats = compute_vix_features(timeline, "2025-06-20")
    assert feats["vix_zscore_20d"] > 1.0


def test_zscore_neutral_for_flat_series() -> None:
    timeline = _series(start=15.0, n=20, step=0.0)
    feats = compute_vix_features(timeline, "2025-06-20")
    # Flat series → sigma=0 → guard returns 0.0
    assert feats["vix_zscore_20d"] == 0.0


def test_zscore_negative_when_below_mean() -> None:
    timeline = _series(start=20.0, n=19, step=0.0) + [("2025-06-20", 10.0)]
    feats = compute_vix_features(timeline, "2025-06-20")
    assert feats["vix_zscore_20d"] < -1.0


def test_zero_close_filtered_out() -> None:
    """Bogus zero closes must be ignored, not treated as a regime crash."""
    timeline = [
        ("2025-06-01", 12.0),
        ("2025-06-02", 0.0),
        ("2025-06-03", 13.0),
    ]
    feats = compute_vix_features(timeline, "2025-06-03")
    assert feats["vix_level"] == pytest.approx(13.0)


def test_change_5d_uses_available_bars_when_calendar_has_gaps() -> None:
    """Non-contiguous bars (weekends, holidays) should still produce a
    sensible 5-session delta — we walk index positions, not calendar days."""
    timeline = [
        ("2025-06-02", 12.0),
        ("2025-06-03", 12.5),
        ("2025-06-04", 13.0),
        ("2025-06-05", 13.5),
        ("2025-06-06", 14.0),
        # weekend gap
        ("2025-06-09", 15.0),
    ]
    feats = compute_vix_features(timeline, "2025-06-09")
    # 6 entries; latest = 15.0; six-back = 12.0; (15-12)/12 = 0.25
    assert feats["vix_change_5d_pct"] == pytest.approx(0.25)
