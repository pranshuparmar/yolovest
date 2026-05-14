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
    intraday_backfill_days: int = 365  # 5-minute-bar history window for backfill-intraday


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
    # Watchlist rotation: evict symbols that produce no actionable signal for N
    # consecutive heartbeats, apply a cooldown so market-scan doesn't re-add them.
    rotation_enabled: bool = True
    rotation_no_signal_threshold: int = Field(default=8, ge=1, le=100)
    rotation_cooldown_hours: int = Field(default=48, ge=1, le=72)


class IndicatorsConfig(BaseModel):
    rsi: bool = True
    macd: bool = True
    bollinger_bands: bool = True
    vwap: bool = True
    atr: bool = True
    volume_profile: bool = True
    obv: bool = True
    supertrend: bool = True


class ATRMultipliers(BaseModel):
    """ATR multipliers for target and stop-loss calculation."""

    target: float = Field(default=2.0, gt=0)
    stop_loss: float = Field(default=1.0, gt=0)


class HoldingPeriodConfig(BaseModel):
    """ATR multipliers per holding period for target/SL sizing.

    For dynamic holding periods, multipliers are interpolated between the
    nearest defined buckets based on expected_holding_days.
    """

    intraday: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(target=0.6, stop_loss=0.3),
    )
    short_swing: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(target=1.5, stop_loss=0.75),
    )
    week: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(target=2.5, stop_loss=1.2),
    )
    long: ATRMultipliers = Field(
        default_factory=lambda: ATRMultipliers(target=5.0, stop_loss=2.0),
    )


class VolatilityConfig(BaseModel):
    """Volatility thresholds for stock selection and holding period decisions.

    ATR% = ATR / price. A stock with 2% ATR% moves ~2% per day on average.
    """

    min_atr_pct: float = Field(default=0.005, ge=0)
    max_atr_pct: float = Field(default=0.05, gt=0)
    ideal_min_atr_pct: float = Field(default=0.015, ge=0)
    ideal_max_atr_pct: float = Field(default=0.03, gt=0)


# Mode presets: (min_days, max_days) range per strategy mode.
# Holding period is computed dynamically per stock within this range.
_MODE_HOLDING_DAYS: dict[str, tuple[int, int]] = {
    "intraday": (0, 0),        # same day (MIS)
    "short_term": (2, 5),      # 2–5 trading days
    "balanced": (0, 15),       # model decides: intraday up to 3 weeks
    "long_term": (5, 66),      # 1 week to ~3 months (configurable via max_holding_days)
}

# Kept for backwards compatibility — maps mode to discrete period labels
_MODE_HOLDING_PERIODS: dict[str, list[str]] = {
    "intraday": ["intraday"],
    "short_term": ["short_term"],
    "balanced": ["intraday", "short_term", "long_term"],
    "long_term": ["long_term"],
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


class ReentryConfig(BaseModel):
    """Smart re-entry — allow re-entering after SL hit if conditions improve."""

    enabled: bool = True
    min_bars_after_exit: int = Field(default=3, ge=1, le=20)  # wait at least N bars
    min_price_move_pct: float = Field(default=0.02, ge=0, le=0.10)  # price must move 2% from exit
    max_reentries_per_symbol: int = Field(default=1, ge=1, le=3)  # max re-entries per symbol per day
    require_higher_confidence: bool = True  # new signal must have higher confidence than original


class StrategyConfig(BaseModel):
    mode: Literal["intraday", "short_term", "balanced", "long_term"] = "balanced"
    allowed_holding_periods: list[str] | None = None
    holding_periods: HoldingPeriodConfig = Field(default_factory=HoldingPeriodConfig)
    volatility: VolatilityConfig = Field(default_factory=VolatilityConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    ema_periods: list[int] = Field(default_factory=lambda: [9, 21, 50, 200])
    indicators: IndicatorsConfig = Field(default_factory=IndicatorsConfig)
    min_training_samples: int = 200
    market_regime: MarketRegimeConfig = Field(default_factory=MarketRegimeConfig)

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
    max_portfolio_exposure_pct: float = Field(default=0.60, gt=0, le=1)
    max_open_positions: int = Field(default=10, ge=1)
    max_single_stock_pct: float = Field(default=0.25, gt=0, le=1)
    daily_loss_limit_pct: float = Field(default=0.03, gt=0, lt=1)
    weekly_loss_limit_pct: float = Field(default=0.05, gt=0, lt=1)
    weekly_loss_sizing_reduction: float = Field(default=0.50, gt=0, le=1)
    mandatory_stop_loss: bool = True
    trailing_sl_enabled: bool = True
    trailing_sl_trigger_multiple: float = Field(default=1.5, gt=0)
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
    min_confidence_buy: float = Field(default=0.60, ge=0, le=1)
    min_confidence_sell: float = Field(default=0.75, ge=0, le=1)
    skip_sell_on_holdings: bool = True  # position-monitor handles exits; no SELL on held symbols
    max_trades_per_day: int = Field(default=5, ge=1)
    loss_cooldown_minutes: int = Field(default=15, ge=0)
    symbol_cooldown_days: int = Field(default=1, ge=0)
    symbol_repeat_lookback_days: int = Field(default=5, ge=0)
    symbol_repeat_min_confidence: float = Field(default=0.80, ge=0, le=1)
    margin_usage_enabled: bool = False  # when False, position value capped by available cash (no leverage)
    weekly_reset_day: str = "monday"  # day when weekly circuit breaker resets
    holding_expiry: HoldingExpiryConfig = Field(default_factory=HoldingExpiryConfig)
    partial_profit: PartialProfitConfig = Field(default_factory=PartialProfitConfig)
    conviction_sizing: ConvictionSizingConfig = Field(default_factory=ConvictionSizingConfig)
    correlation_limit: CorrelationLimitConfig = Field(default_factory=CorrelationLimitConfig)
    reentry: ReentryConfig = Field(default_factory=ReentryConfig)


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


class TransactionCostConfig(BaseModel):
    brokerage_per_leg_pct: float = 0.0003  # 0.03% or ₹20 cap
    brokerage_cap_per_leg: float = 20.0  # ₹20 max brokerage per order
    stt_intraday_pct: float = 0.00025  # 0.025% on sell side (MIS)
    stt_delivery_pct: float = 0.001  # 0.1% on sell side (CNC)
    other_charges_pct: float = 0.0001  # stamp duty + GST + exchange (~0.01%)


class RetentionConfig(BaseModel):
    ohlcv_days: int = 730
    audit_log_days: int = 365
    predictions_days: int = 365
    news_days: int = 90
    economic_events_days: int = 365


class DatabaseConfig(BaseModel):
    path: str = "./data/yolovest.db"
    backup_enabled: bool = True
    backup_cron: str = "0 18 * * *"
    backup_dir: str = "./backups"
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


class RetrainingConfig(BaseModel):
    schedule_cron: str = "0 6 * * 6"
    shadow_mode_days: int = 7
    shadow_min_predictions: int = 10
    retired_model_cleanup_days: int = 30


class ReportsConfig(BaseModel):
    daily_report_time: str = "16:00"
    weekly_report_cron: str = "0 10 * * 6"


class NewsDigestConfig(BaseModel):
    enabled: bool = True
    schedule_cron: str = "0 9 * * *"  # 9:00 AM IST, every day
    max_headlines: int = Field(default=10, ge=1, le=50)


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
    for field_name, field_info in model.model_fields.items():
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
