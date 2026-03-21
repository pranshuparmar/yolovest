"""Abstract broker interface (ABC).

All broker implementations (Zerodha, paper, etc.) extend BrokerBase.
See REQUIREMENTS.md FR-6 for order execution requirements.
"""

from abc import ABC, abstractmethod
from typing import Any


class BrokerBase(ABC):
    """Abstract base for broker integrations.

    Methods cover the full order lifecycle: placement, cancellation,
    status tracking, position queries, authentication, and margin checks.
    """

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,  # "BUY" or "SELL"
        quantity: int,
        order_type: str,  # "MARKET", "LIMIT", "SL", "SL-M"
        product: str,  # "MIS" or "CNC"
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> str:
        """Place an order and return the order ID."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Get current status of an order."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        """Get all current positions from the broker."""
        ...

    @abstractmethod
    async def get_pending_orders(self) -> list[dict[str, Any]]:
        """Get all pending/open orders."""
        ...

    @abstractmethod
    async def is_authenticated(self) -> bool:
        """Check if the broker session is authenticated and valid."""
        ...

    @abstractmethod
    async def get_margins(self) -> dict[str, Any]:
        """Get available margins/funds from the broker."""
        ...
