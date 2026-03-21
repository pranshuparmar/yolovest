"""Application context shared across all skills.

AppContext is a dataclass holding references to config, database, broker,
LLM, market data, notifier, market hours checker, and event bus.
Uses Protocol types so concrete implementations can be swapped.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Protocol, runtime_checkable

from yolovest.config import AppConfig
from yolovest.events import EventBus
from yolovest.models.schemas import OHLCVBar


# ---------------------------------------------------------------------------
# Protocol types for pluggable backends
# ---------------------------------------------------------------------------


@runtime_checkable
class BrokerProtocol(Protocol):
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
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]: ...

    async def get_quote(self, symbol: str) -> dict[str, Any]: ...

    async def health_check(self) -> bool: ...


@runtime_checkable
class NotifierProtocol(Protocol):
    async def send(self, message: str) -> None: ...


@runtime_checkable
class DatabaseProtocol(Protocol):
    async def health_check(self) -> bool: ...

    async def is_kill_switch_active(self) -> bool: ...

    async def get_open_positions(self) -> list[Any]: ...


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

    def _parse_time(self, time_str: str) -> time:
        """Parse HH:MM string to time object."""
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))

    def is_market_hours(self, now: datetime | None = None) -> bool:
        """Check if the current time is within market hours."""
        if now is None:
            now = datetime.now()

        # Check if today is a weekend
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        # Check if today is a holiday
        if self.is_holiday(now.date()):
            return False

        current_time = now.time()
        market_open = self._parse_time(self._mh.open)
        market_close = self._parse_time(self._mh.close)

        return market_open <= current_time <= market_close

    def is_holiday(self, check_date: date | None = None) -> bool:
        """Check if a date is an NSE holiday."""
        if check_date is None:
            check_date = date.today()

        date_str = check_date.isoformat()
        return date_str in self._mh.holidays

    def get_square_off_time(self, check_date: date | None = None) -> time:
        """Get the square-off time, accounting for early close days."""
        if check_date is None:
            check_date = date.today()

        date_str = check_date.isoformat()

        # Check for early close
        if date_str in self._mh.early_close_days:
            early_close = self._mh.early_close_days[date_str]
            return self._parse_time(early_close)

        return self._parse_time(self._mh.square_off)

    def is_order_window(self, now: datetime | None = None) -> bool:
        """Check if the current time is within the order placement window."""
        if now is None:
            now = datetime.now()

        if not self.is_market_hours(now):
            return False

        current_time = now.time()
        order_start = self._parse_time(self._mh.order_start)
        order_end = self._parse_time(self._mh.order_end)

        return order_start <= current_time <= order_end


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
