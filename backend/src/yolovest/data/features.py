"""Feature engineering — technical indicator computation.

Pure functions: list[OHLCVBar] → dict[str, float].
All indicators toggleable via strategy.indicators config.
No DB or network calls — easy to test.
"""

import math
from dataclasses import dataclass

from yolovest.models.schemas import OHLCVBar
from yolovest.timezone import IST

# Bump this whenever a change makes a previously-trained model artifact
# unsafe to load against the current code: the feature vocabulary changes
# (add/rename/remove a feature the model trains on), feature *ordering*
# semantics change, or the label geometry in model_retrain changes. The
# value is stamped into every saved artifact (`ml_signal.save_model`) and
# checked when importing a model trained on another machine
# (`POST /api/ml-models/import`) so a stale .pkl fails loudly instead of
# silently feeding the model the wrong inputs (missing features resolve to
# 0.0 at inference — no crash, just garbage predictions).
#
# History:
#   1 — initial schema versioning (base TA + sector/regime/institutional/
#       time/news/vix/fno/feedback features, path-aware labels).
#   2 — intraday gains higher-timeframe daily-trend features
#       (daily_ema9_vs_ema21_pct, daily_close_vs_ema50_pct) and
#       session-relative features (session_vwap_dist_pct, session_orb_pos).
MODEL_SCHEMA_VERSION = 2

# Feature keys that compute_features emits but the ML model should NOT
# see. These are raw absolute prices, raw cumulative levels, or raw
# indicator bands that don't transfer across stocks at different price
# levels (a stock at ₹150 vs ₹3000 has wildly different absolute close,
# OBV, EMA, BB band values — the model can't learn generalizable patterns
# from them). They stay in the features dict because the inference layer
# (`ml_signal.py::_predict`) reads `close` and `atr_14` for entry-price
# and ATR fallbacks. `model_retrain._prepare_training_data` filters this
# set out of `feature_names`, so the trained model never sees them.
MODEL_FEATURE_EXCLUSIONS: frozenset[str] = frozenset({
    # Raw OHLC — model uses normalized derivatives (range_pct, body_pct, etc.)
    "close", "open", "high", "low",
    # Raw indicator bands / cumulative values — model uses normalized
    # derivatives instead (bb_position, vwap_distance_pct, obv_change_5d).
    # Legacy `vwap` is summed over the entire bars window — it's a year-long
    # P/V average, not session VWAP. `mvwap_20d` is the proper 20-day
    # rolling replacement (added alongside for forward compatibility;
    # new model retrains pick up `mvwap_20d_distance_pct` automatically).
    "vwap", "mvwap_20d", "atr_14", "obv",
    "bb_upper", "bb_middle", "bb_lower",
    "supertrend_upper", "supertrend_lower",
    # Raw EMA levels — model uses close_vs_ema*_pct ratios.
    "ema_9", "ema_21", "ema_50", "ema_200",
    # Raw MACD components scale with price — model uses macd_histogram_pct.
    "macd_line", "macd_signal", "macd_histogram",
    # Absolute average volume — model uses relative_volume / volume_zscore_20d.
    "avg_volume",
})


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
        # 20-day MVWAP — the actually-useful price/volume anchor on
        # daily bars. Kept alongside the legacy whole-window vwap so
        # production models trained against the old feature don't
        # silently see a distribution shift at inference (compute_features
        # runs both, but model artifacts only consume the keys they
        # were trained on).
        mvwap = compute_mvwap_20d(bars)
        if mvwap is not None:
            features["mvwap_20d"] = mvwap

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

    # Normalized derivatives — the features the ML model actually sees.
    # All of these are price-invariant ratios or % deltas, so they
    # generalize across stocks at different absolute price levels.
    _last_close = closes[-1]
    if _last_close > 0:
        features["range_pct"] = (highs[-1] - lows[-1]) / _last_close
        features["body_pct"] = (
            (closes[-1] - opens[-1]) / opens[-1] if opens[-1] > 0 else 0.0
        )
        if len(closes) >= 2 and closes[-2] > 0:
            features["gap_pct"] = (opens[-1] - closes[-2]) / closes[-2]
            features["close_change_pct"] = (closes[-1] - closes[-2]) / closes[-2]

        # Indicator-level → close-distance ratios. Each "raw level"
        # feature gets a corresponding _pct version that's
        # price-invariant. Trees can split on these meaningfully
        # across the full Nifty 500 universe.
        if "vwap" in features and features["vwap"] > 0:
            features["vwap_distance_pct"] = (
                (_last_close - features["vwap"]) / features["vwap"]
            )
        # 20-day MVWAP distance — meaningful price/volume anchor for
        # daily bars (institutional accumulation/distribution zone over
        # the last month). The legacy vwap_distance_pct is preserved
        # above so existing model artifacts still find their key, but
        # new retrains will pick this up as a separate feature.
        if "mvwap_20d" in features and features["mvwap_20d"] > 0:
            features["mvwap_20d_distance_pct"] = (
                (_last_close - features["mvwap_20d"]) / features["mvwap_20d"]
            )
        if "bb_upper" in features and "bb_lower" in features:
            band_width = features["bb_upper"] - features["bb_lower"]
            if band_width > 0:
                # 0 = at lower band, 1 = at upper, can exceed [0,1] outside bands.
                features["bb_position"] = (
                    (_last_close - features["bb_lower"]) / band_width
                )
        if "macd_histogram" in features:
            features["macd_histogram_pct"] = features["macd_histogram"] / _last_close
        if "macd_line" in features:
            features["macd_line_pct"] = features["macd_line"] / _last_close
        if cfg.ema_periods:
            for period in cfg.ema_periods:
                key = f"ema_{period}"
                if key in features and features[key] > 0:
                    features[f"close_vs_{key}_pct"] = (
                        (_last_close - features[key]) / features[key]
                    )
        # Fast vs slow EMA cross-over magnitude (normalised).
        if "ema_9" in features and "ema_21" in features and features["ema_21"] > 0:
            features["ema_9_vs_21_pct"] = (
                (features["ema_9"] - features["ema_21"]) / features["ema_21"]
            )
        if "supertrend_upper" in features and "supertrend_lower" in features:
            trend = features.get("supertrend_trend", 0.0)
            # When bullish (trend=+1), lower band is the active SL line; when
            # bearish, upper band is the active resistance. Distance from the
            # active band is the meaningful signal.
            active = (
                features["supertrend_lower"] if trend >= 0
                else features["supertrend_upper"]
            )
            if active > 0:
                features["supertrend_distance_pct"] = (
                    (_last_close - active) / active
                )

    # OBV normalization: raw OBV is a cumulative sum that drifts
    # unboundedly. The 5-bar % change captures recent volume-direction
    # momentum, which is what users actually look at.
    if "obv" in features and len(closes) >= 6:
        obv_now = features["obv"]
        obv_5_ago = compute_obv(closes[:-5], volumes[:-5])
        if obv_5_ago is not None and obv_5_ago != 0:
            features["obv_change_5d_pct"] = (obv_now - obv_5_ago) / abs(obv_5_ago)

    # Volume z-score: stock-relative spike detector. Raw avg_volume is
    # nonsense across the universe; (today - mean_20) / std_20 is
    # invariant. Uses up to the last 20 bars including today's; if the
    # standard deviation is zero (perfectly flat), returns 0.
    if len(volumes) >= 5:
        window = volumes[-20:]
        if len(window) >= 5:
            mu = sum(window) / len(window)
            var = sum((v - mu) ** 2 for v in window) / len(window)
            sigma = math.sqrt(var)
            if sigma > 0:
                features["volume_zscore_20d"] = (volumes[-1] - mu) / sigma
            else:
                features["volume_zscore_20d"] = 0.0

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
    # Recent-loss count (over the feedback lookback window). Lets the
    # model deprioritise symbols that have been bleeding lately — same
    # data agent_memory tracks but exposed as a model feature so the
    # learned scoring can react automatically rather than via hardcoded
    # cooldowns alone.
    features["fb_recent_loss_count"] = fb.get("trade_loss_count", 0.0)
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
    """Volume Weighted Average Price.

    LEGACY: this sums across the ENTIRE bars list (typically 365 daily
    bars in the heartbeat path) — it's effectively a year-long P/V
    weighted average, not session VWAP. Retained so existing model
    artifacts keep finding `vwap_distance_pct` at inference. New
    training should consume `mvwap_20d_distance_pct` from
    compute_mvwap_20d below.
    """
    if not bars:
        return None
    total_vp = sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars)
    total_vol = sum(b.volume for b in bars)
    if total_vol == 0:
        return None
    return total_vp / total_vol


def compute_mvwap_20d(bars: list[OHLCVBar]) -> float | None:
    """20-day moving Volume Weighted Average Price.

    Proper price/volume anchor for daily-bar models — captures where
    institutional accumulation / distribution has happened over the
    last month. The 20-bar window matches the Bollinger period and
    is short enough that the feature reacts to regime shifts, long
    enough that one outlier session doesn't dominate.

    Falls back to None when fewer than 5 bars are available to
    prevent a degenerate same-day VWAP from sneaking through.
    """
    if not bars or len(bars) < 5:
        return None
    window = bars[-20:] if len(bars) >= 20 else bars
    total_vp = sum(((b.high + b.low + b.close) / 3) * b.volume for b in window)
    total_vol = sum(b.volume for b in window)
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


DAILY_TREND_FEATURE_KEYS: tuple[str, ...] = (
    "daily_ema9_vs_ema21_pct",
    "daily_close_vs_ema50_pct",
)


def daily_trend_features_series(closes: list[float]) -> list[dict[str, float]]:
    """Per-index higher-timeframe (daily) trend features.

    Element ``i`` is the trend context derived from ``closes[:i+1]`` — i.e.
    using only data up to and including bar ``i``. Equivalent to calling
    :func:`compute_daily_trend_features` on each prefix, but runs the EMA
    passes once (the intraday retrain precomputes per (symbol, date) this
    way to avoid O(n^2) cost). Because ``_ema_series`` seeds from the first
    ``period`` values and is purely recursive, the EMA at index ``i`` is
    identical whether computed over the full series or the prefix — so the
    one-pass precompute matches the per-prefix inference call exactly.

    Both features are price-invariant ratios (transfer across the universe)
    and default to 0.0 (neutral) when the series is too short.
    """
    n = len(closes)
    out = [
        {"daily_ema9_vs_ema21_pct": 0.0, "daily_close_vs_ema50_pct": 0.0}
        for _ in range(n)
    ]
    e9 = _ema_series(closes, 9)
    e21 = _ema_series(closes, 21)
    e50 = _ema_series(closes, 50)
    for i in range(n):
        if e9 is not None and e21 is not None and i >= 20:
            b = e21[i - 20]
            if b > 0:
                out[i]["daily_ema9_vs_ema21_pct"] = (e9[i - 8] - b) / b
        if e50 is not None and i >= 49:
            c = e50[i - 49]
            if c > 0:
                out[i]["daily_close_vs_ema50_pct"] = (closes[i] - c) / c
    return out


def compute_daily_trend_features(closes: list[float]) -> dict[str, float]:
    """Daily trend context for the most recent close in ``closes``.

    Used at inference (intraday path) on the prior-session daily window so
    the intraday model can condition entries on the higher-timeframe trend
    rather than 5-min noise alone. Symmetric with the retrain precompute
    via :func:`daily_trend_features_series`.
    """
    if not closes:
        return {k: 0.0 for k in DAILY_TREND_FEATURE_KEYS}
    return daily_trend_features_series(closes)[-1]


SESSION_FEATURE_KEYS: tuple[str, ...] = (
    "session_vwap_dist_pct",
    "session_orb_pos",
)

# Decision bars forming the opening range (3 × 5-min ≈ first 15 minutes).
_ORB_BARS = 3


def compute_session_features(bars: list[OHLCVBar]) -> dict[str, float]:
    """Intraday session-relative features for the LAST bar in ``bars``.

    Two price-invariant signals the rolling multi-day technicals miss
    because they average across sessions:

    - ``session_vwap_dist_pct``: distance of the latest close from today's
      running session VWAP (the canonical intraday reference price).
    - ``session_orb_pos``: position of the latest close within the opening
      range, normalised by the range width — >0.5 broke above the open
      range, <-0.5 broke below.

    Computed only from bars sharing the last bar's IST calendar date (the
    current session), so it's identical in training and inference given the
    same 5-min window. Neutral 0.0 when the session is too short. (Intraday
    sessions never cross an IST/UTC date boundary, so the grouping is robust
    to whichever tz the bars carry.)
    """
    out = {k: 0.0 for k in SESSION_FEATURE_KEYS}
    if not bars:
        return out

    def _session_date(b: OHLCVBar):
        ts = b.timestamp
        return (ts.astimezone(IST) if ts.tzinfo is not None else ts).date()

    last = bars[-1]
    last_date = _session_date(last)
    session = [b for b in bars if _session_date(b) == last_date]
    try:
        close = float(last.close)
    except (TypeError, ValueError):
        return out
    if not session or close <= 0:
        return out

    pv = vol = 0.0
    for b in session:
        v = float(b.volume or 0.0)
        tp = (float(b.high) + float(b.low) + float(b.close)) / 3.0
        pv += tp * v
        vol += v
    if vol > 0 and pv > 0:
        vwap = pv / vol
        if vwap > 0:
            out["session_vwap_dist_pct"] = (close - vwap) / vwap

    if len(session) >= _ORB_BARS:
        opening = session[:_ORB_BARS]
        or_high = max(float(b.high) for b in opening)
        or_low = min(float(b.low) for b in opening)
        rng = or_high - or_low
        if rng > 0:
            or_mid = (or_high + or_low) / 2.0
            out["session_orb_pos"] = (close - or_mid) / rng
    return out


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
