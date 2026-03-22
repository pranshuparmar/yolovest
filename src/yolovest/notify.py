"""Notification system for YoloVest.

Supports console backend (always available) and Telegram (optional).
Respects enabled/disabled toggle from config.
"""

import logging
from abc import ABC, abstractmethod

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

    async def send_trade_alert(self, trade: dict) -> None:
        """Send a trade entry/exit alert."""
        symbol = trade.get("symbol", "?")
        signal_type = trade.get("signal_type", "?")
        qty = trade.get("quantity", 0)
        fill = trade.get("fill_price", trade.get("entry_price", 0))
        sl = trade.get("stop_loss_price", 0)
        target = trade.get("target_price", 0)
        mode = trade.get("mode", "paper")
        msg = (
            f"Trade Alert [{mode.upper()}]: {signal_type} {symbol} "
            f"qty={qty} @ {fill:.2f} SL={sl:.2f} T={target:.2f}"
        )
        await self.send(msg)


class Notifier:
    """Full notifier with config-based routing and message tracking."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._enabled = True
        self._sent_messages: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    async def send(self, message: str) -> bool:
        """Send a notification message.

        Returns True if the message was delivered to at least one backend.
        """
        if not self._enabled:
            return False

        delivered = False

        # Console backend (always available)
        self._sent_messages.append(message)
        delivered = True

        # Telegram backend (if enabled)
        if self._config.notifications.telegram.enabled:
            delivered = await self._send_telegram(message) or delivered

        return delivered

    async def _send_telegram(self, message: str) -> bool:
        """Send via Telegram bot. Stub — implementation in Phase 1."""
        return False

    async def send_trade_alert(self, trade: dict) -> None:
        """Send a trade entry/exit alert via all configured backends."""
        symbol = trade.get("symbol", "?")
        signal_type = trade.get("signal_type", "?")
        qty = trade.get("quantity", 0)
        fill = trade.get("fill_price", trade.get("entry_price", 0))
        sl = trade.get("stop_loss_price", 0)
        target = trade.get("target_price", 0)
        mode = trade.get("mode", "paper")
        msg = (
            f"Trade Alert [{mode.upper()}]: {signal_type} {symbol} "
            f"qty={qty} @ {fill:.2f} SL={sl:.2f} T={target:.2f}"
        )
        await self.send(msg)

    @property
    def sent_messages(self) -> list[str]:
        """Messages sent via console backend (useful for testing)."""
        return list(self._sent_messages)
