"""Application context shared across all skills.

AppContext is a dataclass holding references to config, database, broker,
LLM, market data, notifier, market hours checker, and event bus.
Uses Protocol types so concrete implementations can be swapped.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from yolovest.config import AppConfig
from yolovest.events import EventBus
from yolovest.models.schemas import MLPrediction, OHLCVBar

# ---------------------------------------------------------------------------
# Protocol types for pluggable backends
# ---------------------------------------------------------------------------


@runtime_checkable
class BrokerProtocol(Protocol):
    async def authenticate(self, request_token: str) -> bool: ...

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        product: str,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> str: ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def get_order_status(self, order_id: str) -> dict[str, Any]: ...

    async def get_positions(self) -> list[dict[str, Any]]: ...

    async def get_pending_orders(self) -> list[dict[str, Any]]: ...

    async def is_authenticated(self) -> bool: ...

    async def get_margins(self) -> dict[str, Any]: ...

    async def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool: ...


@runtime_checkable
class LLMProtocol(Protocol):
    async def ping(self) -> bool: ...

    async def review_trade(self, context: Any) -> Any: ...

    async def analyze_sentiment(self, symbol: str, headlines: list[str]) -> Any: ...

    async def summarize_with_web_grounding(self, prompt: str) -> Any: ...

    async def validate_watchlist(
        self,
        shortlist: list[dict[str, object]],
        sector_analysis: dict[str, object],
        premarket_context: dict[str, object],
    ) -> Any: ...

    async def summarize_market_day(self) -> Any: ...

    async def analyze_prediction_failures(
        self, failures: list[dict[str, object]]
    ) -> Any: ...


@runtime_checkable
class MarketDataProtocol(Protocol):
    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30,
        *, skip_stale_check: bool = False,
    ) -> list[OHLCVBar]: ...

    async def get_quote(self, symbol: str) -> dict[str, Any]: ...

    async def get_ltp(self, symbol: str) -> float: ...

    async def health_check(self) -> bool: ...


@runtime_checkable
class MLProtocol(Protocol):
    async def predict_intraday(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction: ...

    async def predict_swing(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction: ...

    async def train(
        self, model_type: str, x: Any, y: Any, params: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def save_model(self, model_type: str, metrics: dict[str, Any]) -> str: ...

    def has_shadow(self, model_type: str) -> bool: ...

    def clear_shadow(self, model_type: str) -> None: ...

    async def load_shadow_model(self, model_type: str, version: str | None = None) -> None: ...

    async def predict_shadow_intraday(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction | None: ...

    async def predict_shadow_swing(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction | None: ...

    async def load_model(
        self, model_type: str, version: str | None = None
    ) -> None: ...

    async def get_production_metrics(self, model_type: str) -> dict[str, Any]: ...

    async def deploy_shadow(
        self, model_type: str, version: str, days: int
    ) -> None: ...


@runtime_checkable
class NotifierProtocol(Protocol):
    async def send(self, message: str) -> bool | None: ...

    async def send_trade_alert(self, trade: dict[str, Any]) -> None: ...


@runtime_checkable
class DatabaseProtocol(Protocol):
    async def health_check(self) -> bool: ...

    async def is_kill_switch_active(self) -> bool: ...

    async def get_open_positions(self) -> list[Any]: ...

    async def upsert_ohlcv(
        self, symbol: str, interval: str, bars: list[OHLCVBar], source: str
    ) -> int: ...

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]: ...

    async def set_system_state(self, key: str, value: str) -> None: ...

    async def get_system_state(self, key: str) -> str | None: ...

    async def log_audit(
        self,
        action_type: str,
        skill_name: str | None = None,
        input_summary: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> None: ...

    async def get_portfolio_state(self) -> dict[str, Any]: ...

    async def get_stock_sector(self, symbol: str) -> str | None: ...

    async def log_llm_review(
        self,
        signal: dict[str, Any],
        decision: str,
        reasoning: str,
        adjusted_size: int | None = None,
    ) -> None: ...

    async def get_sector_rotation(self) -> dict[str, Any]: ...

    async def get_todays_trades(self) -> list[dict[str, Any]]: ...

    async def get_latest_sentiment(self, symbol: str) -> dict[str, Any] | None: ...

    async def insert_trade(self, trade: dict[str, Any]) -> str: ...

    async def insert_signal(self, signal: dict[str, Any]) -> None: ...

    async def upsert_sentiment(self, symbol: str, sentiment: Any) -> None: ...

    async def update_position_sl(
        self, position_id: int | str, new_sl: float
    ) -> None: ...

    async def update_unrealized_pnl(
        self, position_id: int | str, current_price: float
    ) -> None: ...

    async def close_position(
        self, position_id: int | str, exit_price: float, pnl: float
    ) -> None: ...

    async def insert_prediction(self, prediction: dict[str, Any]) -> str: ...

    async def get_unscored_predictions(self) -> list[dict[str, Any]]: ...

    async def score_prediction(
        self,
        prediction_id: str,
        actual_price: float,
        direction_correct: bool,
        target_hit: bool,
        actual_pnl_pct: float,
    ) -> None: ...

    async def refresh_prediction_scoreboard(self) -> None: ...

    async def get_prediction_scoreboard(
        self, group_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_todays_predictions(self) -> list[dict[str, Any]]: ...

    async def get_weekly_trades(self) -> list[dict[str, Any]]: ...

    async def get_weekly_predictions(self) -> list[dict[str, Any]]: ...

    async def get_weekly_llm_reviews(self) -> list[dict[str, Any]]: ...

    async def store_report(self, report: dict[str, Any]) -> None: ...

    async def get_shadow_models_ready(self, shadow_mode_days: int) -> list[dict[str, Any]]: ...

    async def retire_model(self, model_type: str, version: str) -> None: ...

    async def get_trades_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    async def get_equity_curve(self, days: int = 30) -> list[dict[str, Any]]: ...

    async def get_trade_detail(self, trade_id: str) -> dict[str, Any] | None: ...

    async def get_reports_history(
        self,
        report_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]: ...

    async def get_audit_log(
        self, limit: int = 50, action_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def upsert_economic_events(self, events: list[dict[str, Any]]) -> int: ...

    async def get_upcoming_economic_events(
        self, days: int = 7, country: str | None = None, event_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get_earnings_events(
        self, symbol: str | None = None, days: int = 30
    ) -> list[dict[str, Any]]: ...

    async def upsert_fundamentals(self, symbol: str, data: dict[str, Any]) -> None: ...

    async def get_watchlist(self) -> list[dict[str, Any]]: ...

    async def upsert_watchlist(self, stocks: list[dict[str, Any]]) -> None: ...

    async def get_latest_premarket(self) -> dict[str, Any]: ...

    async def upsert_premarket(self, data: dict[str, Any]) -> None: ...

    async def get_nse_universe(self) -> list[dict[str, Any]]: ...

    async def backup(self, backup_dir: str) -> str: ...

    async def run_retention_cleanup(
        self, ohlcv_days: int = 730, audit_days: int = 365, predictions_days: int = 365
    ) -> dict[str, Any]: ...

    async def get_prediction_outcomes(self) -> list[dict[str, Any]]: ...

    async def store_failure_analysis(self, analysis: object) -> None: ...

    async def get_slippage_stats(
        self, symbol: str | None = None, days: int = 30
    ) -> dict[str, Any]: ...

    async def get_llm_review_accuracy(
        self, days: int = 30
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Market Hours Checker
# ---------------------------------------------------------------------------


class MarketHoursChecker:
    """Check market hours, holidays, and square-off times.

    Uses config for market hours, holiday list, and timezone.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._mh = config.market_hours
        self._tz = ZoneInfo(config.market_hours.timezone)

    def _parse_time(self, time_str: str) -> time:
        """Parse HH:MM string to time object."""
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))

    def _now(self) -> datetime:
        """Current time in configured timezone."""
        return datetime.now(self._tz)

    def is_market_hours(self, now: datetime | None = None) -> bool:
        """Check if the current time is within market hours (in configured timezone)."""
        if now is None:
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)

        # Check if today is a weekend
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        # Check if today is a holiday
        if self.is_holiday(now.date()):
            return False

        current_time = now.time()
        market_open = self._parse_time(self._mh.open)
        market_close = self._parse_time(self._mh.close)

        # On early close days, use the early close time instead
        date_str = now.date().isoformat()
        if date_str in self._mh.early_close_days:
            market_close = self._parse_time(self._mh.early_close_days[date_str])

        return market_open <= current_time <= market_close

    def is_holiday(self, check_date: date | None = None) -> bool:
        """Check if a date is an NSE holiday."""
        if check_date is None:
            check_date = self._now().date()

        date_str = check_date.isoformat()
        return date_str in self._mh.holidays

    def get_square_off_time(self, check_date: date | None = None) -> time:
        """Get the square-off time, accounting for early close days."""
        if check_date is None:
            check_date = self._now().date()

        date_str = check_date.isoformat()

        # Check for early close
        if date_str in self._mh.early_close_days:
            early_close = self._mh.early_close_days[date_str]
            return self._parse_time(early_close)

        return self._parse_time(self._mh.square_off)

    def is_order_window(self, now: datetime | None = None) -> bool:
        """Check if the current time is within the order placement window.

        On early close days, the order window end is adjusted
        to the early square-off time so no new orders are placed too late.
        """
        if now is None:
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)

        if not self.is_market_hours(now):
            return False

        current_time = now.time()
        order_start = self._parse_time(self._mh.order_start)
        order_end = self._parse_time(self._mh.order_end)

        # On early close days, cap order window at square-off time
        sq_time = self.get_square_off_time(now.date())
        if sq_time < order_end:
            order_end = sq_time

        return order_start <= current_time <= order_end

    def is_early_close_day(self, check_date: date | None = None) -> bool:
        """Check if a date is an early close day."""
        if check_date is None:
            check_date = self._now().date()
        return check_date.isoformat() in self._mh.early_close_days

    def is_premarket_window(self, now: datetime | None = None) -> bool:
        """Check if now is in the pre-market window (before market open).

        Pre-market: 8:00 AM to market open (e.g. 9:15 AM).
        Used by ingest-premarket skill.
        """
        if now is None:
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)

        if now.weekday() >= 5:
            return False
        if self.is_holiday(now.date()):
            return False

        current_time = now.time()
        premarket_start = time(8, 0)
        market_open = self._parse_time(self._mh.open)

        return premarket_start <= current_time < market_open

    def is_square_off_window(self, now: datetime | None = None) -> bool:
        """Check if now is within the square-off window.

        Square-off window: from square_off time to square_off + extension.
        """
        if now is None:
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)

        if now.weekday() >= 5:
            return False
        if self.is_holiday(now.date()):
            return False

        current_time = now.time()
        sq_time = self.get_square_off_time(now.date())

        # Parse extension (HH:MM format)
        ext_parts = self._mh.square_off_extension.split(":")
        ext_minutes = int(ext_parts[0]) * 60 + int(ext_parts[1])

        from datetime import timedelta

        sq_dt = datetime.combine(now.date(), sq_time)
        sq_end_dt = sq_dt + timedelta(minutes=ext_minutes)
        sq_end_time = sq_end_dt.time()

        return sq_time <= current_time <= sq_end_time


# ---------------------------------------------------------------------------
# Application Context
# ---------------------------------------------------------------------------


@dataclass
class AppContext:
    """Shared context object passed to all skills via self.ctx.

    Holds references to all major subsystems. Skills access these
    through protocols, allowing concrete implementations to be swapped.
    """

    config: AppConfig
    db: DatabaseProtocol
    broker: BrokerProtocol
    llm: LLMProtocol
    market_data: MarketDataProtocol
    notify: NotifierProtocol
    market_hours: MarketHoursChecker
    event_bus: EventBus = field(default_factory=EventBus)
    ml: MLProtocol | None = None
    news_aggregator: Any = None
    memory: Any = None
