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
    model: str = "gemini-2.5-pro"
    api_key: SecretStr = SecretStr("")


class MarketDataConfig(BaseModel):
    daily_provider: str = "jugaad"
    daily_fallback: str = "yfinance"
    intraday_provider: str = "tvdatafeed"
    kite_data_enabled: bool = False  # enable Kite Connect as data provider
    news_enabled: bool = True  # fetch news from MoneyControl, ET Markets, LiveMint
    scrapers_enabled: bool = True  # fetch from Screener.in, Trendlyne, Google Finance, NSE, economic calendar
    bhavcopy_dir: str = "./data/bhavcopy"
    cache_ttl_minutes: int = 15
    stale_threshold_minutes: int = 30
    backfill_days: int = 365  # days of history to fetch in backfill-data skill


class HeartbeatConfig(BaseModel):
    market_hours_interval_min: int = 15
    off_hours_interval_min: int = 60
    max_consecutive_skips: int = 3


class ScanningWeights(BaseModel):
    technical: float = 0.40
    volume_momentum: float = 0.25
    news_sentiment: float = 0.20
    fundamental: float = 0.15

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "ScanningWeights":
        total = self.technical + self.volume_momentum + self.news_sentiment + self.fundamental
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Scanning weights must sum to 1.0, got {total:.4f}"
            )
        return self


class ScanningConfig(BaseModel):
    universe: str = "nifty500"  # "nifty500", "nifty50", "all"
    universe_cron: str = "30 8 * * 1-5"  # daily 8:30 AM IST on weekdays
    seed_symbols: list[str] = Field(
        default_factory=lambda: ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    )
    shortlist_size: int = 25
    min_avg_daily_volume: int = 500_000
    weights: ScanningWeights = Field(default_factory=ScanningWeights)


class IndicatorsConfig(BaseModel):
    rsi: bool = True
    macd: bool = True
    bollinger_bands: bool = True
    vwap: bool = True
    atr: bool = True
    volume_profile: bool = True
    obv: bool = True
    supertrend: bool = True


class StrategyConfig(BaseModel):
    ema_periods: list[int] = Field(default_factory=lambda: [9, 21, 50, 200])
    indicators: IndicatorsConfig = Field(default_factory=IndicatorsConfig)
    default_trade_type: Literal["intraday", "swing"] = "intraday"
    min_training_samples: int = 200


class RiskConfig(BaseModel):
    max_risk_per_trade_pct: float = Field(default=0.02, gt=0, lt=1)
    max_portfolio_exposure_pct: float = Field(default=0.60, gt=0, le=1)
    max_open_positions: int = Field(default=3, ge=1)
    max_single_stock_pct: float = Field(default=0.25, gt=0, le=1)
    daily_loss_limit_pct: float = Field(default=0.03, gt=0, lt=1)
    weekly_loss_limit_pct: float = Field(default=0.05, gt=0, lt=1)
    weekly_loss_sizing_reduction: float = Field(default=0.50, gt=0, le=1)
    mandatory_stop_loss: bool = True
    trailing_sl_enabled: bool = True
    trailing_sl_trigger_multiple: float = Field(default=1.5, gt=0)
    trailing_sl_step_pct: float = Field(default=0.005, gt=0, lt=1)
    llm_review_enabled: bool = True
    llm_fallback_to_rules: bool = True
    max_same_sector_positions: int = Field(default=1, ge=1)
    kill_switch_enabled: bool = True
    kill_switch_persistent: bool = True
    min_confidence_score: float = Field(default=0.65, ge=0, le=1)
    max_trades_per_day: int = Field(default=10, ge=1)
    loss_cooldown_minutes: int = Field(default=15, ge=0)
    symbol_cooldown_days: int = Field(default=1, ge=0)
    symbol_repeat_lookback_days: int = Field(default=5, ge=0)
    symbol_repeat_min_confidence: float = Field(default=0.80, ge=0, le=1)
    margin_usage_enabled: bool = False  # when False, position value capped by available cash (no leverage)
    weekly_reset_day: str = "monday"  # day when weekly circuit breaker resets


class MarketHoursConfig(BaseModel):
    open: str = "09:15"
    close: str = "15:30"
    order_start: str = "09:15"
    order_end: str = "15:15"
    square_off: str = "15:15"
    square_off_extension: str = "00:05"
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
    retry_base_delay_sec: int = 2
    paper_slippage_pct: float = Field(default=0.001, ge=0)
    order_timeout_sec: int = 30
    price_drift_max_pct: float = Field(default=0.02, gt=0, lt=1)
    transaction_mode: Literal["auto", "manual"] = "auto"  # manual = require approval before execution


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
    news_days: int = 30
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
