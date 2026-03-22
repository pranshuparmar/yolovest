"""Zerodha Kite Connect broker implementation.

Execution only (free tier, no market data). Supports paper + live modes.
See REQUIREMENTS.md FR-6.1, FR-6.2, FR-6.3, FR-6.8.
"""

import asyncio
import logging
from typing import Any

from yolovest.broker.base import BrokerBase

logger = logging.getLogger(__name__)


class ZerodhaBroker(BrokerBase):
    """Concrete broker using Zerodha Kite Connect API.

    In paper mode, all orders are simulated locally.
    In live mode, orders are placed via Kite Connect SDK.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        mode: str = "paper",
        paper_slippage_pct: float = 0.001,
        max_retries: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._mode = mode
        self._paper_slippage_pct = paper_slippage_pct
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._access_token: str | None = None
        self._kite: Any = None
        # Rate limiter: 8 concurrent to stay under Kite's 10 req/s (FR-6.8)
        self._rate_limiter = asyncio.Semaphore(8)
        # Paper mode state
        self._paper_orders: dict[str, dict[str, Any]] = {}
        self._paper_order_counter = 0

    def get_login_url(self) -> str:
        """Get the Kite Connect login URL for daily re-authentication."""
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={self._api_key}"

    # ------------------------------------------------------------------
    # Authentication (FR-6.3)
    # ------------------------------------------------------------------

    async def authenticate(self, request_token: str) -> bool:
        """Exchange request_token for access_token (daily re-auth)."""
        if self._mode == "paper":
            self._access_token = "paper_token"
            logger.info("Paper mode: authentication simulated")
            return True

        try:
            self._kite = await asyncio.to_thread(
                self._create_kite_session, request_token
            )
            self._access_token = self._kite.access_token
            logger.info("Kite Connect authenticated successfully")
            return True
        except Exception:
            logger.exception("Kite authentication failed")
            return False

    def _create_kite_session(self, request_token: str) -> Any:
        """Synchronous Kite session creation (runs in thread)."""
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=self._api_key)
        data = kite.generate_session(request_token, api_secret=self._api_secret)
        kite.set_access_token(data["access_token"])
        return kite

    async def is_authenticated(self) -> bool:
        if self._mode == "paper":
            return self._access_token is not None

        if self._kite is None or self._access_token is None:
            return False
        try:
            async with self._rate_limiter:
                await asyncio.to_thread(self._kite.profile)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Order Placement (FR-6.1, FR-6.2, FR-6.6)
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        product: str,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> str:
        if self._mode == "paper":
            return self._paper_place_order(
                symbol, side, quantity, order_type, product, price, trigger_price
            )

        return await self._live_place_order(
            symbol, side, quantity, order_type, product, price, trigger_price
        )

    def _paper_place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        product: str,
        price: float | None,
        trigger_price: float | None,
    ) -> str:
        """Simulate order placement in paper mode (FR-6.2)."""
        self._paper_order_counter += 1
        order_id = f"PAPER-{self._paper_order_counter}"

        fill_price = price or 0.0
        if order_type == "MARKET" and fill_price > 0:
            # Apply simulated slippage
            direction = 1 if side == "BUY" else -1
            fill_price *= 1 + direction * self._paper_slippage_pct

        self._paper_orders[order_id] = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
            "price": price,
            "trigger_price": trigger_price,
            "fill_price": fill_price,
            "status": "filled" if order_type == "MARKET" else "open",
        }

        logger.info(
            "Paper order placed: %s %s %s x%d @ %s",
            side, symbol, order_type, quantity, fill_price,
        )
        return order_id

    async def _live_place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        product: str,
        price: float | None,
        trigger_price: float | None,
    ) -> str:
        """Place order via Kite API with retry (FR-6.6)."""
        if self._kite is None:
            raise RuntimeError("Not authenticated")

        kite_side = "BUY" if side == "BUY" else "SELL"
        params: dict[str, Any] = {
            "tradingsymbol": symbol,
            "exchange": "NSE",
            "transaction_type": kite_side,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
        }
        if price is not None:
            params["price"] = price
        if trigger_price is not None:
            params["trigger_price"] = trigger_price

        return str(await self._retry_api_call(
            lambda: self._kite.place_order(variety="regular", **params)
        ))

    # ------------------------------------------------------------------
    # Order Management
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        if self._mode == "paper":
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "cancelled"
                return True
            return False

        try:
            async with self._rate_limiter:
                await asyncio.to_thread(
                    self._kite.cancel_order, variety="regular", order_id=order_id
                )
            return True
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        if self._mode == "paper":
            return self._paper_orders.get(order_id, {"status": "unknown"})

        async with self._rate_limiter:
            orders = await asyncio.to_thread(self._kite.orders)
        for order in orders:
            if order.get("order_id") == order_id:
                return dict[str, Any](order)
        return {"status": "unknown"}

    async def get_positions(self) -> list[dict[str, Any]]:
        if self._mode == "paper":
            return [
                o for o in self._paper_orders.values()
                if o["status"] in ("filled", "open")
            ]

        async with self._rate_limiter:
            positions = await asyncio.to_thread(self._kite.positions)
        return list(positions.get("net", []))

    async def get_pending_orders(self) -> list[dict[str, Any]]:
        if self._mode == "paper":
            return [o for o in self._paper_orders.values() if o["status"] == "open"]

        async with self._rate_limiter:
            orders = await asyncio.to_thread(self._kite.orders)
        return [o for o in orders if o.get("status") in ("OPEN", "PENDING")]

    async def get_margins(self) -> dict[str, Any]:
        if self._mode == "paper":
            return {"available": {"cash": 100_000}, "used": {"cash": 0}}

        async with self._rate_limiter:
            return await asyncio.to_thread(self._kite.margins)

    # ------------------------------------------------------------------
    # Modify SL Order (FR-5.8)
    # ------------------------------------------------------------------

    async def modify_sl_order(
        self, order_id: str, new_trigger_price: float
    ) -> bool:
        """Modify the trigger price of a stop-loss order."""
        if self._mode == "paper":
            logger.info(
                "[PAPER] Modify SL order %s → trigger=%.2f",
                order_id, new_trigger_price,
            )
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["trigger_price"] = new_trigger_price
            return True

        if not self._kite:
            raise RuntimeError("Not authenticated")

        def _modify() -> None:
            self._kite.modify_order(
                variety="regular",
                order_id=order_id,
                trigger_price=new_trigger_price,
            )

        await self._retry_api_call(_modify)
        return True

    # ------------------------------------------------------------------
    # Retry Helper (FR-6.6)
    # ------------------------------------------------------------------

    async def _retry_api_call(self, fn: Any) -> Any:
        """Retry with exponential backoff."""
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                async with self._rate_limiter:
                    result = await asyncio.to_thread(fn)
                return result
            except Exception as e:
                last_error = e
                delay = self._retry_base_delay * (2 ** attempt)
                logger.warning(
                    "API call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, self._max_retries, delay, e,
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]
