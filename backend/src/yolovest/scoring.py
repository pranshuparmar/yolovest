"""Shared path-aware scoring for dry-runs and predictions.

A signal/prediction is scored over the daily bars of its holding window
(the bars between entry and the target date). Path-aware means
``target_hit`` / ``sl_hit`` are True if the price touched that level on
ANY bar in the window — not just the final day — while direction and the
realised move are measured at the window-end (target-date) close. This
mirrors how a real trade exits over its hold rather than judging a single
day's bar.

Pure function (no I/O, no yolovest deps) so it can be imported by both the
DB layer and skills without circular-import risk.
"""

from __future__ import annotations

from typing import Any


def path_aware_score(
    bars: list[tuple[Any, ...]],
    entry: float,
    target: float | None,
    sl: float | None,
    direction: str,
) -> dict[str, Any]:
    """Score one signal/prediction over a window of daily bars.

    ``bars`` are ascending tuples ``(open, high, low, close, date)`` and must
    be non-empty. Returns the actuals to persist: window open/close/high/low,
    direction_correct, target_hit, sl_hit, actual_move_pct (percent) and the
    target_date (the window-end bar's date).
    """
    window_open = float(bars[0][0])
    window_close = float(bars[-1][3])
    window_high = max(float(b[1]) for b in bars)
    window_low = min(float(b[2]) for b in bars)
    target_date = str(bars[-1][4])[:10]

    if direction == "BUY":
        direction_correct = 1 if window_close > entry else 0
        target_hit = 1 if (target and window_high >= target) else 0
        sl_hit = 1 if (sl and window_low <= sl) else 0
        move_pct = (window_close - entry) / entry * 100 if entry else 0.0
    else:  # SELL
        direction_correct = 1 if window_close < entry else 0
        target_hit = 1 if (target and window_low <= target) else 0
        sl_hit = 1 if (sl and window_high >= sl) else 0
        move_pct = (entry - window_close) / entry * 100 if entry else 0.0

    return {
        "actual_open": window_open,
        "actual_close": window_close,
        "actual_high": window_high,
        "actual_low": window_low,
        "direction_correct": direction_correct,
        "target_hit": target_hit,
        "sl_hit": sl_hit,
        "actual_move_pct": round(move_pct, 4),
        "target_date": target_date,
    }
