"""Shared holding period decision logic.

Used by both the generate-signals skill (live pipeline) and the dry-run
endpoint (signal preview). Keeps the decision logic in one place.

Holding periods are dynamic: the system computes expected_holding_days
per stock based on ATR%, trend strength, and volatility regime, clamped
to the mode's allowed range. A position-mix bias shortens durations when
existing portfolio positions are already long-dated.
"""

import logging
from datetime import time
from typing import Any

logger = logging.getLogger(__name__)

# Reference ATR% for "normal" volatility (mid-cap average).
# Stocks with higher ATR% need fewer days; lower ATR% need more.
_REFERENCE_ATR_PCT = 0.02  # 2%


def decide_holding_period(
    features: dict[str, Any],
    allowed_periods: list[str],
    volatility_config: Any,
    now_time: time,
    mode_days_range: tuple[int, int] | None = None,
    existing_positions: list[dict[str, Any]] | None = None,
    market_regime: str | None = None,
    bear_max_holding_days: int | None = None,
) -> tuple[str, str, int]:
    """Decide holding period, product type, and expected days based on stock characteristics.

    Args:
        features: Computed technical features dict (must include atr_pct, close, etc.)
        allowed_periods: List of allowed holding period labels
        volatility_config: VolatilityConfig instance with ATR% thresholds
        now_time: Current time (IST) for intraday time-of-day gating
        mode_days_range: (min_days, max_days) from the strategy mode config.
        existing_positions: Current open positions (for position-mix bias).
            Each dict should have "expected_holding_days" and optionally "product".
        market_regime: Current market regime ("bull", "bear", "range", or None).
        bear_max_holding_days: Cap holding days in bear markets (from config).

    Returns:
        (holding_period_label, product, expected_holding_days)
    """
    atr_pct = features.get("atr_pct", 0.0)
    rel_vol = features.get("relative_volume", 1.0)

    # Resolve days range
    if mode_days_range is None:
        mode_days_range = _periods_to_days_range(allowed_periods)
    min_days, max_days = mode_days_range

    # Pure intraday mode — always intraday regardless of conditions
    if max_days == 0:
        return ("intraday", "MIS", 0)

    # For balanced mode: check if intraday is viable
    if min_days == 0 and "intraday" in allowed_periods:
        if _is_intraday_viable(atr_pct, rel_vol, now_time, volatility_config):
            return ("intraday", "MIS", 0)

    # Compute dynamic holding days
    days = _estimate_holding_days(features, min_days, max_days)

    # Apply position-mix bias — shorten if portfolio is heavy on long positions
    if existing_positions:
        days = _apply_position_mix_bias(days, min_days, max_days, existing_positions)

    # Bear market cap — limit holding duration in downtrends
    if market_regime == "bear" and bear_max_holding_days is not None:
        days = min(days, bear_max_holding_days)

    # Classify into label
    label = _days_to_label(days)
    product = "MIS" if label == "intraday" else "CNC"
    return (label, product, days)


def _is_intraday_viable(
    atr_pct: float,
    rel_vol: float,
    now_time: time,
    volatility_config: Any,
) -> bool:
    """Check if intraday trading conditions are met.

    Relaxed from the original: requires decent volatility OR high volume
    (not both), and allows until 2:30 PM instead of 2:00 PM.

    Hard eligibility cap (`max_atr_pct_for_intraday_eligibility`): refuses
    intraday for stocks too volatile to square off in a half-day session
    (e.g. 11%+ daily ATR small-caps). Returns False so the caller routes
    to swing instead.
    """
    eligibility_cap = getattr(
        volatility_config, "max_atr_pct_for_intraday_eligibility", 0.0,
    )
    if eligibility_cap > 0 and atr_pct > eligibility_cap:
        return False

    has_volatility = atr_pct >= volatility_config.min_atr_pct  # 0.5% min (was ideal 1.5%)
    has_good_volatility = atr_pct >= volatility_config.ideal_min_atr_pct  # 1.5% ideal
    has_volume = rel_vol >= 1.2  # relaxed from 1.5
    has_time = now_time < time(14, 30)  # relaxed from 14:00

    if not has_time:
        return False
    if not has_volatility:
        return False
    # Either good volatility OR good volume (not both required)
    return has_good_volatility or has_volume


def _estimate_holding_days(
    features: dict[str, Any],
    min_days: int,
    max_days: int,
) -> int:
    """Estimate optimal holding days based on ATR%, trend strength, and volatility.

    Key fix: ATR now actually affects the result. High-ATR stocks move faster
    and get shorter holding periods. Low-ATR stocks need longer to reach targets.

    Formula: base_days (from trend) × volatility_factor (from ATR)
    """
    atr_pct = features.get("atr_pct", 0.0)
    if atr_pct <= 0:
        return min_days

    # --- Trend strength (drives base duration) ---
    trend_score = _compute_trend_score(features)

    # Base days from trend: weak trends → short hold, strong trends → longer hold
    # Range: 2 days (choppy, trend_score=0) to 10 days (strong trend, trend_score=1)
    base_days = 2 + trend_score * 8

    # --- Volatility factor (high ATR → fewer days needed) ---
    # A stock with 3% ATR moves 3× faster than one with 1% ATR,
    # so it needs ~1/3 the time to reach the same ATR-multiple target.
    volatility_factor = _REFERENCE_ATR_PCT / max(atr_pct, 0.003)
    # Clamp factor to [0.4, 2.5] to prevent extremes
    volatility_factor = max(0.4, min(2.5, volatility_factor))

    raw_days = base_days * volatility_factor

    # --- RSI-based adjustment ---
    # Oversold/overbought stocks are likely to revert faster
    rsi = features.get("rsi_14") or features.get("rsi")
    if rsi is not None:
        if rsi < 30 or rsi > 70:
            raw_days *= 0.7  # strong mean-reversion signal → shorter hold

    # Clamp to allowed range
    days = max(min_days, min(max_days, round(raw_days)))
    return days


def _compute_trend_score(features: dict[str, Any]) -> float:
    """Score trend alignment from 0 (choppy) to 1 (strong trending)."""
    ema_9 = features.get("ema_9", 0)
    ema_21 = features.get("ema_21", 0)
    ema_50 = features.get("ema_50", 0)
    ema_200 = features.get("ema_200", 0)
    supertrend = features.get("supertrend_trend") or features.get("supertrend_direction", 0)

    score = 0.0

    if ema_9 > 0 and ema_21 > 0 and ema_50 > 0:
        if ema_9 > ema_21 > ema_50:
            score = 0.6  # bullish alignment
        elif ema_9 < ema_21 < ema_50:
            score = 0.6  # bearish alignment
        elif ema_9 > ema_21:
            score = 0.3  # partial alignment
        else:
            score = 0.1  # choppy

        # Bonus for EMA-200 alignment (strong long-term trend)
        if ema_200 > 0:
            if (ema_9 > ema_200 and ema_50 > ema_200) or (ema_9 < ema_200 and ema_50 < ema_200):
                score = min(1.0, score + 0.2)

    if supertrend != 0:
        score = min(1.0, score + 0.15)

    return score


def _apply_position_mix_bias(
    days: int,
    min_days: int,
    max_days: int,
    positions: list[dict[str, Any]],
) -> int:
    """Bias holding period toward capital rotation when portfolio is heavy on long trades.

    If existing positions are mostly long-dated, reduce the new trade's
    holding period to keep capital turning over. If positions are mostly
    short-dated, allow longer holds.
    """
    if not positions:
        return days

    # Compute average remaining holding days of existing positions
    total_expected = 0
    count = 0
    for pos in positions:
        exp_days = pos.get("expected_holding_days")
        if exp_days is not None and exp_days > 0:
            total_expected += exp_days
            count += 1

    if count == 0:
        return days

    avg_existing_days = total_expected / count

    # If average existing holding is > 5 days, bias new trade shorter
    # If average existing holding is < 3 days, allow new trade to be longer
    if avg_existing_days > 5:
        # Reduce by 20-40% based on how heavy the portfolio is
        reduction = min(0.4, (avg_existing_days - 5) * 0.05)
        days = max(min_days, round(days * (1 - reduction)))
    elif avg_existing_days < 3 and days < 3:
        # Portfolio is very short-dated, allow this trade to go a bit longer
        days = min(max_days, days + 1)

    return days


def _days_to_label(days: int) -> str:
    """Convert expected holding days to a human-readable label."""
    if days == 0:
        return "intraday"
    elif days <= 2:
        return "short_term"
    elif days <= 5:
        return "swing"
    elif days <= 15:
        return "positional"
    else:
        return "long_term"


def _periods_to_days_range(periods: list[str]) -> tuple[int, int]:
    """Convert legacy period labels to a (min_days, max_days) range."""
    if not periods:
        return (0, 5)
    _label_days = {
        "intraday": 0,
        "short_term": 2,
        "3d": 3,
        "swing": 5,
        "1w": 5,
        "positional": 15,
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


def interpolate_atr_pct_cap(days: int, holding_period_config: Any) -> float:
    """Interpolate the `max_atr_pct_for_target` ATR cap for the holding days.

    Mirrors `interpolate_atr_multipliers` so the geometry cap tracks the
    same anchor points (intraday / short_swing / week / long). 0 at every
    anchor means the cap is disabled for that horizon.
    """
    anchors = [
        (0, holding_period_config.intraday.max_atr_pct_for_target),
        (3, holding_period_config.short_swing.max_atr_pct_for_target),
        (5, holding_period_config.week.max_atr_pct_for_target),
        (22, holding_period_config.long.max_atr_pct_for_target),
    ]
    if days <= anchors[0][0]:
        return anchors[0][1]
    if days >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        d0, c0 = anchors[i]
        d1, c1 = anchors[i + 1]
        if d0 <= days <= d1:
            frac = (days - d0) / (d1 - d0) if d1 != d0 else 0
            return round(c0 + frac * (c1 - c0), 4)
    return anchors[-1][1]


def apply_session_caps(
    signal_type: str,
    target: float,
    stop_loss: float,
    quote: dict[str, Any],
) -> tuple[float, float, list[str]]:
    """Constrain target/SL by exchange circuit limits.

    Only the upper/lower circuit limits are real forward boundaries —
    orders at or beyond them physically cannot fill. Today's session
    high/low are intentionally not enforced (they're current extremes,
    not forward caps; breakouts past them are legitimate).

    Caps applied (for BUY; SELL mirrors):
      - target < upper_circuit (hard cap at 99% of circuit)
      - stop_loss > lower_circuit (hard floor at 101% of circuit)

    Returns:
        (new_target, new_stop_loss, list_of_adjustment_messages)

    No-op for any field missing from `quote`.
    """
    upper_circuit = quote.get("upper_circuit")
    lower_circuit = quote.get("lower_circuit")
    adjustments: list[str] = []

    if signal_type == "BUY":
        if upper_circuit and target >= upper_circuit:
            new_target = upper_circuit * 0.99
            adjustments.append(
                f"target {target:.2f} → {new_target:.2f} (upper circuit {upper_circuit:.2f})"
            )
            target = new_target
        if lower_circuit and stop_loss <= lower_circuit:
            new_sl = lower_circuit * 1.01
            adjustments.append(
                f"SL {stop_loss:.2f} → {new_sl:.2f} (lower circuit {lower_circuit:.2f})"
            )
            stop_loss = new_sl

    elif signal_type == "SELL":
        if lower_circuit and target <= lower_circuit:
            new_target = lower_circuit * 1.01
            adjustments.append(
                f"target {target:.2f} → {new_target:.2f} (lower circuit {lower_circuit:.2f})"
            )
            target = new_target
        if upper_circuit and stop_loss >= upper_circuit:
            new_sl = upper_circuit * 0.99
            adjustments.append(
                f"SL {stop_loss:.2f} → {new_sl:.2f} (upper circuit {upper_circuit:.2f})"
            )
            stop_loss = new_sl

    return target, stop_loss, adjustments


def adjust_sell_for_holdings(
    signal_type: str,
    holding_period: str,
    product: str,
    symbol: str,
    held_symbols: set[str],
    expected_days: int = 0,
) -> tuple[str, str, int] | None:
    """Adjust SELL signals based on whether the user holds the stock.

    - If the user holds the stock, SELL can use any product/period
      (selling owned shares).
    - If the user does NOT hold the stock, it's a short sell. Indian
      retail rules require intraday/MIS — but we only ALLOW that when
      the per-symbol decision already chose `holding_period ==
      "intraday"`. A swing-model SELL converted to intraday would mix
      geometries: swing ATR-multiplier target/SL clamped onto an
      intraday horizon. Dropping is the right call instead.
    - BUY signals are never affected.

    Mode interaction (informational — this function checks
    `holding_period` directly rather than the strategy mode):
      * `intraday` mode: every signal already decides intraday →
        all non-held SELLs route through as MIS shorts. (unchanged)
      * `balanced` mode: the per-symbol balanced predictor picks
        intraday vs swing. Only intraday-winners survive a non-held
        SELL; swing-winners are dropped.
      * `short_term` / `long_term` modes: holding_period is never
        "intraday" → all non-held SELLs are dropped.

    Returns:
        (holding_period, product, expected_days), or None when the
        signal should be dropped because a non-held SELL on a swing
        horizon would have to be converted to intraday/MIS.
    """
    if signal_type != "SELL":
        return (holding_period, product, expected_days)

    if symbol in held_symbols:
        return (holding_period, product, expected_days)

    # Non-held SELL = short candidate. Only keep when the per-symbol
    # decision already chose intraday — otherwise we'd be silently
    # repurposing a swing setup as an intraday short.
    if holding_period != "intraday":
        return None

    return ("intraday", "MIS", 0)


def get_atr_multipliers(holding_period: str, holding_period_config: Any) -> Any:
    """Get ATR multipliers for the given holding period label from config.

    For backwards compatibility with code that uses discrete period labels.
    New code should prefer interpolate_atr_multipliers(days, config).
    """
    if holding_period == "intraday":
        return holding_period_config.intraday
    elif holding_period in ("long_term", "positional"):
        return holding_period_config.long
    elif holding_period in ("1w", "week", "swing"):
        return holding_period_config.week
    else:
        return holding_period_config.short_swing
