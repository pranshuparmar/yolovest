"""Shared holding period decision logic.

Used by both the generate-signals skill (live pipeline) and the dry-run
endpoint (signal preview). Keeps the decision logic in one place.
"""

from datetime import time
from typing import Any


def decide_holding_period(
    features: dict[str, Any],
    allowed_periods: list[str],
    volatility_config: Any,
    now_time: time,
) -> tuple[str, str]:
    """Decide holding period and product type based on stock characteristics.

    Args:
        features: Computed technical features dict (must include atr_pct, relative_volume, EMAs, etc.)
        allowed_periods: List of allowed holding periods (e.g. ["intraday", "3d", "1w"])
        volatility_config: VolatilityConfig instance with min/max/ideal ATR% thresholds
        now_time: Current time (IST) for intraday time-of-day gating

    Returns:
        (holding_period, product) — e.g. ("intraday", "MIS") or ("1w", "CNC")
    """
    atr_pct = features.get("atr_pct", 0.0)
    rel_vol = features.get("relative_volume", 1.0)

    # Intraday: needs high volatility, high volume, and enough time before square-off
    if "intraday" in allowed_periods:
        has_volatility = atr_pct >= volatility_config.ideal_min_atr_pct
        has_volume = rel_vol >= 1.5
        has_time = now_time < time(14, 0)
        if has_volatility and has_volume and has_time:
            return ("intraday", "MIS")

    # 1-week: needs strong trend (EMA alignment) and SuperTrend confirming
    if "1w" in allowed_periods:
        ema_9 = features.get("ema_9", 0)
        ema_21 = features.get("ema_21", 0)
        ema_50 = features.get("ema_50", 0)
        supertrend = features.get("supertrend_trend", 0)

        bullish_trend = ema_9 > ema_21 > ema_50 > 0 and supertrend > 0
        bearish_trend = 0 < ema_9 < ema_21 < ema_50 and supertrend < 0
        moderate_vol = volatility_config.min_atr_pct <= atr_pct <= volatility_config.ideal_max_atr_pct

        if (bullish_trend or bearish_trend) and moderate_vol:
            return ("1w", "CNC")

    # 3-day swing: default fallback
    if "3d" in allowed_periods:
        return ("3d", "CNC")

    # Fall back to first allowed period
    period = allowed_periods[0] if allowed_periods else "intraday"
    product = "MIS" if period == "intraday" else "CNC"
    return (period, product)


def get_atr_multipliers(holding_period: str, holding_period_config: Any) -> Any:
    """Get ATR multipliers for the given holding period from config."""
    if holding_period == "intraday":
        return holding_period_config.intraday
    elif holding_period == "1w":
        return holding_period_config.week
    else:
        return holding_period_config.short_swing
