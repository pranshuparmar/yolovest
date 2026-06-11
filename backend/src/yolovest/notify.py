"""Notification system for YoloVest.

Supports console backend (always available) and Telegram (optional).
Respects enabled/disabled toggle and per-alert-type config from config.
"""

import logging
from abc import ABC, abstractmethod
from collections import deque
from typing import Any

from yolovest.config import AppConfig

logger = logging.getLogger(__name__)

# Max messages retained in memory (prevents unbounded growth over days/weeks)
_MAX_SENT_MESSAGES = 1000


class NotifierBase(ABC):
    """Abstract notifier interface."""

    @abstractmethod
    async def send(self, message: str) -> None:
        """Send a notification message."""
        ...


class ConsoleNotifier(NotifierBase):
    """Development notifier that prints to console/log."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    async def send(self, message: str) -> None:
        if not self._enabled:
            return
        logger.info("[NOTIFY] %s", message)

    async def send_trade_alert(self, trade: dict[str, Any]) -> None:
        """Send a trade entry alert."""
        msg = _format_trade_alert(trade)
        await self.send(msg)

    async def send_exit_alert(self, symbol: str, reason: str, pnl: float, product: str = "") -> None:
        """Send a trade exit alert."""
        sign = "+" if pnl >= 0 else ""
        prod_str = f" [{product}]" if product else ""
        await self.send(f"Exit: {symbol}{prod_str} — {reason} — PnL: {sign}{pnl:.2f}")

    async def send_error_alert(self, error: str) -> None:
        """Send an error alert."""
        await self.send(f"Error: {error}")


class Notifier:
    """Full notifier with config-based routing and message tracking.

    Supports console (always) + Telegram (when configured) backends.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._enabled = True
        self._sent_messages: deque[str] = deque(maxlen=_MAX_SENT_MESSAGES)
        self._telegram_bot: object | None = None  # set by main.py after bot creation

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def set_telegram_bot(self, bot: object) -> None:
        """Set the Telegram bot reference for sending messages."""
        self._telegram_bot = bot

    # Maps alert_type names to TelegramAlertsConfig field names
    _ALERT_TYPE_MAP: dict[str, str] = {
        "trade_entry": "trade_entry",
        "trade_exit": "trade_exit",
        "errors": "errors",
        "daily_summary": "daily_summary",
        "weekly_summary": "weekly_summary",
        "kill_switch": "kill_switch",
    }

    def _is_alert_enabled(self, alert_type: str | None) -> bool:
        """Check if a specific alert type is enabled in telegram config."""
        if alert_type is None:
            return True
        field = self._ALERT_TYPE_MAP.get(alert_type)
        if field is None:
            return True
        return bool(getattr(self._config.notifications.telegram.alerts, field, True))

    async def send(self, message: str, *, alert_type: str | None = None) -> bool:
        """Send a notification message.

        Args:
            message: The notification text.
            alert_type: Optional alert category (e.g. "errors", "trade_entry").
                When set, the corresponding telegram.alerts toggle is checked
                before sending to Telegram. Console always receives the message.

        Returns True if the message was delivered to at least one backend.
        """
        if not self._enabled:
            return False

        delivered = False

        # Console backend (always available)
        logger.info("[NOTIFY] %s", message)
        self._sent_messages.append(message)
        delivered = True

        # Telegram backend (if enabled, bot is set, and alert type is allowed)
        if (
            self._config.notifications.telegram.enabled
            and self._telegram_bot
            and self._is_alert_enabled(alert_type)
        ):
            try:
                result = await self._telegram_bot.send_message(message)  # type: ignore[attr-defined]
                delivered = result or delivered
            except Exception as e:
                logger.warning("Telegram send failed: %s", e)

        return delivered

    async def send_trade_alert(self, trade: dict[str, Any]) -> None:
        """Send a trade entry alert via all configured backends."""
        msg = _format_trade_alert(trade)
        await self.send(msg, alert_type="trade_entry")

    async def send_exit_alert(self, symbol: str, reason: str, pnl: float, product: str = "") -> None:
        """Send a trade exit alert (target/SL hit, square-off)."""
        sign = "+" if pnl >= 0 else ""
        prod_str = f" [{product}]" if product else ""
        msg = f"Exit: {symbol}{prod_str} — {reason} — PnL: {sign}{pnl:.2f}"
        await self.send(msg, alert_type="trade_exit")

    async def send_error_alert(self, error: str) -> None:
        """Send an error alert."""
        msg = f"Error: {error}"
        await self.send(msg, alert_type="errors")

    @property
    def sent_messages(self) -> list[str]:
        """Messages sent via console backend (useful for testing)."""
        return list(self._sent_messages)


def _format_trade_alert(trade: dict[str, Any]) -> str:
    """Format a trade dict into a human-readable alert message."""
    symbol = trade.get("symbol", "?")
    signal_type = trade.get("signal_type", "?")
    qty = trade.get("quantity", 0)
    fill = trade.get("fill_price", trade.get("entry_price", 0))
    sl = trade.get("stop_loss_price", 0)
    target = trade.get("target_price", 0)
    mode = trade.get("mode", "paper")
    product = trade.get("product", "MIS")
    hold_days = trade.get("expected_holding_days")
    hold_str = f" ({hold_days}d)" if hold_days is not None else ""
    override = " [OVERRIDE]" if trade.get("is_override") else ""
    manual = " [MANUAL]" if trade.get("is_manual") else ""
    return (
        f"Trade [{mode.upper()}]: {signal_type} {symbol} "
        f"{product}{hold_str} qty={qty} @ {fill:.2f} "
        f"SL={sl:.2f} T={target:.2f}{override}{manual}"
    )
