"""India VIX-derived market-wide volatility-regime features.

VIX is a single time series (the NSE volatility index) that becomes a
broadcast feature: every per-(symbol, bar-date) sample on a given date
sees the same VIX context. This is the macro regime layer ChatGPT's
review flagged as the only kind of "is today a chop day or a trend day"
signal the per-stock indicators can't see on their own — universe
breadth covers cross-sectional dispersion, VIX covers anticipated vol.

Three features per sample:
  vix_level         — VIX close on (or before) the bar date
  vix_change_5d_pct — 5-trading-day percent change (rolling spike detector)
  vix_zscore_20d    — (level − 20d mean) / 20d std (regime z-score)

`compute_vix_features` takes a pre-sorted list of (date_str, close)
tuples covering at least the trailing 20 sessions before as_of_date.
Neutral defaults (0.0) when the timeline is empty or too short.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from statistics import mean, pstdev

VIX_FEATURE_KEYS: tuple[str, ...] = (
    "vix_level",
    "vix_change_5d_pct",
    "vix_zscore_20d",
)


def _neutral() -> dict[str, float]:
    return {k: 0.0 for k in VIX_FEATURE_KEYS}


def compute_vix_features(
    timeline: Sequence[tuple[str, float]],
    as_of_date: str,
) -> dict[str, float]:
    """Aggregate VIX context as of as_of_date (YYYY-MM-DD).

    timeline must be sorted ascending by date_str. Only entries with
    date_str <= as_of_date are considered; this prevents leakage when
    the same timeline is reused across training samples spanning years.

    Bars are not required to be contiguous — `vix_change_5d_pct` uses
    the 6th-newest available bar within the cutoff, which matches the
    "five sessions ago" intent on a non-trading-day-aware calendar.
    """
    if not timeline:
        return _neutral()

    cutoff_idx = bisect_right(
        [d for d, _ in timeline], as_of_date,
    )
    in_window = timeline[:cutoff_idx]
    if not in_window:
        return _neutral()

    closes = [v for _, v in in_window if v is not None and v > 0]
    if not closes:
        return _neutral()

    latest = float(closes[-1])

    if len(closes) >= 6:
        five_back = float(closes[-6])
        change_5d_pct = (latest - five_back) / five_back if five_back > 0 else 0.0
    else:
        change_5d_pct = 0.0

    window_20 = closes[-20:]
    if len(window_20) >= 5:
        mu = mean(window_20)
        sigma = pstdev(window_20)
        zscore = (latest - mu) / sigma if sigma > 0 else 0.0
    else:
        zscore = 0.0

    return {
        "vix_level": latest,
        "vix_change_5d_pct": float(change_5d_pct),
        "vix_zscore_20d": float(zscore),
    }
