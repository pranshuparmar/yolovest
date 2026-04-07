"""Shared holding period decision logic.

Used by both the generate-signals skill (live pipeline) and the dry-run
endpoint (signal preview). Keeps the decision logic in one place.

Holding periods are now dynamic: instead of fixed "3d" or "1w" buckets,
the system computes expected_holding_days per stock based on ATR and the
target move, clamped to the mode's allowed range.
"""

import logging
import math
from datetime import time
from typing import Any

logger = logging.getLogger(__name__)

# ATR multiplier interpolation anchors: (days, target_mult, sl_mult)
# Used to interpolate ATR multipliers for any holding duration.
_INTERP_ANCHORS = [
    (0, 0.75, 0.5),   # intraday — capture half the daily range
    (3, 1.5, 0.75),   # short swing
    (5, 2.5, 1.2),    # week
    (15, 3.5, 1.5),   # 2-3 weeks
    (44, 4.5, 2.0),   # ~2 months
    (66, 5.5, 2.5),   # ~3 months
]


def decide_holding_period(
    features: dict[str, Any],
    allowed_periods: list[str],
    volatility_config: Any,
    now_time: time,
    mode_days_range: tuple[int, int] | None = None,
) -> tuple[str, str, int]:
    """Decide holding period, product type, and expected days based on stock characteristics.

    Args:
        features: Computed technical features dict (must include atr_pct, close, etc.)
        allowed_periods: List of allowed holding period labels (e.g. ["intraday", "short_term"])
        volatility_config: VolatilityConfig instance with ATR% thresholds
        now_time: Current time (IST) for intraday time-of-day gating
        mode_days_range: (min_days, max_days) from the strategy mode config.
            If None, derived from allowed_periods for backwards compatibility.

    Returns:
        (holding_period_label, product, expected_holding_days)
        e.g. ("intraday", "MIS", 0) or ("short_term", "CNC", 4) or ("long_term", "CNC", 22)
    """
    atr_pct = features.get("atr_pct", 0.0)
    rel_vol = features.get("relative_volume", 1.0)
    close = features.get("close", 0.0)

    # Resolve days range
    if mode_days_range is None:
        mode_days_range = _periods_to_days_range(allowed_periods)
    min_days, max_days = mode_days_range

    # Pure intraday mode
    if max_days == 0:
        has_volatility = atr_pct >= volatility_config.ideal_min_atr_pct
        has_volume = rel_vol >= 1.5
        has_time = now_time < time(14, 0)
        if has_volatility and has_volume and has_time:
            return ("intraday", "MIS", 0)
        # Even in intraday mode, if conditions aren't met, still return intraday
        return ("intraday", "MIS", 0)

    # Compute dynamic holding days from ATR
    days = _estimate_holding_days(features, min_days, max_days)

    # For balanced mode: check if intraday is viable first
    if min_days == 0 and "intraday" in allowed_periods:
        has_volatility = atr_pct >= volatility_config.ideal_min_atr_pct
        has_volume = rel_vol >= 1.5
        has_time = now_time < time(14, 0)
        if has_volatility and has_volume and has_time:
            return ("intraday", "MIS", 0)

    # Classify into label
    label = _days_to_label(days)
    product = "MIS" if label == "intraday" else "CNC"
    return (label, product, days)


def _estimate_holding_days(
    features: dict[str, Any],
    min_days: int,
    max_days: int,
) -> int:
    """Estimate optimal holding days for a stock based on ATR and trend strength.

    Logic: days_to_target = target_move / avg_daily_move
    - Strong trend (EMA alignment) → can hold longer (target further out)
    - Weak/choppy → shorter hold
    - High ATR → moves faster → fewer days needed
    """
    atr_pct = features.get("atr_pct", 0.0)
    if atr_pct <= 0:
        return min_days

    # Trend strength from EMA alignment
    ema_9 = features.get("ema_9", 0)
    ema_21 = features.get("ema_21", 0)
    ema_50 = features.get("ema_50", 0)
    supertrend = features.get("supertrend_trend", 0)

    # Score trend alignment (0 to 1)
    trend_score = 0.0
    if ema_9 > 0 and ema_21 > 0 and ema_50 > 0:
        if ema_9 > ema_21 > ema_50:
            trend_score = 0.8  # bullish alignment
        elif ema_9 < ema_21 < ema_50:
            trend_score = 0.8  # bearish alignment
        elif ema_9 > ema_21:
            trend_score = 0.4  # partial alignment
        else:
            trend_score = 0.2  # choppy
    if supertrend != 0:
        trend_score = min(1.0, trend_score + 0.2)

    # Target multiplier based on trend: strong trends get wider targets
    # which means more days to reach them, but higher probability
    target_atr_mult = 2.0 + trend_score * 4.0  # 2x to 6x ATR

    # Days to reach target: target_move / daily_move
    # ATR approximates daily range, actual directional move ~50-70% of ATR
    directional_move_pct = atr_pct * 0.6
    target_move_pct = atr_pct * target_atr_mult
    raw_days = target_move_pct / directional_move_pct if directional_move_pct > 0 else min_days

    # Clamp to allowed range
    days = max(min_days, min(max_days, round(raw_days)))
    return days


def _days_to_label(days: int) -> str:
    """Convert expected holding days to a human-readable label."""
    if days == 0:
        return "intraday"
    elif days <= 5:
        return "short_term"
    else:
        return "long_term"


def _periods_to_days_range(periods: list[str]) -> tuple[int, int]:
    """Convert legacy period labels to a (min_days, max_days) range."""
    if not periods:
        return (0, 5)
    _label_days = {
        "intraday": 0,
        "3d": 3,
        "short_term": 3,
        "1w": 5,
        "long_term": 22,
    }
    day_values = [_label_days.get(p, 5) for p in periods]
    return (min(day_values), max(day_values))


def interpolate_atr_multipliers(
    days: int,
    holding_period_config: Any,
) -> tuple[float, float]:
    """Interpolate ATR target and SL multipliers for the given holding days.

    Uses the config's defined multipliers at anchor points (intraday, short_swing,
    week, long) and linearly interpolates between them.

    Returns:
        (target_multiplier, sl_multiplier)
    """
    # Build anchors from config
    anchors = [
        (0, holding_period_config.intraday.target, holding_period_config.intraday.stop_loss),
        (3, holding_period_config.short_swing.target, holding_period_config.short_swing.stop_loss),
        (5, holding_period_config.week.target, holding_period_config.week.stop_loss),
        (22, holding_period_config.long.target, holding_period_config.long.stop_loss),
    ]

    if days <= anchors[0][0]:
        return (anchors[0][1], anchors[0][2])
    if days >= anchors[-1][0]:
        return (anchors[-1][1], anchors[-1][2])

    # Find surrounding anchors and interpolate
    for i in range(len(anchors) - 1):
        d0, t0, s0 = anchors[i]
        d1, t1, s1 = anchors[i + 1]
        if d0 <= days <= d1:
            frac = (days - d0) / (d1 - d0) if d1 != d0 else 0
            target = t0 + frac * (t1 - t0)
            sl = s0 + frac * (s1 - s0)
            return (round(target, 3), round(sl, 3))

    # Fallback (shouldn't reach here)
    return (anchors[-1][1], anchors[-1][2])


def adjust_sell_for_holdings(
    signal_type: str,
    holding_period: str,
    product: str,
    symbol: str,
    held_symbols: set[str],
    expected_days: int = 0,
) -> tuple[str, str, int]:
    """Adjust SELL signals based on whether the user holds the stock.

    - If the user holds the stock, SELL can use any product/period (selling owned shares).
    - If the user does NOT hold the stock, it's a short sell — force to MIS/intraday
      (Indian equity rules: retail short selling must be squared off same day).
    - BUY signals are never affected.

    Args:
        signal_type: "BUY", "SELL", or "HOLD"
        holding_period: Current holding period decision (e.g. "short_term", "long_term")
        product: Current product decision (e.g. "CNC")
        symbol: Stock symbol
        held_symbols: Set of symbols the user currently holds (open positions + broker holdings)
        expected_days: Current expected holding days

    Returns:
        (holding_period, product, expected_days) — possibly overridden to ("intraday", "MIS", 0) for naked shorts
    """
    if signal_type != "SELL":
        return (holding_period, product, expected_days)

    if symbol in held_symbols:
        # User owns the stock — SELL is exiting a position, any product/period is fine
        return (holding_period, product, expected_days)

    # Short sell — must be intraday MIS (no overnight short positions for retail)
    return ("intraday", "MIS", 0)


def get_atr_multipliers(holding_period: str, holding_period_config: Any) -> Any:
    """Get ATR multipliers for the given holding period label from config.

    For backwards compatibility with code that uses discrete period labels.
    New code should prefer interpolate_atr_multipliers(days, config).
    """
    if holding_period == "intraday":
        return holding_period_config.intraday
    elif holding_period == "long_term":
        return holding_period_config.long
    elif holding_period in ("1w", "week"):
        return holding_period_config.week
    else:
        return holding_period_config.short_swing
