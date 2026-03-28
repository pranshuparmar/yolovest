"""Notification system for YoloVest.

Supports console backend (always available) and Telegram (optional).
Respects enabled/disabled toggle and per-alert-type config from config.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from yolovest.config import AppConfig

logger = logging.getLogger(__name__)


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

    async def send_exit_alert(self, symbol: str, reason: str, pnl: float) -> None:
        """Send a trade exit alert."""
        emoji = "+" if pnl >= 0 else ""
        await self.send(f"Exit: {symbol} — {reason} — PnL: {emoji}{pnl:.2f}")

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
        self._sent_messages: list[str] = []
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

    async def send(self, message: str) -> bool:
        """Send a notification message.

        Returns True if the message was delivered to at least one backend.
        """
        if not self._enabled:
            return False

        delivered = False

        # Console backend (always available)
        logger.info("[NOTIFY] %s", message)
        self._sent_messages.append(message)
        delivered = True

        # Telegram backend (if enabled and bot is set)
        if self._config.notifications.telegram.enabled and self._telegram_bot:
            try:
                result = await self._telegram_bot.send_message(message)  # type: ignore[attr-defined]
                delivered = result or delivered
            except Exception as e:
                logger.warning("Telegram send failed: %s", e)

        return delivered

    async def send_trade_alert(self, trade: dict[str, Any]) -> None:
        """Send a trade entry alert via all configured backends."""
        alerts_cfg = self._config.notifications.telegram.alerts
        msg = _format_trade_alert(trade)

        if not alerts_cfg.trade_entry:
            logger.info("[TRADE] %s", msg)
            self._sent_messages.append(msg)
            return

        await self.send(msg)

    async def send_exit_alert(self, symbol: str, reason: str, pnl: float) -> None:
        """Send a trade exit alert (target/SL hit, square-off)."""
        alerts_cfg = self._config.notifications.telegram.alerts
        emoji = "+" if pnl >= 0 else ""
        msg = f"Exit: {symbol} — {reason} — PnL: {emoji}{pnl:.2f}"

        if not alerts_cfg.trade_exit:
            logger.info("[EXIT] %s", msg)
            self._sent_messages.append(msg)
            return

        await self.send(msg)

    async def send_error_alert(self, error: str) -> None:
        """Send an error alert."""
        alerts_cfg = self._config.notifications.telegram.alerts
        msg = f"Error: {error}"

        if not alerts_cfg.errors:
            logger.warning("[ERROR ALERT SUPPRESSED] %s", error)
            return

        await self.send(msg)

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
    return (
        f"Trade Alert [{mode.upper()}]: {signal_type} {symbol} "
        f"qty={qty} @ {fill:.2f} SL={sl:.2f} T={target:.2f}"
    )
