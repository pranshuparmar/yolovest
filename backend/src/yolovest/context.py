"""Application context shared across all skills.

AppContext is a dataclass holding references to config, database, broker,
LLM, market data, notifier, market hours checker, and event bus.
Uses Protocol types so concrete implementations can be swapped.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from yolovest.config import AppConfig
from yolovest.events import EventBus
from yolovest.models.schemas import MLPrediction, OHLCVBar

if TYPE_CHECKING:
    from yolovest.data.db import Database

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
        tag: str | None = None,
    ) -> str: ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def get_order_status(self, order_id: str) -> dict[str, Any]: ...

    async def get_positions(self) -> list[dict[str, Any]]: ...

    async def get_pending_orders(self) -> list[dict[str, Any]]: ...

    async def is_authenticated(self) -> bool: ...

    async def get_margins(self) -> dict[str, Any]: ...

    async def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool: ...

    async def modify_order(
        self,
        order_id: str,
        *,
        price: float | None = None,
        quantity: int | None = None,
        trigger_price: float | None = None,
        order_type: str | None = None,
    ) -> bool: ...

    async def get_orders(self) -> list[dict[str, Any]]: ...

    async def initiate_holdings_auth(
        self, holdings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None: ...

    async def get_holdings(self) -> list[dict[str, Any]]: ...

    def get_login_url(self) -> str: ...

    async def logout(self) -> None: ...

    async def get_order_history(self, order_id: str) -> list[dict[str, Any]]: ...

    async def get_order_trades(self, order_id: str) -> list[dict[str, Any]]: ...

    async def convert_position(
        self,
        symbol: str,
        quantity: int,
        from_product: str,
        to_product: str,
        side: str = "BUY",
    ) -> bool: ...


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

    def get_shadow_version(self, model_type: str) -> str | None: ...

    def clear_shadow(self, model_type: str) -> None: ...

    def clear_model(self, model_type: str) -> None: ...

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

    def get_effective_thresholds(
        self, model_type: str,
    ) -> dict[str, float] | None: ...

    async def get_production_metrics(self, model_type: str) -> dict[str, Any]: ...

    async def deploy_shadow(
        self, model_type: str, version: str, days: int
    ) -> None: ...


@runtime_checkable
class NotifierProtocol(Protocol):
    async def send(
        self, message: str, *, alert_type: str | None = None,
    ) -> bool | None: ...

    async def send_trade_alert(self, trade: dict[str, Any]) -> None: ...


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

    def seconds_until_next_market_open(
        self, now: datetime | None = None,
    ) -> float:
        """Return seconds from `now` to the next market-open moment.

        - Returns 0.0 when the market is currently open.
        - Walks forward day by day across weekends + NSE holidays so a
          Friday-after-close gives ~63h until Monday's 9:15, etc.
        - Caps at 10 days of look-ahead so stacked-holiday weeks still
          terminate.

        Used by the heartbeat loop to avoid getting stuck in a 60-min
        off-hours sleep that straddles 9:15 AM — instead the next
        wake-up is scheduled for market-open, so the first
        market-hours heartbeat fires within a minute of opening bell.
        """
        from datetime import timedelta as _td
        if now is None:
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        if self.is_market_hours(now):
            return 0.0

        market_open = self._parse_time(self._mh.open)
        for offset in range(11):
            candidate_date = (now + _td(days=offset)).date()
            if not self.is_trading_day(candidate_date):
                continue
            candidate = datetime.combine(
                candidate_date, market_open, tzinfo=now.tzinfo,
            )
            if candidate > now:
                return (candidate - now).total_seconds()
        # Fallback — should never hit with the 10-day cap, but stay
        # safe rather than returning a negative interval.
        return 24 * 3600.0

    def seconds_until_next_order_window(
        self, now: datetime | None = None,
    ) -> float:
        """Return seconds from `now` to the next `order_start` moment.

        The heartbeat uses this (not `seconds_until_next_market_open`)
        so the first cycle of the day fires at order_start rather than
        market_open. A user who sets order_start=09:20 to skip opening
        volatility was previously seeing a 09:15 heartbeat fire,
        generate signals, and have every one of them risk-rejected with
        "Outside order window" — wasting one risk-rejected-retry slot
        per symbol before trading could actually begin.

        - Returns 0.0 when the market is open AND we're inside the
          order window.
        - Walks forward day by day across weekends + holidays.
        - Caps at 10 days of look-ahead.
        """
        from datetime import timedelta as _td
        if now is None:
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        if self.is_order_window(now):
            return 0.0

        order_start = self._parse_time(self._mh.order_start)
        for offset in range(11):
            candidate_date = (now + _td(days=offset)).date()
            if not self.is_trading_day(candidate_date):
                continue
            candidate = datetime.combine(
                candidate_date, order_start, tzinfo=now.tzinfo,
            )
            if candidate > now:
                return (candidate - now).total_seconds()
        return 24 * 3600.0

    def is_holiday(self, check_date: date | None = None) -> bool:
        """Check if a date is an NSE holiday."""
        if check_date is None:
            check_date = self._now().date()

        date_str = check_date.isoformat()
        return date_str in self._mh.holidays

    def is_trading_day(self, check_date: date) -> bool:
        """A date is a trading day when it's a weekday and not on the
        configured NSE holiday list.
        """
        return check_date.weekday() < 5 and not self.is_holiday(check_date)

    def most_recent_completed_trading_day(
        self, now: datetime | None = None,
    ) -> date:
        """Return the most recent date whose trading session has closed.

        - If `now` falls within today's market hours, today is in-progress
          → return the previous trading day.
        - If `now` is after today's close on a trading day → return today.
        - Otherwise walk back day-by-day until we find a trading day.

        Walks back at most 10 days to handle stacked holidays (e.g.
        Diwali week + adjacent weekend). Used by the signal-gen
        staleness gate to bound the "data should be at least this fresh"
        target.
        """
        from datetime import timedelta as _td
        if now is None:
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        today = now.date()
        # Today's session done?
        if self.is_trading_day(today):
            close_time = self._parse_time(self._mh.close)
            if (today.isoformat() in self._mh.early_close_days):
                close_time = self._parse_time(self._mh.early_close_days[today.isoformat()])
            if now.time() >= close_time:
                return today
        # Walk back until a closed trading day is found.
        d = today - _td(days=1)
        for _ in range(10):
            if self.is_trading_day(d):
                return d
            d -= _td(days=1)
        return d

    def trading_days_missing_after(self, start: date, end: date) -> int:
        """Count trading days in (start, end] — i.e. the number of
        expected trading sessions that fall AFTER `start` and up to
        and including `end`. Returns 0 when start >= end.

        Used by the signal-gen staleness gate to express "data goes
        up to start; the freshest expected session is end; therefore
        N trading sessions are missing from our store".
        """
        from datetime import timedelta as _td
        if start >= end:
            return 0
        n = 0
        d = start + _td(days=1)
        while d <= end:
            if self.is_trading_day(d):
                n += 1
            d += _td(days=1)
        return n

    def add_trading_days(self, start: date, n: int) -> date:
        """Return the date `n` trading days after `start` (holiday- and
        weekend-aware). n <= 0 returns `start` unchanged — an intraday
        signal (0-day horizon) targets the same session it's generated in.

        Used to derive a signal's target / predicted-exit date from its
        base date plus the model's expected holding-day horizon, so the
        UI can show "expected to close by <date>". Walks at most a few
        hundred calendar days as a safety bound.
        """
        from datetime import timedelta as _td
        if n <= 0:
            return start
        d = start
        added = 0
        for _ in range(n * 3 + 30):  # generous bound for stacked holidays
            d += _td(days=1)
            if self.is_trading_day(d):
                added += 1
                if added >= n:
                    return d
        return d

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
    # Concrete on purpose: there is exactly one Database implementation
    # (193 methods); a hand-maintained Protocol mirror drifted constantly.
    # Broker / LLM / market-data stay Protocols — those are real plug points.
    db: "Database"
    broker: BrokerProtocol
    llm: LLMProtocol
    market_data: MarketDataProtocol
    notify: NotifierProtocol
    market_hours: MarketHoursChecker
    event_bus: EventBus = field(default_factory=EventBus)
    ml: MLProtocol | None = None
    news_aggregator: Any = None
    memory: Any = None
    # KiteTicker WebSocket client — populated only when
    # market_data.kite_websocket_enabled is true and the broker is
    # authenticated. Skills can read latest LTP via ctx.ticker.get_ltp.
    ticker: Any = None
    # Heartbeat orchestrator handle — wired by main.async_main once the
    # orchestrator is constructed. The heartbeat-pipeline skill calls
    # `ctx.orchestrator.run_heartbeat()` to let the user trigger a full
    # heartbeat cycle on demand (Telegram /run, dashboard Skills page).
    # None until the orchestrator finishes booting.
    orchestrator: Any = None
