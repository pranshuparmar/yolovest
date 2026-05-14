"""Feature engineering — technical indicator computation.

Pure functions: list[OHLCVBar] → dict[str, float].
All indicators toggleable via strategy.indicators config.
No DB or network calls — easy to test.
"""

import math
from dataclasses import dataclass

from yolovest.models.schemas import OHLCVBar


@dataclass
class IndicatorConfig:
    """Which indicators to compute. Maps to strategy.indicators config."""

    rsi: bool = True
    macd: bool = True
    bollinger_bands: bool = True
    vwap: bool = True
    atr: bool = True
    volume_profile: bool = True
    obv: bool = True
    supertrend: bool = True
    ema_periods: list[int] | None = None

    def __post_init__(self) -> None:
        if self.ema_periods is None:
            self.ema_periods = [9, 21, 50, 200]


def compute_features(
    bars: list[OHLCVBar],
    config: IndicatorConfig | None = None,
) -> dict[str, float]:
    """Compute all enabled technical indicators from OHLCV bars.

    Returns a flat dict of feature_name → value (the features_snapshot).
    """
    if not bars:
        return {}

    cfg = config or IndicatorConfig()
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    volumes = [b.volume for b in bars]
    opens = [b.open for b in bars]

    features: dict[str, float] = {}

    # Always include latest price data — needed for entry/target/SL computation
    features["close"] = closes[-1]
    features["open"] = opens[-1]
    features["high"] = highs[-1]
    features["low"] = lows[-1]

    # Time-of-day signal. Intraday setups that work in the 9:15–11:00 morning
    # window often fail in the 11:30–14:00 chop zone and again differ in the
    # 14:00–15:30 hour. Daily bars don't have a meaningful time of day, so
    # the feature evaluates to 0 for those — model can treat that as
    # "ignore me" via tree splits. Last bar's timestamp drives the value.
    _bar_min = _minutes_since_open(bars[-1].timestamp)
    if _bar_min is not None:
        features["minutes_since_open"] = float(_bar_min)
        # Normalised 0..1 over a 375-min trading day so trees can split
        # cleanly across regimes (open, mid, close).
        features["day_phase"] = min(max(_bar_min / 375.0, 0.0), 1.0)

    if cfg.rsi:
        rsi = compute_rsi(closes, period=14)
        if rsi is not None:
            features["rsi_14"] = rsi

    if cfg.macd:
        macd_vals = compute_macd(closes)
        features.update(macd_vals)

    if cfg.bollinger_bands:
        bb = compute_bollinger_bands(closes, period=20)
        features.update(bb)

    if cfg.vwap:
        vwap = compute_vwap(bars)
        if vwap is not None:
            features["vwap"] = vwap

    if cfg.atr:
        atr = compute_atr(highs, lows, closes, period=14)
        if atr is not None:
            features["atr_14"] = atr
            features["atr_pct"] = atr / closes[-1] if closes[-1] > 0 else 0.0

    if cfg.volume_profile:
        vp = compute_volume_profile(volumes)
        features.update(vp)

    if cfg.obv:
        obv = compute_obv(closes, volumes)
        if obv is not None:
            features["obv"] = obv

    if cfg.supertrend:
        st = compute_supertrend(highs, lows, closes, period=10, multiplier=3.0)
        features.update(st)

    if cfg.ema_periods:
        for period in cfg.ema_periods:
            ema = compute_ema(closes, period)
            if ema is not None:
                features[f"ema_{period}"] = ema

    return features


def merge_feedback_features(
    features: dict[str, float],
    symbol: str,
    feedback_data: dict[str, dict[str, float]],
) -> None:
    """Merge per-symbol feedback stats into the feature dict (in-place).

    Adds rolling accuracy, PnL, slippage features from recent predictions,
    dry runs, and trades. Defaults to 0.5 (neutral) for missing data.
    """
    fb = feedback_data.get(symbol, {})
    has_data = bool(fb)

    features["fb_pred_accuracy"] = fb.get("pred_accuracy", 0.5)
    features["fb_pred_target_hit"] = fb.get("pred_target_hit_rate", 0.5)
    features["fb_pred_avg_pnl"] = fb.get("pred_avg_pnl_pct", 0.0)
    features["fb_dry_run_accuracy"] = fb.get("dry_run_accuracy", 0.5)
    features["fb_dry_run_avg_move"] = fb.get("dry_run_avg_move_pct", 0.0)
    features["fb_trade_win_rate"] = fb.get("trade_win_rate", 0.5)
    features["fb_trade_avg_pnl"] = fb.get("trade_avg_pnl", 0.0)
    features["fb_trade_avg_slippage"] = fb.get("trade_avg_slippage_pct", 0.0)
    features["fb_has_data"] = 1.0 if has_data else 0.0


# ------------------------------------------------------------------
# Individual indicators
# ------------------------------------------------------------------


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Relative Strength Index (Wilder's smoothing)."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, float]:
    """MACD line, signal line, and histogram."""
    result: dict[str, float] = {}
    if len(closes) < slow + signal:
        return result

    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)

    if fast_ema is None or slow_ema is None:
        return result

    # Align lengths
    min_len = min(len(fast_ema), len(slow_ema))
    macd_line = [fast_ema[-(min_len - i)] - slow_ema[-(min_len - i)] for i in range(min_len)]

    signal_ema = _ema_series(macd_line, signal)
    if signal_ema is None:
        return result

    result["macd_line"] = macd_line[-1]
    result["macd_signal"] = signal_ema[-1]
    result["macd_histogram"] = macd_line[-1] - signal_ema[-1]
    return result


def compute_bollinger_bands(
    closes: list[float], period: int = 20, num_std: float = 2.0
) -> dict[str, float]:
    """Bollinger Bands: upper, middle (SMA), lower."""
    result: dict[str, float] = {}
    if len(closes) < period:
        return result

    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)

    result["bb_upper"] = middle + num_std * std
    result["bb_middle"] = middle
    result["bb_lower"] = middle - num_std * std
    result["bb_width"] = (result["bb_upper"] - result["bb_lower"]) / middle if middle else 0
    return result


def compute_vwap(bars: list[OHLCVBar]) -> float | None:
    """Volume Weighted Average Price."""
    if not bars:
        return None
    total_vp = sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars)
    total_vol = sum(b.volume for b in bars)
    if total_vol == 0:
        return None
    return total_vp / total_vol


def compute_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float | None:
    """Average True Range."""
    if len(highs) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    atr = sum(true_ranges[:period]) / period
    for i in range(period, len(true_ranges)):
        atr = (atr * (period - 1) + true_ranges[i]) / period
    return atr


def compute_volume_profile(volumes: list[int]) -> dict[str, float]:
    """Relative volume (current vs average)."""
    result: dict[str, float] = {}
    if len(volumes) < 2:
        return result

    avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1
    if avg_vol > 0:
        result["relative_volume"] = volumes[-1] / avg_vol
    result["avg_volume"] = avg_vol
    return result


def compute_obv(closes: list[float], volumes: list[int]) -> float | None:
    """On-Balance Volume."""
    if len(closes) < 2:
        return None

    obv = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
    return obv


def compute_supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> dict[str, float]:
    """SuperTrend indicator — full implementation with band carryover.

    Tracks upper/lower bands across the entire bar series, carrying
    forward band values based on trend direction (not single-bar).
    """
    result: dict[str, float] = {}
    n = len(highs)
    if n < period + 1:
        return result

    # Compute ATR series using Wilder's smoothing
    true_ranges = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return result

    # ATR series (Wilder smoothing)
    atr_series = [sum(true_ranges[:period]) / period]
    for i in range(period, len(true_ranges)):
        atr_series.append((atr_series[-1] * (period - 1) + true_ranges[i]) / period)

    # Compute SuperTrend with band carryover
    # Start index in original data: period (since we skip first bar for TR)
    start = period
    upper_bands = [0.0] * (n - start)
    lower_bands = [0.0] * (n - start)
    trends = [1] * (n - start)  # 1 = bullish, -1 = bearish

    for j in range(n - start):
        idx = start + j
        atr_val = atr_series[j]
        hl2 = (highs[idx] + lows[idx]) / 2
        basic_upper = hl2 + multiplier * atr_val
        basic_lower = hl2 - multiplier * atr_val

        if j == 0:
            upper_bands[j] = basic_upper
            lower_bands[j] = basic_lower
            trends[j] = 1 if closes[idx] > basic_upper else -1
        else:
            # Upper band: carry forward (lower value) if previous close was below it
            prev_upper = upper_bands[j - 1]
            upper_bands[j] = (
                min(basic_upper, prev_upper) if closes[idx - 1] <= prev_upper else basic_upper
            )

            # Lower band: carry forward (higher value) if previous close was above it
            prev_lower = lower_bands[j - 1]
            lower_bands[j] = (
                max(basic_lower, prev_lower) if closes[idx - 1] >= prev_lower else basic_lower
            )

            # Determine trend
            prev_trend = trends[j - 1]
            if prev_trend == 1:
                # Was bullish: stay bullish unless close drops below lower band
                trends[j] = -1 if closes[idx] < lower_bands[j] else 1
            else:
                # Was bearish: stay bearish unless close rises above upper band
                trends[j] = 1 if closes[idx] > upper_bands[j] else -1

    result["supertrend_upper"] = upper_bands[-1]
    result["supertrend_lower"] = lower_bands[-1]
    result["supertrend_trend"] = float(trends[-1])
    return result


def compute_ema(values: list[float], period: int) -> float | None:
    """Exponential Moving Average — returns latest value."""
    if len(values) < period:
        return None
    series = _ema_series(values, period)
    return series[-1] if series else None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _ema_series(values: list[float], period: int) -> list[float] | None:
    """Compute full EMA series."""
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]

    for i in range(period, len(values)):
        ema.append((values[i] - ema[-1]) * multiplier + ema[-1])

    return ema


def _minutes_since_open(ts: str | None) -> int | None:
    """Minutes elapsed since 09:15 IST market open for the given timestamp.

    Returns None for daily bars or unparseable timestamps. Negative
    values (pre-market) are clamped to 0; post-close values keep
    rolling so the model can distinguish closing-auction-period bars.
    """
    if not ts:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Daily timestamps lack hour info → time component is 00:00:00,
    # which would always return -555 minutes (before 09:15). Treat as
    # daily bar with no time-of-day signal.
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return None
    minutes = dt.hour * 60 + dt.minute - (9 * 60 + 15)
    return max(0, minutes)
