"""Abstract broker interface (ABC).

All broker implementations (Zerodha, paper, etc.) extend BrokerBase.
"""

from abc import ABC, abstractmethod
from typing import Any


class BrokerBase(ABC):
    """Abstract base for broker integrations.

    Methods cover the full order lifecycle: placement, cancellation,
    status tracking, position queries, authentication, and margin checks.
    """

    @abstractmethod
    async def authenticate(self, request_token: str) -> bool:
        """Exchange a request token for an authenticated session (daily re-auth)."""
        ...

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

    @abstractmethod
    async def modify_sl_order(
        self, order_id: str, new_trigger_price: float
    ) -> bool:
        """Modify the trigger price of an existing stop-loss order."""
        ...

    @abstractmethod
    async def get_holdings(self) -> list[dict[str, Any]]:
        """Get all CNC holdings from the broker (delivery stocks held overnight)."""
        ...

    async def compute_charges(
        self, legs: list[dict[str, Any]]
    ) -> list[dict[str, float]] | None:
        """Return actual per-leg charges from the broker, or None if unsupported.

        Each input leg is a dict with: exchange, tradingsymbol, transaction_type,
        variety, product, order_type, quantity, average_price. Returns a list of
        charges breakdowns in the same order — each dict carries `brokerage`,
        `stt`, `other_charges`, `total`. Returning None lets callers fall back
        to a config-based estimate (e.g. paper mode, broker offline).
        """
        return None

    def get_login_url(self) -> str:
        """Get the broker login URL for daily re-authentication."""
        return ""
