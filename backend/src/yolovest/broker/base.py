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

    async def logout(self) -> None:
        """Drop any cached session state so the next is_authenticated()
        check returns False. Default no-op for brokers without a
        persistent session concept (e.g. paper). Subclasses should
        override when they hold a token or DB-persisted state.
        """
        return None

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
        tag: str | None = None,  # ≤20 chars, flows back via orders() and postbacks
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

    async def modify_order(
        self,
        order_id: str,
        *,
        price: float | None = None,
        quantity: int | None = None,
        trigger_price: float | None = None,
        order_type: str | None = None,
    ) -> bool:
        """Generic order modification. Default implementation defers to
        modify_sl_order for trigger-only changes; concrete brokers
        override to support full price/qty/type modification.
        """
        if trigger_price is not None and price is None and quantity is None and order_type is None:
            return await self.modify_sl_order(order_id, trigger_price)
        raise NotImplementedError(
            f"{type(self).__name__} does not implement generic modify_order",
        )

    async def get_orders(self) -> list[dict[str, Any]]:
        """Return today's full order book. Default implementation
        narrows to pending orders; concrete brokers override.
        """
        return await self.get_pending_orders()

    async def initiate_holdings_auth(
        self, holdings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Initiate CDSL TPIN authorisation for selling holdings.

        Default returns None (broker doesn't support / not authenticated);
        concrete brokers override. UI falls back to a static help URL
        when this returns None.
        """
        return None

    @abstractmethod
    async def get_holdings(self) -> list[dict[str, Any]]:
        """Get all CNC holdings from the broker (delivery stocks held overnight)."""
        ...

    async def convert_position(
        self,
        symbol: str,
        quantity: int,
        from_product: str,
        to_product: str,
        side: str = "BUY",
    ) -> bool:
        """Convert an existing open position between product types (e.g.
        MIS → CNC, taking delivery of intraday shares before square-off).
        Returns True on success; default implementation is a no-op
        returning False.

        Zerodha allows MIS → CNC and CNC → MIS for equity. Margin
        requirements change between products; the caller must have
        already verified the new requirement fits available margin.
        """
        return False

    async def estimate_margin(
        self, legs: list[dict[str, Any]],
    ) -> dict[str, float] | None:
        """Return the broker's canonical margin estimate for a proposed
        order (or basket of orders). Each leg dict has: exchange,
        tradingsymbol, transaction_type, variety, product, order_type,
        quantity, price (and trigger_price for SL).

        Result dict carries at minimum `total` (margin required across
        all legs) plus a `charges` sub-dict on a per-leg basis when the
        broker returns one. Returning None signals the caller to fall
        back to a naive `entry × qty` notional check.
        """
        return None

    async def get_order_history(self, order_id: str) -> list[dict[str, Any]]:
        """Return the state-transition timeline of a single order
        (placed → modified → triggered → complete, with timestamps and
        broker-side notes). Empty list when unavailable."""
        return []

    async def get_order_trades(self, order_id: str) -> list[dict[str, Any]]:
        """Return individual fill records for a single order — important
        when partial fills compose the full quantity. Empty list when
        unavailable."""
        return []

    async def get_executed_trades(self) -> list[dict[str, Any]]:
        """Return today's executed trades from the broker.

        Each entry should carry at minimum: tradingsymbol, transaction_type
        ("BUY"/"SELL"), quantity, average_price, exchange, fill_timestamp.
        Used by ghost-position reconciliation to recover the actual broker
        fill price when a position is closed outside the system. Default
        implementation returns an empty list — callers must tolerate it.
        """
        return []

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

    def tick_for(self, symbol: str) -> float:
        """Return the tick size for `symbol`. Default 0.05 (NSE equity
        standard) when the implementation doesn't carry a per-symbol
        map. Concrete brokers should override to expose the warmed
        cache so prices upstream of order-placement (signal target /
        SL, manual trade entry) can snap to the same grid the broker
        will enforce."""
        return 0.05

    def round_to_tick(self, symbol: str, price: float) -> float:
        """Snap `price` to the symbol's tick grid. Wrapper around
        tick_for so callers don't need to know the tick size."""
        tick = self.tick_for(symbol)
        if tick <= 0:
            tick = 0.05
        return round(round(price / tick) * tick, 2)
