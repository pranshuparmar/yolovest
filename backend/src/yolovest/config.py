"""Configuration system for YoloVest.

Nested Pydantic v2 models matching config.yaml structure.
Supports environment variable expansion for secrets (${VAR_NAME}).
"""

import os
import re
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator


def _parse_time(t: str) -> dt_time:
    """Parse HH:MM string to datetime.time for safe comparison."""
    parts = t.strip().split(":")
    return dt_time(int(parts[0]), int(parts[1]))


def _load_dotenv(config_dir: Path) -> None:
    """Load .env file into os.environ if present. Does not override existing vars."""
    for candidate in [config_dir / ".env", Path(".env")]:
        if candidate.is_file():
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    os.environ.setdefault(key, value)
            break


def _expand_env_vars(value: object) -> object:
    """Recursively expand ${VAR_NAME} patterns with environment variable values.

    If an environment variable is not set, the placeholder is left unchanged.
    """
    if isinstance(value, str):
        pattern = re.compile(r"\$\{(\w+)\}")

        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            env_val = os.environ.get(var_name)
            if env_val is None:
                return match.group(0)  # leave unexpanded
            return env_val

        return pattern.sub(replacer, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Config sub-models
# ---------------------------------------------------------------------------


class CapitalConfig(BaseModel):
    initial_amount: float = Field(default=100_000, gt=0)


class BrokerConfig(BaseModel):
    api_key: SecretStr = SecretStr("")
    api_secret: SecretStr = SecretStr("")


class LLMConfig(BaseModel):
    enabled: bool = False
    model: str = "gemini-2.5-flash"
    api_key: SecretStr = SecretStr("")


class MarketDataConfig(BaseModel):
    daily_provider: str = "jugaad"
    daily_fallback: str = "yfinance"
    intraday_provider: str = "tvdatafeed"
    kite_data_enabled: bool = False  # enable Kite Connect as data provider
    # Collect order-book depth snapshots (one batched Kite quote call per
    # heartbeat across the watchlist) into the depth_snapshots table.
    # Pure data collection — nothing trades on it. This builds the
    # dataset that can eventually make an intraday model viable:
    # bar-derived features rank intraday outcomes (AUC ~0.58) but can't
    # pay intraday costs; order-flow imbalance is the feature class that
    # can. No-ops unless kite_data_enabled and the broker is authed.
    depth_snapshots_enabled: bool = True
    # Self-pruned retention for the snapshots (>= ~13 months keeps a
    # year of history plus headroom for the eventual training window).
    depth_snapshot_retention_days: int = Field(default=400, ge=30)
    # KiteTicker WebSocket for sub-second LTP cache. Requires the paid
    # Kite data plan and a valid access token. Position-monitor uses
    # the cached price first, falling back to REST when stale or
    # missing. Off by default — opt-in until tested in the user's env.
    kite_websocket_enabled: bool = False
    news_enabled: bool = True  # fetch news from MoneyControl, ET Markets, LiveMint
    scrapers_enabled: bool = True  # fetch from Screener.in, Trendlyne, Google Finance, NSE, economic calendar
    bhavcopy_dir: str = "./data/bhavcopy"
    cache_ttl_minutes: int = 15
    stale_threshold_minutes: int = 30
    sentiment_ttl_hours: int = 48  # sentiment older than this is ignored in scanning
    backfill_days: int = 1095  # daily-bar history window for backfill-data and ingest-universe
    # 5-minute-bar history window for backfill-intraday. Also acts as the
    # intraday retention floor (database-maintenance won't prune intraday
    # bars newer than this), so raising it for the intraday model (e.g. 730)
    # keeps the deep backfill from being trimmed back to intraday_ohlcv_days.
    intraday_backfill_days: int = 365
    # Reject a symbol from signal generation when its latest daily bar
    # is older than this many trading days behind the most recent
    # completed trading day. Default 1 covers the normal "mid-session
    # ingest just hasn't run yet" case while still catching the
    # FEDERALBNK-style "data is 2+ trading days stale" silence the
    # YFinance provider used to flow through unchecked. 0 = strict
    # (latest bar must be the most recent completed session). Set to
    # a high number to disable the gate.
    max_signal_data_age_trading_days: int = 1


class HeartbeatConfig(BaseModel):
    market_hours_interval_min: int = 15
    off_hours_interval_min: int = 60
    max_consecutive_skips: int = 3
    auth_broker_cron: str = "30 8 * * 1-5"  # daily broker re-auth
    ingest_premarket_cron: str = "30 8 * * 1-5"  # pre-market data fetch


class ScanningWeights(BaseModel):
    technical: float = 0.35
    volume_momentum: float = 0.25
    news_sentiment: float = 0.15
    fundamental: float = 0.15
    volatility: float = 0.10

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "ScanningWeights":
        total = (
            self.technical + self.volume_momentum + self.news_sentiment
            + self.fundamental + self.volatility
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Scanning weights must sum to 1.0, got {total:.4f}"
            )
        return self


class ScanningConfig(BaseModel):
    universe: str = "nifty500"  # "nifty50" | "nifty100" | "nifty200" | "nifty500"
    universe_cron: str = "30 8 * * 1-5"  # daily 8:30 AM IST on weekdays
    seed_symbols: list[str] = Field(
        default_factory=lambda: ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    )
    shortlist_size: int = 500
    min_avg_daily_volume: int = 500_000
    weights: ScanningWeights = Field(default_factory=ScanningWeights)
    # Watchlist rotation: evict symbols that fail to produce an actionable
    # signal for N consecutive *heartbeats with model evaluation*, then
    # apply a cooldown so market-scan doesn't immediately re-add them.
    # Default disabled — the threshold + cooldown defaults below are
    # safe values for opt-in users, but the feature is fundamentally a
    # foot-gun on broad universes: a 500-stock screener legitimately
    # produces no signal for most symbols most heartbeats, so any
    # aggressive cooldown ends up benching the whole universe within
    # a few hours. Re-enable only if you understand the trade-off.
    rotation_enabled: bool = False
    # Days (not heartbeats) of consecutive no-signal before benching.
    # 1 trading day ≈ 26 market-hours heartbeats — was 8, which is
    # 2 hours, far too aggressive.
    rotation_no_signal_threshold: int = Field(default=50, ge=1, le=500)
    rotation_cooldown_hours: int = Field(default=12, ge=1, le=72)


class IndicatorsConfig(BaseModel):
    rsi: bool = True
    macd: bool = True
    bollinger_bands: bool = True
    vwap: bool = True
    atr: bool = True
    volume_profile: bool = True
    obv: bool = True
    supertrend: bool = True
    # Multi-horizon momentum + volatility-regime + fractional-difference
    # features for the SWING/daily model (3-9mo momentum is the strongest
    # Indian-equity anomaly). Daily-only — meaningless on 5-min intraday
    # bars, so the intraday feature set is unchanged. Default on; flip off
    # to A/B against a price-snapshot-only swing model.
    extended_momentum: bool = True


class ATRMultipliers(BaseModel):
    """ATR multipliers for target and stop-loss calculation."""

    target: float = Field(default=2.0, gt=0)
    stop_loss: float = Field(default=1.0, gt=0)
    # Cap on the ATR value (as a fraction of entry price) used when
    # computing target/SL distance. Only relevant for the intraday
    # bucket today — a high-ATR stock (e.g. JAINREC at ~11.6% daily
    # ATR) gets a 6-7% intraday target with the default 0.6× multiplier,
    # which is unreachable in a half-day session. Clamping caps the
    # implied target distance at ~max_atr_pct × multiplier (e.g. 0.035
    # × 0.6 = ~2.1% max target distance). 0 = no cap (legacy behaviour).
    max_atr_pct_for_target: float = Field(default=0.0, ge=0, le=0.5)


class HoldingPeriodConfig(BaseModel):
    """ATR multipliers per holding period for target/SL sizing.

    For dynamic holding periods, multipliers are interpolated between the
    nearest defined buckets based on expected_holding_days.
    """

    intraday: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(
            target=0.6, stop_loss=0.3,
            # 3.5% caps the implied target distance at ~2.1% for high-ATR
            # stocks. Median NSE large-caps have 1-2% daily ATR and stay
            # well under the cap; only the volatile tail (small caps,
            # F&O momentum names) gets clamped.
            max_atr_pct_for_target=0.035,
        ),
    )
    # Swing/CNC buckets also cap the ATR used for geometry so a noisy or
    # corrupt ATR can't emit an unreachable target / blown-out SL. Wider
    # than intraday since multi-day holds tolerate larger moves.
    short_swing: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(
            target=1.5, stop_loss=0.75, max_atr_pct_for_target=0.06,
        ),
    )
    week: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(
            target=2.5, stop_loss=1.2, max_atr_pct_for_target=0.08,
        ),
    )
    long: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(
            target=5.0, stop_loss=2.0, max_atr_pct_for_target=0.12,
        ),
    )


class VolatilityConfig(BaseModel):
    """Volatility thresholds for stock selection and holding period decisions.

    ATR% = ATR / price. A stock with 2% ATR% moves ~2% per day on average.
    """

    min_atr_pct: float = Field(default=0.005, ge=0)
    max_atr_pct: float = Field(default=0.05, gt=0)
    ideal_min_atr_pct: float = Field(default=0.015, ge=0)
    ideal_max_atr_pct: float = Field(default=0.03, gt=0)
    # Hard eligibility cap for the intraday bucket. When `atr_pct >
    # this`, the stock is refused as intraday material — either routed
    # to swing (balanced mode where swing is allowed), or dropped
    # outright (pure intraday strategy mode). Different lever from
    # `max_atr_pct_for_target` on the multipliers config: that one
    # *caps* the geometry on a stock that still trades intraday; this
    # one *refuses* intraday entirely for stocks too volatile to
    # square off in a half-day session. 0 = disabled.
    max_atr_pct_for_intraday_eligibility: float = Field(default=0.05, ge=0, le=0.5)


# Mode presets: (min_days, max_days) range per strategy mode.
# Holding period is computed dynamically per stock within this range.
_MODE_HOLDING_DAYS: dict[str, tuple[int, int]] = {
    "intraday": (0, 0),        # same day (MIS)
    "short_term": (2, 5),      # 2–5 trading days
    "balanced": (0, 15),       # model decides: intraday up to 3 weeks
    "long_term": (5, 66),      # 1 week to ~3 months (configurable via max_holding_days)
    "swing": (2, 66),          # short + long combined, NEVER intraday/MIS (CNC only)
}

# Kept for backwards compatibility — maps mode to discrete period labels
_MODE_HOLDING_PERIODS: dict[str, list[str]] = {
    "intraday": ["intraday"],
    "short_term": ["short_term"],
    "balanced": ["intraday", "short_term", "long_term"],
    "long_term": ["long_term"],
    "swing": ["short_term", "long_term"],   # swing model across the full 2–66d range, no MIS
}


class HoldingExpiryConfig(BaseModel):
    """Controls what happens when a position exceeds its expected holding period."""

    enabled: bool = True
    action: Literal["tighten_or_close", "force_close", "ignore"] = "tighten_or_close"
    breakeven_buffer_pct: float = Field(default=0.3, ge=0, le=5.0)
    loss_threshold_pct: float = Field(default=-0.5, ge=-10.0, le=0)
    max_holding_days: int = Field(default=66, ge=1, le=252)  # ~3 months of trading days


class FeedbackSourcesConfig(BaseModel):
    """Which feedback data sources to include in retraining."""

    predictions: bool = True  # prediction outcomes (paper, live, all modes)
    dry_runs: bool = True  # scored dry run signals
    trades: bool = True  # closed trade PnL and slippage


class FeedbackConfig(BaseModel):
    """Controls the ML feedback loop — how the model learns from its own performance."""

    enabled: bool = True
    lookback_days: int = Field(default=14, ge=1, le=90)
    sample_weight_boost: float = Field(default=2.0, gt=1.0, le=5.0)
    sources: FeedbackSourcesConfig = Field(default_factory=FeedbackSourcesConfig)


class PartialProfitConfig(BaseModel):
    """Partial profit booking — close a portion of the position at intermediate targets."""

    enabled: bool = True
    first_target_pct: float = Field(default=0.5, gt=0, le=1)  # book at 50% of target
    first_close_pct: float = Field(default=0.5, gt=0, le=1)  # close 50% of position
    move_sl_to_breakeven: bool = True  # after first booking, move SL to entry price


class ScaledEntryConfig(BaseModel):
    """Scaled entry — split orders into legs for better average entry."""

    enabled: bool = False
    legs: int = Field(default=2, ge=1, le=4)  # number of entry legs
    second_leg_offset_pct: float = Field(default=0.005, ge=0, le=0.05)  # 0.5% below entry for BUY
    second_leg_delay_sec: int = Field(default=30, ge=0, le=300)  # wait before second leg


class MarketRegimeConfig(BaseModel):
    """Market regime detection — adjust strategy based on market conditions."""

    enabled: bool = True
    index_symbol: str = "NIFTY 50"  # benchmark index for regime detection
    lookback_days: int = Field(default=20, ge=5, le=60)
    bull_bias_intraday_pct: float = Field(default=0.3, ge=0, le=1)  # 30% preference for shorter trades in bull
    bear_max_holding_days: int = Field(default=5, ge=1, le=15)  # cap holding days in bear market
    range_prefer_mean_reversion: bool = True  # prefer oversold/overbought entries in range


class ConvictionSizingConfig(BaseModel):
    """Conviction-based position sizing — scale size by ML confidence."""

    enabled: bool = True
    min_multiplier: float = Field(default=0.6, gt=0, le=1)  # size at min confidence
    max_multiplier: float = Field(default=1.5, ge=1, le=3)  # size at max confidence
    confidence_floor: float = Field(default=0.65, ge=0, le=1)  # maps to min_multiplier
    confidence_ceiling: float = Field(default=0.90, ge=0, le=1)  # maps to max_multiplier


class CorrelationLimitConfig(BaseModel):
    """Correlation-aware position limits — beyond simple sector counts."""

    enabled: bool = False
    max_correlated_positions: int = Field(default=2, ge=1, le=5)
    correlation_threshold: float = Field(default=0.7, ge=0.3, le=1)  # pairs above this are "correlated"
    lookback_days: int = Field(default=60, ge=20, le=252)


class DepthGateConfig(BaseModel):
    """Order-flow depth gate using Kite quote depth.

    Imbalance = (total_buy_qty - total_sell_qty) / (total_buy_qty +
    total_sell_qty). Ranges -1 (all sell pressure) to +1 (all buy
    pressure). Instead of hard-blocking, unfavourable books reduce
    position size down to min_size_multiplier (default 40% of normal).
    Size scales linearly: neutral book → 1.0×, worst possible book →
    min_size_multiplier×.

    Requires market_data.kite_data_enabled — only the paid feed exposes
    total_buy_quantity / total_sell_quantity. Off by default; enable
    once you've watched a few sessions to confirm the thresholds match
    your universe's typical book depth.
    """

    enabled: bool = False
    min_imbalance_for_buy: float = Field(default=-0.30, ge=-1.0, le=0.0)
    max_imbalance_for_sell: float = Field(default=0.30, ge=0.0, le=1.0)
    min_size_multiplier: float = Field(default=0.4, ge=0.1, le=1.0)


class LiquidityGateConfig(BaseModel):
    """Pre-trade liquidity gate using Kite top-5 depth.

    Refuses orders whose size would consume more than `max_pct_of_top5`
    of the relevant side of the book. Protects against being your own
    slippage on thinly-traded names — the broker would happily fill
    you, but at the cost of walking through multiple price levels.
    Requires market_data.kite_data_enabled (non-Kite providers don't
    return per-level depth quantities).
    """

    enabled: bool = False
    max_pct_of_top5: float = Field(default=0.10, gt=0, le=1.0)


class ExitTweaksConfig(BaseModel):
    """Auxiliary exit conditions for client-side-managed positions.

    These run alongside the standard target/SL geometry. Currently
    only apply to positions without a broker-side GTT or MIS OCO
    pair (adopted positions, paper trades, edge cases where OCO
    placement failed). Broker-managed exits handle their own
    lifecycle; extending these conditions to MIS OCO / GTT positions
    would require cancelling broker orders and is left for a later
    iteration.
    """

    # Time-stop: intraday positions still open after this many minutes
    # of zero / negligible target progress get exited at market.
    # Captures the "chop trade" failure mode where the setup neither
    # works nor breaks, just stalls.
    time_stop_enabled: bool = False
    intraday_stop_after_min: int = Field(default=180, ge=30, le=375)
    intraday_stop_progress_threshold: float = Field(default=0.30, ge=0, le=1)

    # Volume-exhaustion exit: when the most recent 5-min bar's volume
    # collapses below `min_volume_ratio` × average of the previous
    # `lookback_bars`, and the position is in modest profit
    # (0.5R - 2R), exit. Reads "trend is dying" before SL has a
    # chance to take back the gains.
    volume_exit_enabled: bool = False
    volume_exit_lookback_bars: int = Field(default=12, ge=3, le=60)
    volume_exit_min_ratio: float = Field(default=0.30, gt=0, le=1.0)

    # Trailing-SL tightening near target. Step-up curve: once target
    # progress hits `tighten_start_at_target_pct`, the trailing-SL
    # step shrinks by `tighten_step_decay` per bucket of
    # `tighten_step_size` progress, floored at `tighten_min_multiplier`.
    # Defaults give a gradual ramp — first tightening at 50% target
    # progress, fully floored at 100%.
    #
    # Default curve:
    #   progress  multiplier
    #   < 0.50    1.00 (full step, no tighten)
    #   0.50      0.85
    #   0.60      0.70
    #   0.70      0.55
    #   0.80      0.40
    #   0.90      0.25
    #   >= 1.00   0.20 (floor)
    #
    # Increase tighten_step_decay or lower tighten_min_multiplier for
    # an aggressive lock-in; raise tighten_start_at_target_pct for
    # wider trades that need room to breathe. Applies to both
    # client-side and GTT-managed trailing SL paths.
    tighten_trailing_enabled: bool = True
    tighten_start_at_target_pct: float = Field(default=0.50, ge=0.3, le=0.95)
    tighten_step_size: float = Field(default=0.10, ge=0.05, le=0.50)
    tighten_step_decay: float = Field(default=0.15, ge=0.05, le=0.50)
    tighten_min_multiplier: float = Field(default=0.20, gt=0, le=1.0)


class InstitutionalFlowConfig(BaseModel):
    """Conviction-sizing tweak based on NSE institutional flow data.

    Reads two free-of-cost data points the system was already
    fetching but discarding:

    - Bulk/block deals: institutional accumulation (BUY > SELL) or
      distribution (SELL > BUY) on the candidate symbol in the last
      `bulk_deal_lookback_days`.
    - FII net flow: today's foreign institutional buy minus sell.
      Positive = foreigners net buying, negative = net selling.

    When the signal direction agrees with the flow direction, the
    position size is scaled up by `bulk_deal_size_multiplier` (or
    `fii_aligned_size_multiplier` respectively). When it strongly
    opposes, size is scaled down by 1/multiplier. Both checks are
    independent and multiplicative.

    Off by default. Enable when you've watched a few sessions of
    institutional-flow logs and are happy with the calibration.
    """

    enabled: bool = False
    bulk_deal_lookback_days: int = Field(default=5, ge=1, le=30)
    # Multiplier applied when bulk deals strongly agree with signal
    # direction (e.g. BUY signal + at least 2 net BUY bulk deals).
    bulk_deal_size_multiplier: float = Field(default=1.20, ge=1.0, le=2.0)
    # FII regime threshold (₹ crore). Above + reads as buying-day
    # supporting BUYs; below − reads as selling-day supporting SELLs.
    fii_net_threshold_cr: float = Field(default=500.0, ge=0)
    fii_aligned_size_multiplier: float = Field(default=1.15, ge=1.0, le=2.0)


class RegimeGateConfig(BaseModel):
    """Cross-sectional market-regime gate.

    Computes universe breadth (fraction of symbols up on the day) live
    and refuses new positions when the regime opposes the signal
    direction. BUY signals are blocked when breadth is below
    `min_breadth_for_buy` (broad market is red), SELL signals are
    blocked when breadth is above `max_breadth_for_sell` (broad market
    is green). When breadth is strongly bullish (above
    `bullish_breadth_threshold`), BUY position size is scaled by
    `bullish_size_multiplier`. Mirror for strong bearish on SELL.

    Most "bad days" share one thing: the broad market is moving
    against the trade. This is the cheap, cross-sectional signal that
    catches it without needing a NIFTY ingest.
    """

    enabled: bool = False
    min_breadth_for_buy: float = Field(default=0.40, ge=0.0, le=1.0)
    max_breadth_for_sell: float = Field(default=0.60, ge=0.0, le=1.0)
    bullish_breadth_threshold: float = Field(default=0.65, ge=0.5, le=1.0)
    bearish_breadth_threshold: float = Field(default=0.35, ge=0.0, le=0.5)
    bullish_size_multiplier: float = Field(default=1.20, ge=1.0, le=2.0)
    bearish_size_multiplier: float = Field(default=1.20, ge=1.0, le=2.0)


class MarketTrendFilterConfig(BaseModel):
    """Market-trend circuit breaker for a long-only book.

    Builds an equal-weight market index from the universe's daily closes
    and refuses NEW long (BUY) entries when the index is below its
    `ma_window`-day moving average (a downtrend). Exits (SELLs / closing
    holdings) are never blocked. This is the standard, robust protection
    for a long-biased systematic strategy: ride uptrends, stand aside in
    downtrends. Unlike the breadth `regime_gate` (noisy day-to-day), the
    index-vs-MA trend is the signal that actually bounds drawdowns in a
    sustained bear — the regime a bull-heavy backtest can't validate.

    Default off (opt-in). Enable before running a long-only swing book
    unattended in `auto` mode.
    """

    enabled: bool = False
    ma_window: int = Field(default=50, ge=5, le=400)


class ReentryConfig(BaseModel):
    """Smart re-entry — allow re-entering after SL hit if conditions improve."""

    enabled: bool = True
    min_bars_after_exit: int = Field(default=3, ge=1, le=20)  # wait at least N bars
    min_price_move_pct: float = Field(default=0.02, ge=0, le=0.10)  # price must move 2% from exit
    max_reentries_per_symbol: int = Field(default=1, ge=1, le=3)  # max re-entries per symbol per day
    require_higher_confidence: bool = True  # new signal must have higher confidence than original
    # Tolerance applied when require_higher_confidence is True. The
    # original strict "new >= old" rule rejected legitimate re-entries
    # because ML confidence typically decays as a trend matures — a
    # breakout that scored 0.85 will score lower (e.g. 0.70) on the
    # pullback re-entry even when the setup is just as valid. With
    # tolerance 0.85, we accept new_conf >= orig_conf × 0.85.
    confidence_tolerance: float = Field(default=0.85, ge=0.5, le=1.0)
    # Absolute floor — re-entries below this confidence are rejected
    # regardless of how the original compared. Belt-and-braces with
    # confidence_tolerance: tolerance keeps quality high relative to
    # the originating signal; floor keeps it high in absolute terms.
    min_reentry_confidence: float = Field(default=0.55, ge=0.0, le=1.0)


class FeatureGroupsConfig(BaseModel):
    """Which OPTIONAL (non-technical) feature groups the model trains on.

    Price/technical features (RSI, MACD, EMA, ATR, …) computed from the
    historical OHLCV are ALWAYS used — they're the primary source of truth.
    The groups below are supporting signals layered on top. Each defaults
    on, but can be disabled so the model trains on a leaner, price-primary
    feature set — useful when a support source is sparse/noisy and you want
    to confirm (via a retrain + dry-run conviction comparison) that it
    isn't diluting the core signal. Disabling a group excludes its features
    from the trained model entirely; inference then never feeds them, so
    there's no train/inference mismatch.
    """

    regime: bool = True          # universe breadth / avg-return
    sector: bool = True          # sector breadth / avg-return / relative momentum
    institutional: bool = True   # bulk-deal counts + delivery %
    news: bool = True            # news-sentiment features
    vix: bool = True             # India VIX features
    # F&O is forward-only (Kite exposes no option-chain history), so until
    # months of daily ingest accumulate it's ~all-neutral in training and
    # can only add noise — default OFF, flip on once data exists.
    fno: bool = False
    feedback: bool = True        # fb_* prediction/trade feedback loop


class StrategyConfig(BaseModel):
    mode: Literal["intraday", "short_term", "balanced", "long_term", "swing"] = "balanced"
    allowed_holding_periods: list[str] | None = None
    holding_periods: HoldingPeriodConfig = Field(default_factory=HoldingPeriodConfig)
    # Swing label mode. "relative" (default): cross-sectional
    # relative-momentum label — per trading date, every symbol's forward
    # 10-bar return (entry next-open -> horizon close) is ranked across
    # the universe; the top relative_label_quantile become BUY, the
    # bottom become SELL, the middle HOLD. This subtracts the market's
    # own drift from the label (an absolute barrier label is dominated
    # by it: a zero-skill coin-flip already wins ~40% of 2:1-barrier
    # trades in a rising market) and targets the best-documented
    # Indian-equity anomaly, cross-sectional momentum. Trades still
    # execute with the ATR target/SL geometry — the backtest exits at
    # the LIVE geometry, not at label barriers, which also breaks the
    # label/exit circularity that inflated barrier-mode Sharpe.
    # "barrier": legacy absolute path-aware triple-barrier label.
    # The intraday lane always uses its 1-min triple-barrier label.
    swing_label_mode: Literal["barrier", "relative"] = "relative"
    # Top/bottom quantile for the relative label (0.20 = top/bottom 20%,
    # giving a ~20/60/20 BUY/HOLD/SELL class mix by construction).
    # Shared by the swing and intraday relative modes.
    relative_label_quantile: float = Field(default=0.20, gt=0.0, le=0.4)
    # Intraday label mode. "triple_barrier" (default): 1-min path-resolved
    # hit-target-before-SL, walked to the session close. "relative": the
    # intraday edition of the swing relative label — per 5-min decision
    # INSTANT, every symbol's forward return-to-close is ranked across the
    # universe; top relative_label_quantile -> BUY, bottom -> SELL.
    # Isolates "which stocks outperform TODAY" from absolute barrier hits
    # (the component the intraday features could rank — AUC 0.58/0.63 —
    # but couldn't monetise at absolute barriers). Trade exits keep the
    # ATR geometry either way. Validate via scripts/experiment.py before
    # flipping.
    intraday_label_mode: Literal["triple_barrier", "relative"] = "triple_barrier"
    # Horizon-consistency cap on ML swing trades. The swing model's
    # path-aware label measures a 10-bar (~2-week) window — execution
    # horizons far beyond that ride an edge the label never measured
    # (the trade is held on a model that was only ever asked "does the
    # target hit within ~2 weeks?"). Caps the upper bound of the
    # holding-day range the chooser may assign (long_term/swing modes'
    # 66-day tails clamp to this; balanced's 0-15 is already inside).
    # Raise or set 0 to disable if you knowingly want longer rides.
    swing_horizon_cap_days: int = Field(default=15, ge=0, le=66)
    # Hard sanity ceiling on ATR% (= ATR / entry). A daily ATR above this
    # fraction of price is implausible for an NSE equity (real ATRs run
    # ~1-8%) and almost always means corrupt OHLCV — so the signal is
    # rejected rather than sized off a garbage ATR (which produced e.g. a
    # +189% target / -94% SL). 0 disables the gate.
    max_atr_pct_hard_reject: float = Field(default=0.20, ge=0.0, le=1.0)
    volatility: VolatilityConfig = Field(default_factory=VolatilityConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    feature_groups: FeatureGroupsConfig = Field(default_factory=FeatureGroupsConfig)
    ema_periods: list[int] = Field(default_factory=lambda: [9, 21, 50, 200])
    indicators: IndicatorsConfig = Field(default_factory=IndicatorsConfig)
    min_training_samples: int = 200
    # Number of symbols generate-signals evaluates concurrently per
    # chunk. Each evaluation does DB reads (OHLCV + news), an LTP fetch,
    # feature computation, and the ML predict — the dominant cost is
    # I/O, so a chunk size of ~10 lets ~10 reads overlap while the ML
    # predicts are running for the previous batch. Higher numbers
    # increase memory pressure and risk hitting Kite's REST rate limit
    # for LTP fetches; lower numbers reverts to near-sequential. Set to
    # 1 to disable concurrency entirely (for debugging).
    signal_generation_concurrency: int = Field(default=10, ge=1, le=50)
    market_regime: MarketRegimeConfig = Field(default_factory=MarketRegimeConfig)
    # Apply inverse-frequency class weights at training time so a
    # rare class (e.g. BUY under path-aware 2:1 R/R labelling) isn't
    # buried by the majority class. Multiplies into the existing
    # feedback-driven sample weights. Default-on because zero-BUY
    # output is a failure mode users will hit on first deploy without
    # it; disable if you ever want the unbalanced classifier back.
    class_balance_enabled: bool = True
    # Floor the triple-barrier TARGET at the round-trip transaction-cost +
    # slippage level when labelling. A "win" whose ATR-derived target is
    # smaller than the round-trip cost is a net loss — labelling it BUY/SELL
    # teaches the model an unprofitable, unreachable target (worst on the
    # tight 0.6×ATR intraday geometry). When the ATR target already clears
    # costs (typical swing), the label is unchanged. The same effective
    # target is stored in bars_meta so the walk-forward backtest exits at
    # the same barrier it was labelled against. Set False for legacy
    # gross-return labels.
    label_cost_floor_enabled: bool = True
    # Time-decay sample weighting: older training samples get a linearly
    # decaying weight down to this floor (newest sample = 1.0). Tilts the
    # model toward recent regimes on a non-stationary market. 1.0 = off
    # (every sample weighted equally). Kept off by default — an expanding
    # training window deliberately retains rare old-regime samples (2008 /
    # 2020 crashes), and aggressive decay discards that coverage; lower to
    # ~0.5 to tilt toward recent data once a paper run confirms it helps.
    time_decay_last_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    # Refuse to save a model when any of {BUY, HOLD, SELL} accounts for
    # less than this fraction of training labels. Catches the
    # "BUY is functionally extinct in the data" failure mode at train
    # time instead of letting a sterile model reach production.
    # Set to 0 to disable the guard.
    class_balance_min_pct: float = Field(default=3.0, ge=0.0, le=33.0)
    # After saving a fresh model, run inference on the most recent
    # in-training samples and verify all three classes win argmax at
    # least once. Belt-and-braces for cases where label balance is
    # fine but the model still never predicts a class (calibration
    # collapse, feature dominance). Default-on. Cheap (one matmul on
    # ~hundreds of samples). Disable if you trust the train-time guard.
    post_train_class_check_enabled: bool = True
    # Minimum fraction of recent samples that must produce a NON-HOLD
    # signal through the FULL production path (calibration + tuned
    # thresholds), checked after training. The raw-argmax class check
    # above can pass while the deployed model — after calibration and the
    # threshold gate — fires ~zero signals live (the silent-model failure
    # that shipped a never-trading model). This catches that end-to-end.
    # 0.005 = "at least 0.5% of recent rows must signal". Set 0 to disable.
    post_train_min_signal_rate: float = Field(default=0.005, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def apply_mode_defaults(self) -> "StrategyConfig":
        """Set allowed_holding_periods from mode if not explicitly provided."""
        if self.allowed_holding_periods is None:
            self.allowed_holding_periods = _MODE_HOLDING_PERIODS.get(
                self.mode, ["intraday", "short_term", "long_term"],
            )
        return self


class RiskConfig(BaseModel):
    max_risk_per_trade_pct: float = Field(default=0.02, gt=0, lt=1)
    # Ceiling on how far the chain of conviction / regime / institutional
    # multipliers is allowed to push the per-trade RISK above
    # max_risk_per_trade_pct. Default 1.5 means a 2% base risk can grow
    # to 3% on a strongly-favourable stack but no higher — protects
    # against multiplicative compounding (1.5 × 1.5 × 1.2 = 2.7× base)
    # silently running 5%+ effective risk. Set to 1.0 to disable
    # conviction up-sizing entirely; bumps above ~2.0 are not advised.
    risk_uplift_cap: float = Field(default=1.5, ge=1.0, le=3.0)
    max_portfolio_exposure_pct: float = Field(default=0.60, gt=0, le=1)
    max_open_positions: int = Field(default=10, ge=1)
    max_single_stock_pct: float = Field(default=0.25, gt=0, le=1)
    # Per-signal allocation cap, applied AFTER max_single_stock_pct.
    # Distinct from max_single_stock_pct in intent:
    #   - max_single_stock_pct is a safety cap (don't have 1/4 of capital
    #     in one name even if it's a great trade).
    #   - max_pct_per_signal is a pacing cap (don't blow the daily
    #     exposure budget on the first heartbeat of the day).
    # Default 0.10 means a 60% portfolio cap fits ~6 trades before
    # binding, instead of just 2-3 at the looser single-stock cap.
    # Set equal to max_single_stock_pct to disable.
    max_pct_per_signal: float = Field(default=0.10, gt=0, le=1)
    daily_loss_limit_pct: float = Field(default=0.03, gt=0, lt=1)
    weekly_loss_limit_pct: float = Field(default=0.05, gt=0, lt=1)
    weekly_loss_sizing_reduction: float = Field(default=0.50, gt=0, le=1)
    mandatory_stop_loss: bool = True
    trailing_sl_enabled: bool = True
    # Legacy trigger expressed as a multiple of risk_per_share. Hard
    # to reason about because it depends on the R:R ratio of each
    # signal: 1.5 fires at 75% of target for a 2:1 R:R signal but
    # at 150% (= never) for a 1:1 signal. Kept for backwards-compat
    # with deployments that explicitly tuned this, but the runtime
    # prefers the per-mode target-pct knobs below when set.
    trailing_sl_trigger_multiple: float = Field(default=1.5, gt=0)
    # Per-strategy-bucket trailing trigger expressed as a fraction of
    # target distance covered (0.0–1.0). 0.35 = "once price has moved
    # 35% of the way from entry to target, start trailing." Bucket-
    # split because intraday and swing have very different time
    # horizons — an intraday position has 5-6 hours to reach the
    # trigger; a swing position has 2-5 days, so intraday warrants a
    # more eager trigger. None = fall back to the legacy
    # trailing_sl_trigger_multiple semantics above.
    trailing_sl_trigger_target_pct_intraday: float | None = Field(
        default=0.35, ge=0.0, le=1.0,
    )
    trailing_sl_trigger_target_pct_swing: float | None = Field(
        default=0.50, ge=0.0, le=1.0,
    )
    trailing_sl_step_pct: float = Field(default=0.01, gt=0, lt=1)
    # Early-exit buffer applied to the target check. Heartbeats run every
    # 15 min, so a price that gets within this percentage of target but
    # never quite touches it would otherwise wait a full cycle (and may
    # reverse). 0.0015 = 0.15% which catches a ~15 paisa gap on a ₹100
    # stock or ₹0.75 on a ₹500 stock.
    target_early_exit_pct: float = Field(default=0.0015, ge=0, lt=0.05)
    llm_review_enabled: bool = True
    llm_fallback_to_rules: bool = True
    max_same_sector_positions: int = Field(default=1, ge=1)
    kill_switch_enabled: bool = True
    # Auto-suspend signal generation when drift-watch detects a >15pp
    # win-rate decay or signal-class collapse. Drift-watch runs at 16:30
    # IST daily; when this is on, the suspension flag blocks the next
    # session's generate-signals from running until either (a) a manual
    # retrain via /run model-retrain clears the flag, or (b) the user
    # clears it via the dashboard / API. Off by default — opt-in safety
    # net for users running unattended (drift-watch alerts are still
    # delivered via Telegram regardless).
    drift_auto_suspend_enabled: bool = False
    # Block new entries in symbols with an earnings / board-meeting
    # announcement scheduled within `earnings_blackout_days` calendar
    # days. Earnings reactions routinely move stocks ±5-20% overnight,
    # blowing through any ATR-based SL. The data comes from the
    # `economic_events` table populated by ingest-data's NSE corp-
    # actions scraper. 0 disables the gate.
    earnings_blackout_days: int = Field(default=0, ge=0, le=10)
    # Portfolio-level beta cap (vs NIFTY proxy = INDIA VIX / cross-
    # sectional regime index). 0 disables. When > 0, risk-check
    # computes the position-weighted beta of currently-open + this
    # candidate signal and rejects if it'd push the portfolio over
    # the cap. Use to prevent "every position is a high-beta tech name"
    # correlated-drawdown scenarios. 1.5 is the standard "diversified"
    # ceiling; 2.0 lets you concentrate further.
    max_portfolio_beta: float = Field(default=0.0, ge=0.0, le=5.0)
    min_confidence_buy: float = Field(default=0.60, ge=0, le=1)
    min_confidence_sell: float = Field(default=0.75, ge=0, le=1)
    # Per-strategy-mode floors. Intraday and swing have very different
    # signal characteristics (volatility, holding window, R:R geometry,
    # short-availability), so the same probability floor rarely fits
    # both well. When set, these REPLACE the global floor above for
    # signals matching that holding bucket. `None` falls back to the
    # global value, preserving behaviour for users who haven't
    # configured per-mode floors. "Intraday" = `holding_period ==
    # "intraday"`; everything else (short_swing / week / long) routes
    # to the swing pair.
    min_confidence_buy_intraday: float | None = Field(default=None, ge=0, le=1)
    min_confidence_sell_intraday: float | None = Field(default=None, ge=0, le=1)
    min_confidence_buy_swing: float | None = Field(default=None, ge=0, le=1)
    min_confidence_sell_swing: float | None = Field(default=None, ge=0, le=1)
    skip_sell_on_holdings: bool = True  # position-monitor handles exits; no SELL on held symbols
    max_trades_per_day: int = Field(default=5, ge=1)
    # Per-product caps on top of max_trades_per_day. Both default to
    # None (disabled — only the combined cap applies). Set independently
    # to allow asymmetric policies: e.g. 10 MIS entries per day for an
    # active intraday workflow but only 1 CNC entry per day for slow,
    # deliberate delivery positions. The combined max_trades_per_day
    # still acts as an overall backstop; raise it if the sum of the
    # per-product caps exceeds the current combined value.
    max_mis_trades_per_day: int | None = Field(default=None, ge=1)
    max_cnc_trades_per_day: int | None = Field(default=None, ge=1)
    loss_cooldown_minutes: int = Field(default=15, ge=0)
    # Risk-rejected signals get re-evaluated each heartbeat (most
    # reasons — exposure, drift, depth, correlation, cooldown — are
    # transient). This caps how many times a chronically-rejected
    # symbol may regenerate per day before we give up on it.
    max_risk_rejected_retries_per_day: int = Field(default=5, ge=1, le=20)
    symbol_cooldown_days: int = Field(default=1, ge=0)
    symbol_repeat_lookback_days: int = Field(default=5, ge=0)
    symbol_repeat_min_confidence: float = Field(default=0.80, ge=0, le=1)
    # When True, ask the broker for the real margin requirement via
    # kite.order_margins per signal — catches insufficient-funds /
    # special-margin failures that notional-only sizing misses, and
    # gives MIS its real ~5x leverage. Default False: notional-only
    # sizing is the conservative choice (no leverage unless the user
    # explicitly opts in from Settings).
    margin_usage_enabled: bool = False
    weekly_reset_day: str = "monday"  # day when weekly circuit breaker resets
    # Cap how far apart the model's PnL-tuned BUY and SELL probability
    # thresholds may be at inference time. The walk-forward sweep that
    # picks these thresholds can land on highly asymmetric pairs (e.g.
    # BUY=0.80, SELL=0.70 from the user's most recent retrain) when one
    # class happens to pay better on the holdout slice — and then the
    # production model never fires that class. We shrink both
    # thresholds toward their midpoint until the gap is at most this
    # value. Default 0.05 (5 percentage points) keeps the model's
    # learned preference but prevents the "0 BUY signals in 7 days"
    # class-collapse the drift-watch tonight alert catches. Set to a
    # large number (e.g. 1.0) to disable; set to 0.0 to force exactly
    # symmetric thresholds.
    tuned_threshold_max_diff: float = Field(default=0.05, ge=0, le=1.0)
    # Absolute ceiling on the tuned BUY / SELL probability thresholds.
    # The diff cap above only addresses asymmetry — it can't help when
    # the sweep saved (0.70, 0.70) and the calibrated probabilities
    # rarely cross 0.60. That's the second class-collapse mode: both
    # tuned thresholds are reachable in the holdout slice but
    # unreachable on the live feed (because the holdout had a few
    # high-conviction setups dominating Sharpe, while live trading
    # mostly sees moderate-conviction signals). Capping at this value
    # keeps the sweep's directional preference intact while guaranteeing
    # the gate stays reachable. 0.60 = "any tuned threshold above 0.60
    # gets pulled down to 0.60". Set to 1.0 to disable. Setting this
    # below `min_confidence_buy` / `min_confidence_sell` doesn't add
    # value since those floors still apply downstream.
    tuned_threshold_max_value: float = Field(default=0.60, ge=0.5, le=1.0)
    # Minimum fraction of holdout samples a tuned-threshold cell must
    # signal on (BUY or SELL) to be eligible. The threshold sweep ranks by
    # Sharpe, and max selectivity tends to maximise Sharpe — so without a
    # signal-RATE floor the tuner parks at the most selective (highest)
    # cell, which fires ~never on the live feed and collapses every signal
    # to HOLD (the silent-model failure). The pre-existing `min_trades`
    # floor is absolute (100), a trivial 0.2% rate on a large holdout, so
    # it doesn't catch this. 0.02 = "the chosen cutoff must produce a
    # signal on at least 2% of samples". Set 0.0 to disable.
    tuned_min_signal_rate: float = Field(default=0.02, ge=0.0, le=1.0)
    # Hard overrides on the model's tuned probability thresholds.
    # When set, these REPLACE the saved tuned values entirely (the
    # diff cap above no longer applies). Use when the model's saved
    # thresholds are unreachable in production — e.g. the tuner saved
    # buy=0.80 but the model never outputs P(BUY) > 0.50, so no BUY
    # signals fire regardless of the diff cap. Setting
    # buy_threshold_override=0.45 then lets every P(BUY) >= 0.45
    # through. None = use the model's saved tuned threshold (default).
    # Both checks remain ANDed with `min_confidence_buy` /
    # `min_confidence_sell`, so those are still the absolute floor.
    buy_threshold_override: float | None = Field(default=None, ge=0, le=1.0)
    sell_threshold_override: float | None = Field(default=None, ge=0, le=1.0)
    # Minimum cost-adjusted reward:risk ratio required to take a signal.
    # Computes (target − entry) × qty − round-trip-costs as net win and
    # (entry − sl) × qty + costs as net loss (sign-flipped for SELL),
    # then rejects when net_win / net_loss < this threshold. Catches the
    # "0.6 × ATR target on a ₹180 stock at 38 qty" signals where the
    # gross 2:1 collapses to 1.3:1 after brokerage + STT + GST, leaving
    # no margin for slippage. Set to 0 to disable.
    min_net_rr: float = Field(default=1.5, ge=0, le=10)
    holding_expiry: HoldingExpiryConfig = Field(default_factory=HoldingExpiryConfig)
    partial_profit: PartialProfitConfig = Field(default_factory=PartialProfitConfig)
    conviction_sizing: ConvictionSizingConfig = Field(default_factory=ConvictionSizingConfig)
    correlation_limit: CorrelationLimitConfig = Field(default_factory=CorrelationLimitConfig)
    depth_gate: DepthGateConfig = Field(default_factory=DepthGateConfig)
    liquidity_gate: LiquidityGateConfig = Field(default_factory=LiquidityGateConfig)
    regime_gate: RegimeGateConfig = Field(default_factory=RegimeGateConfig)
    market_trend_filter: MarketTrendFilterConfig = Field(
        default_factory=MarketTrendFilterConfig
    )
    institutional_flow: InstitutionalFlowConfig = Field(default_factory=InstitutionalFlowConfig)
    exit_tweaks: ExitTweaksConfig = Field(default_factory=ExitTweaksConfig)
    reentry: ReentryConfig = Field(default_factory=ReentryConfig)

    def resolve_min_confidence(
        self, holding_period: str, signal_type: str,
    ) -> float:
        """Pick the per-mode floor when set, else fall back to the
        global `min_confidence_buy` / `min_confidence_sell`.

        Routes `holding_period == "intraday"` to the intraday pair;
        everything else (short_swing / week / long) routes to the
        swing pair.
        """
        is_intraday = holding_period == "intraday"
        is_buy = signal_type == "BUY"
        if is_intraday:
            per_mode = (
                self.min_confidence_buy_intraday if is_buy
                else self.min_confidence_sell_intraday
            )
        else:
            per_mode = (
                self.min_confidence_buy_swing if is_buy
                else self.min_confidence_sell_swing
            )
        if per_mode is not None:
            return float(per_mode)
        return float(
            self.min_confidence_buy if is_buy else self.min_confidence_sell
        )

    def resolve_trailing_trigger(
        self, holding_period: str, risk_per_share: float, target_distance: float,
    ) -> float:
        """Return the profit-in-rupees threshold at which trailing-SL
        should start firing, given a position's holding bucket.

        New semantic (preferred): `trailing_sl_trigger_target_pct_*`
        expresses the trigger as a fraction of target distance, so
        0.5 = "start trailing once we've covered half the way from
        entry to target." This is the same scale users see on the
        progress bars in PositionsTable and is independent of the
        signal's R:R ratio.

        Legacy fallback: when the per-mode target-pct is None, we
        keep the old `trailing_sl_trigger_multiple × risk_per_share`
        behaviour so deployments that explicitly tuned the legacy
        knob keep working.

        Always returns rupees-of-profit threshold so the three
        trailing paths (client-side / GTT / MIS) can stay symmetric.
        """
        is_intraday = holding_period == "intraday"
        per_mode_pct = (
            self.trailing_sl_trigger_target_pct_intraday if is_intraday
            else self.trailing_sl_trigger_target_pct_swing
        )
        if per_mode_pct is not None and target_distance > 0:
            return float(per_mode_pct) * float(target_distance)
        return float(self.trailing_sl_trigger_multiple) * float(risk_per_share)


class MarketHoursConfig(BaseModel):
    open: str = "09:15"
    close: str = "15:30"
    order_start: str = "09:30"
    order_end: str = "15:15"
    square_off: str = "15:15"
    square_off_extension: str = "00:05"
    intraday_cutoff: str = "14:30"  # No new intraday signals after this time
    timezone: str = "Asia/Kolkata"
    holidays: list[str] = Field(default_factory=list)  # YYYY-MM-DD strings
    early_close_days: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_ordering(self) -> "MarketHoursConfig":
        t_open = _parse_time(self.open)
        t_close = _parse_time(self.close)
        t_order_start = _parse_time(self.order_start)
        t_order_end = _parse_time(self.order_end)
        t_square_off = _parse_time(self.square_off)

        if t_order_start >= t_order_end:
            raise ValueError(
                f"market_hours.order_start ({self.order_start}) must be before "
                f"order_end ({self.order_end})"
            )
        if t_order_start < t_open:
            raise ValueError(
                f"order_start ({self.order_start}) cannot be before market open ({self.open})"
            )
        if t_order_end > t_close:
            raise ValueError(
                f"order_end ({self.order_end}) cannot be after market close ({self.close})"
            )
        if t_square_off > t_close:
            raise ValueError(
                f"square_off ({self.square_off}) cannot be after market close ({self.close})"
            )
        return self


class ExecutionConfig(BaseModel):
    max_order_retries: int = 3
    scaled_entry: ScaledEntryConfig = Field(default_factory=ScaledEntryConfig)
    retry_base_delay_sec: int = 2
    paper_slippage_pct: float = Field(default=0.001, ge=0)
    order_timeout_sec: int = 30
    price_drift_max_pct: float = Field(default=0.02, gt=0, lt=1)
    transaction_mode: Literal["auto", "manual"] = "auto"  # manual = require approval before execution
    rejection_cooldown_hours: int = Field(default=48, ge=0, le=168)  # skip re-queuing a rejected trade
    # Pending trade auto-expiry. Heartbeat sweeps anything older than
    # this before running risk-check so abandoned approvals don't
    # silently lock max_open_positions / max_trades_per_day budgets.
    pending_expiry_minutes: int = Field(default=30, ge=1, le=1440)


class TransactionCostConfig(BaseModel):
    brokerage_per_leg_pct: float = 0.0003  # 0.03% or ₹20 cap
    brokerage_cap_per_leg: float = 20.0  # ₹20 max brokerage per order
    stt_intraday_pct: float = 0.00025  # 0.025% on sell side (MIS)
    stt_delivery_pct: float = 0.001  # 0.1% on sell side (CNC)
    other_charges_pct: float = 0.0001  # stamp duty + GST + exchange (~0.01%)


class RetentionConfig(BaseModel):
    # DAILY OHLCV retention. The nightly maintenance floors the actual
    # prune window at max(retraining.max_training_days,
    # market_data.backfill_days), so a value below that is harmless —
    # training history (and exited/delisted symbols) is preserved
    # regardless. Set it >= that window to make the intent explicit.
    ohlcv_days: int = 730
    # INTRADAY OHLCV (5-minute etc.) retention — decoupled from daily.
    # Intraday bars are ~75× heavier per day than daily and are NOT
    # used for model training (the model trains on daily bars); they're
    # only consumed operationally (volume-exhaustion exits, live
    # monitoring). Keeping years of 5-min bars just bloats the DB, so
    # this defaults to the intraday backfill window (365d) rather than
    # inheriting the much longer daily retention.
    intraday_ohlcv_days: int = 365
    audit_log_days: int = 365
    predictions_days: int = 365
    news_days: int = 90
    economic_events_days: int = 365
    # Dry-run previews accumulate one row per generated signal every time the
    # user runs a dry-run; they have no FK dependents, so they're safe to
    # time-prune (unlike `signals`, which trades/predictions reference).
    dry_run_days: int = 90


class DatabaseConfig(BaseModel):
    path: str = "./data/yolovest.db"
    backup_enabled: bool = True
    backup_cron: str = "0 18 * * *"
    backup_dir: str = "./backups"
    # How many of the most-recent (unlocked) DB backups + model snapshots the
    # daily maintenance keeps. Locked backups float out of the rotation on
    # top of this count. Was hardcoded to 7.
    backup_keep: int = Field(default=7, ge=1, le=365)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class XGBoostConfig(BaseModel):
    """XGBoost training hyperparameters. Defaults favour generalization on
    noisy financial features: a lower learning rate with more trees gated
    by early stopping, plus row/column subsampling and a higher
    min_child_weight (the core variance-reduction knobs). Tune via Settings
    for offline training; the live retrain reads these."""
    max_depth: int = Field(default=6, ge=1, le=16)
    learning_rate: float = Field(default=0.05, gt=0.0, le=1.0)
    # Upper bound on trees; early stopping usually selects far fewer.
    n_estimators: int = Field(default=400, ge=10, le=5000)
    min_child_weight: float = Field(default=5.0, ge=0.0, le=1000.0)
    subsample: float = Field(default=0.8, gt=0.0, le=1.0)
    colsample_bytree: float = Field(default=0.8, gt=0.0, le=1.0)
    gamma: float = Field(default=0.0, ge=0.0, le=10.0)
    reg_lambda: float = Field(default=1.0, ge=0.0, le=100.0)
    reg_alpha: float = Field(default=0.0, ge=0.0, le=100.0)
    # Early stopping: probe the tree count on a purged chronological
    # validation tail, then refit on all data at that count. 0 = off.
    early_stopping_rounds: int = Field(default=50, ge=0, le=500)
    # Corpora below this skip the probe and keep the configured n_estimators
    # (the validation tail would be too small to trust).
    early_stopping_min_samples: int = Field(default=2000, ge=0)


class RetrainingConfig(BaseModel):
    schedule_cron: str = "0 6 * * 6"
    xgb: XGBoostConfig = Field(default_factory=XGBoostConfig)
    shadow_mode_days: int = 7
    shadow_min_predictions: int = 10
    retired_model_cleanup_days: int = 30
    # Cap training history loaded for the feature matrix. Peak training
    # memory scales with days × symbols, so this is the primary knob for
    # sizing a retrain to whatever host it runs on. Raise it if you want
    # the model to see deeper history — the ceiling is 12000 (~33yr),
    # comfortably covering the full available history (daily data starts
    # ~1996).
    max_training_days: int = Field(default=730, ge=90, le=12000)
    # Honest-edge promotion gate. A model may only be promoted to
    # production when its *argmax* walk-forward Sharpe (the edge of its
    # natural, untuned decisions) is at least this value. The
    # threshold-tuned Sharpe is selection-biased — a model can score
    # well only on a cherry-picked high-probability tail that the live
    # model may never reach — so promotion decisions must clear the
    # untuned edge first. Default 0.0 blocks net-losing models. Set
    # negative to disable (not recommended on a live account).
    min_argmax_sharpe_for_promotion: float = Field(default=0.0, ge=-100.0, le=100.0)
    # CV/holdout embargo (López de Prado). The label-overlap purge already
    # drops train rows whose label window reaches the test/holdout start;
    # the embargo adds a further buffer to absorb serial-correlation and
    # delayed-market-reaction leakage between the train tail and the
    # test/holdout head (features near the boundary stay correlated even
    # when label windows don't overlap). Expressed as a fraction of the
    # data's calendar span (~0.01 = 1% is the standard rule of thumb).
    # Widens both the K-fold purge gap and the final-scale tuning-holdout
    # gap. 0 disables (legacy: label-overlap purge only).
    cv_embargo_frac: float = Field(default=0.01, ge=0.0, le=0.2)


class ReportsConfig(BaseModel):
    daily_report_time: str = "16:00"
    weekly_report_cron: str = "0 10 * * 6"


class NewsDigestConfig(BaseModel):
    enabled: bool = True
    schedule_cron: str = "0 9 * * *"  # 9:00 AM IST, every day
    max_headlines: int = Field(default=10, ge=1, le=50)


class ScoringConfig(BaseModel):
    """Auto-scoring of dry-runs and predictions against their target dates.

    A daily CRON (after market close, once the day's daily bars are
    ingested) sweeps every dry-run with unscored signals and every
    elapsed prediction, and scores each against the actuals on its OWN
    target date — path-aware over the holding window — rather than today.
    Partial by construction: signals whose horizon hasn't fully elapsed
    are left pending and picked up on a later run.
    """

    auto_score_enabled: bool = True
    # 16:45 IST weekdays — after daily bars land (~15:30-16:00) and after
    # report-generate (16:00) / ahead of drift-watch (16:30 reads scores).
    auto_score_cron: str = "45 16 * * 1-5"


class LoggingConfig(BaseModel):
    level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR
    file_level: str = "INFO"  # log file can have a different level
    log_dir: str = "./logs"
    max_bytes: int = 10 * 1024 * 1024  # 10 MB per log file
    backup_count: int = 5  # number of rotated files to keep


class DashboardConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    password: SecretStr = SecretStr("yolovest")
    show_degraded_banner: bool = True  # set false if intentionally running without LLM/services


class TelegramAlertsConfig(BaseModel):
    trade_entry: bool = True
    trade_exit: bool = True
    daily_summary: bool = True
    weekly_summary: bool = True
    errors: bool = True
    kill_switch: bool = True


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: SecretStr = SecretStr("")
    chat_id: str = ""
    alerts: TelegramAlertsConfig = Field(default_factory=TelegramAlertsConfig)


class NotificationsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """Top-level application configuration. Composes all sub-configs."""

    mode: Literal["paper", "live"] = "paper"
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    scanning: ScanningConfig = Field(default_factory=ScanningConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    market_hours: MarketHoursConfig = Field(default_factory=MarketHoursConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    transaction_costs: TransactionCostConfig = Field(default_factory=TransactionCostConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    retraining: RetrainingConfig = Field(default_factory=RetrainingConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    log: LoggingConfig = Field(default_factory=LoggingConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    news_digest: NewsDigestConfig = Field(default_factory=NewsDigestConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """Create config from dict, expanding environment variables."""
        expanded = _expand_env_vars(data)
        return cls.model_validate(expanded)


def load_config(path: str) -> AppConfig:
    """Load and validate config from a YAML file.

    Environment variables in ${VAR_NAME} format are expanded before parsing.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    _load_dotenv(config_path.parent)

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raw = {}

    expanded = _expand_env_vars(raw)
    return AppConfig.model_validate(expanded)


# ---------------------------------------------------------------------------
# File-only keys — never stored in DB or exposed via UI.
# These require secrets, filesystem paths, or server restart to change.
# ---------------------------------------------------------------------------

FILE_ONLY_KEYS: set[str] = {
    # Secrets (must come from env vars)
    "broker.api_key",
    "broker.api_secret",
    "llm.api_key",
    "notifications.telegram.bot_token",
    "notifications.telegram.chat_id",
    # Filesystem paths (hardcoded by Docker volume mounts)
    "database.path",
    "database.backup_dir",
    "market_data.bhavcopy_dir",
    # Server binding (hardcoded by Docker EXPOSE / nginx proxy)
    "dashboard.host",
    "dashboard.port",
    "dashboard.password",
    # Logging paths/rotation (hardcoded by Docker volume mounts)
    "log.log_dir",
    "log.max_bytes",
    "log.backup_count",
}

# Keys managed via dedicated UI or internal-only, hidden from Settings page
# but still stored in DB.
SETTINGS_HIDDEN_KEYS: set[str] = {
    "market_hours.holidays",
    "market_hours.early_close_days",
}


def _flatten_model(
    model: BaseModel, prefix: str = "",
) -> dict[str, str]:
    """Flatten a Pydantic model to dot-notation key-value pairs.

    Values are JSON-encoded for non-scalar types (lists, dicts).
    SecretStr fields are skipped.
    """
    import json as _json

    result: dict[str, str] = {}
    for field_name, _field_info in type(model).model_fields.items():
        key = f"{prefix}{field_name}" if prefix else field_name
        value = getattr(model, field_name)

        if isinstance(value, SecretStr):
            continue  # never persist secrets
        if isinstance(value, BaseModel):
            result.update(_flatten_model(value, prefix=f"{key}."))
        elif isinstance(value, (list, dict)):
            result[key] = _json.dumps(value)
        elif isinstance(value, bool):
            result[key] = _json.dumps(value)  # "true"/"false" not "True"/"False"
        elif value is None:
            result[key] = _json.dumps(None)
        else:
            result[key] = str(value)
    return result


def get_db_editable_defaults() -> dict[str, str]:
    """Return the default values for all DB-editable config keys.

    Builds a default AppConfig, flattens it, then removes file-only keys.
    """
    defaults = _flatten_model(AppConfig())
    return {k: v for k, v in defaults.items() if k not in FILE_ONLY_KEYS}


def _set_nested(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    """Set a value in a nested dict using dot-notation key."""
    parts = dotted_key.split(".")
    obj = data
    for part in parts[:-1]:
        if part not in obj:
            obj[part] = {}
        obj = obj[part]
    obj[parts[-1]] = value


def _parse_db_value(key: str, raw: str) -> Any:
    """Parse a DB string value back to its Python type using the model schema."""
    import json as _json

    # Try JSON first (handles booleans, lists, dicts, null)
    try:
        parsed = _json.loads(raw)
        # JSON parsed successfully — return as-is for booleans, lists, dicts, null
        if isinstance(parsed, (bool, list, dict)) or parsed is None:
            return parsed
        # For numbers that came through JSON, return them
        if isinstance(parsed, (int, float)):
            return parsed
        # For strings that happen to be valid JSON strings, return raw
        return raw
    except (ValueError, _json.JSONDecodeError):
        pass

    # Try numeric conversion
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass

    return raw


def apply_db_config(base_config: AppConfig, db_values: dict[str, str]) -> AppConfig:
    """Merge DB config values into an AppConfig, returning a new instance.

    Builds a nested dict from the base config, overlays DB values,
    then re-validates through Pydantic.
    """
    # Start with the full base config as a dict
    data = base_config.model_dump()

    # Overlay DB values
    for key, raw_value in db_values.items():
        if key in FILE_ONLY_KEYS:
            continue
        parsed = _parse_db_value(key, raw_value)
        _set_nested(data, key, parsed)

    # Re-validate (this runs all Pydantic validators)
    merged = AppConfig.model_validate(data)

    # Preserve SecretStr fields from the original config (they aren't in DB)
    merged.broker.api_key = base_config.broker.api_key
    merged.broker.api_secret = base_config.broker.api_secret
    merged.llm.api_key = base_config.llm.api_key
    merged.notifications.telegram.bot_token = base_config.notifications.telegram.bot_token
    merged.dashboard.password = base_config.dashboard.password
    return merged


def _annotation_number_kind(annotation: Any) -> str | None:
    """Classify a field annotation as "int" / "float" / None (not a
    scalar number). Unwraps Optional[X]; bool is excluded explicitly
    (it subclasses int but is a toggle, not a number)."""
    import types as _types
    import typing as _typing

    origin = _typing.get_origin(annotation)
    if origin is _typing.Union or origin is getattr(_types, "UnionType", None):
        args = [a for a in _typing.get_args(annotation) if a is not type(None)]
        return _annotation_number_kind(args[0]) if len(args) == 1 else None
    if annotation is bool:
        return None
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    return None


def config_field_kinds(
    model: BaseModel | None = None, prefix: str = "",
) -> dict[str, str]:
    """Dot-notation key -> "int"/"float" for every numeric config field,
    derived from the Pydantic ANNOTATIONS (the source of truth).

    The Settings UI needs this because JSON erases the distinction: a
    float field sitting at a whole-number value (time_decay_last_weight
    = 1.0) serializes as `1`, the frontend's value-based heuristic
    classifies it as int, and the number input then rejects perfectly
    valid decimals like 0.5. Mirrors _flatten_model's traversal so the
    keys match /api/config exactly.
    """
    if model is None:
        model = AppConfig()
    kinds: dict[str, str] = {}
    for field_name, field_info in type(model).model_fields.items():
        key = f"{prefix}{field_name}" if prefix else field_name
        value = getattr(model, field_name)
        if isinstance(value, SecretStr):
            continue
        if isinstance(value, BaseModel):
            kinds.update(config_field_kinds(value, prefix=f"{key}."))
            continue
        kind = _annotation_number_kind(field_info.annotation)
        if kind:
            kinds[key] = kind
    return kinds


def config_to_ui_sections(config: AppConfig) -> dict[str, dict[str, Any]]:
    """Convert the DB-editable portion of config into UI-friendly sections.

    Returns a dict of section_name -> {key: value} for the frontend.
    """
    flat = _flatten_model(config)
    sections: dict[str, dict[str, Any]] = {}
    for key, value in sorted(flat.items()):
        if key in FILE_ONLY_KEYS or key in SETTINGS_HIDDEN_KEYS:
            continue
        section = key.split(".")[0]
        if section not in sections:
            sections[section] = {}
        sections[section][key] = _parse_db_value(key, value)
    return sections
