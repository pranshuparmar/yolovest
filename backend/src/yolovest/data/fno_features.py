"""F&O option-chain derived features for the ML model.

Raw aggregates (PCR, OI, volume, futures close) are persisted daily by
`ingest-fno` into the `fno_daily` table. This module reads those rows
and derives the ML-facing feature set, including the categorical
OI-buildup classification that needs yesterday's row to compute.

Five features per sample:
  pcr_oi             — put OI / call OI across all strikes (>1 = puts dominate)
  pcr_volume         — put vol / call vol
  oi_change_pct_1d   — % change in front-month futures OI vs prior session
  oi_buildup_signal  — encoded buildup direction (see _classify_buildup)
  is_fno_stock       — 1.0 if F&O-eligible row exists, 0.0 otherwise

Only F&O-eligible names (~200 of Nifty 500) have rows. For other names
(or for any date before ingest-fno started collecting), is_fno_stock=0
and the other features are neutral. The model can learn to weight these
features only when the eligibility flag is set.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

FNO_FEATURE_KEYS: tuple[str, ...] = (
    "pcr_oi",
    "pcr_volume",
    "oi_change_pct_1d",
    "oi_buildup_signal",
    "is_fno_stock",
)


def _neutral() -> dict[str, float]:
    return {k: 0.0 for k in FNO_FEATURE_KEYS}


def _classify_buildup(price_change_pct: float, oi_change_pct: float) -> float:
    """Encode the textbook futures OI-buildup quadrant as a signed scalar.

    price ↑ + OI ↑  → long buildup       → +1.0 (bullish entry)
    price ↓ + OI ↑  → short buildup      → -1.0 (bearish entry)
    price ↑ + OI ↓  → short covering     → +0.5 (bullish exit)
    price ↓ + OI ↓  → long unwinding     → -0.5 (bearish exit)
    Either change near zero → 0.0 (no conviction).
    """
    # 0.1% deadband on each side — sub-deadband moves are noise.
    if abs(price_change_pct) < 0.001 or abs(oi_change_pct) < 0.001:
        return 0.0
    if price_change_pct > 0 and oi_change_pct > 0:
        return 1.0
    if price_change_pct < 0 and oi_change_pct > 0:
        return -1.0
    if price_change_pct > 0 and oi_change_pct < 0:
        return 0.5
    return -0.5


def compute_fno_features(
    symbol_timeline: Sequence[tuple[str, dict[str, float]]],
    as_of_date: str,
    prior_stock_close: float | None = None,
    current_stock_close: float | None = None,
) -> dict[str, float]:
    """Derive F&O features for a single (symbol, as_of_date) sample.

    symbol_timeline: sorted ascending list of (date_str, fno_row) where
      fno_row carries pcr_oi / pcr_volume / futures_oi / futures_volume /
      futures_close. Must contain entries for this symbol only — caller
      is expected to look up the per-symbol slice once and reuse it.

    as_of_date: YYYY-MM-DD. Only entries with date_str <= as_of_date
      are considered; rows after the cutoff are silently dropped to
      preserve leakage-safety when the same timeline is replayed
      across years of training samples.

    prior_stock_close / current_stock_close: optional. When provided,
      replace futures_close-derived price_change in the oi_buildup
      computation. Equity close is the canonical signal — futures can
      diverge near expiry. Caller supplies stock closes from the ohlcv
      table when available.
    """
    if not symbol_timeline:
        return _neutral()

    cutoff_idx = bisect_right(
        [d for d, _ in symbol_timeline], as_of_date,
    )
    in_window = symbol_timeline[:cutoff_idx]
    if not in_window:
        return _neutral()

    today = in_window[-1][1]
    yesterday = in_window[-2][1] if len(in_window) >= 2 else None

    pcr_oi = float(today.get("pcr_oi") or 0.0)
    pcr_vol = float(today.get("pcr_volume") or 0.0)

    futures_oi_today = float(today.get("futures_oi") or 0.0)
    oi_change_pct = 0.0
    if yesterday:
        futures_oi_yest = float(yesterday.get("futures_oi") or 0.0)
        if futures_oi_yest > 0:
            oi_change_pct = (futures_oi_today - futures_oi_yest) / futures_oi_yest

    # Prefer the underlying-equity close pair when callers supply it;
    # fall back to the futures-close series we already store.
    if (
        current_stock_close is not None
        and prior_stock_close is not None
        and prior_stock_close > 0
    ):
        price_change_pct = (current_stock_close - prior_stock_close) / prior_stock_close
    elif yesterday:
        futures_close_today = float(today.get("futures_close") or 0.0)
        futures_close_yest = float(yesterday.get("futures_close") or 0.0)
        price_change_pct = (
            (futures_close_today - futures_close_yest) / futures_close_yest
            if futures_close_yest > 0
            else 0.0
        )
    else:
        price_change_pct = 0.0

    return {
        "pcr_oi": pcr_oi,
        "pcr_volume": pcr_vol,
        "oi_change_pct_1d": oi_change_pct,
        "oi_buildup_signal": _classify_buildup(price_change_pct, oi_change_pct),
        "is_fno_stock": 1.0,
    }
