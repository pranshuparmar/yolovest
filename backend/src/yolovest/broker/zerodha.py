"""Zerodha Kite Connect broker implementation.

Execution only (free tier, no market data). Supports paper + live modes.
"""

import asyncio
import logging
import time
from typing import Any

from yolovest.broker.base import BrokerBase

logger = logging.getLogger(__name__)


class BrokerCircuitBreaker:
    """Circuit breaker for broker API calls.

    States:
    - CLOSED: normal operation, requests pass through
    - OPEN: too many consecutive failures, all requests fail fast
    - HALF_OPEN: cooldown expired, allow one probe request

    Prevents hammering a failing/rate-limited Kite API, which would
    compound the problem and potentially trigger IP bans.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_sec: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_sec = cooldown_sec
        self._consecutive_failures = 0
        self._opened_at: float = 0.0  # monotonic time when circuit opened
        self._state = "CLOSED"

    @property
    def state(self) -> str:
        if self._state == "OPEN":
            if time.monotonic() - self._opened_at >= self._cooldown_sec:
                self._state = "HALF_OPEN"
        return self._state

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = "CLOSED"

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            if self._state != "OPEN":
                logger.warning(
                    "Broker circuit breaker OPEN after %d consecutive failures "
                    "(cooldown: %.0fs)",
                    self._consecutive_failures, self._cooldown_sec,
                )
            self._state = "OPEN"
            self._opened_at = time.monotonic()

    def check(self) -> None:
        """Raise if circuit is open (requests should fail fast)."""
        state = self.state
        if state == "OPEN":
            remaining = self._cooldown_sec - (time.monotonic() - self._opened_at)
            raise RuntimeError(
                f"Broker circuit breaker is OPEN — API calls blocked for "
                f"{remaining:.0f}s after {self._consecutive_failures} consecutive failures"
            )


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
        db: Any = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._mode = mode
        self._paper_slippage_pct = paper_slippage_pct
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._access_token: str | None = None
        self._kite: Any = None
        self._db = db  # For persisting access token across restarts
        self._authenticated_at: float = 0.0  # monotonic time of last successful auth
        # Kite tokens expire at 6:00 AM IST daily. We cache the auth status
        # and only re-verify via API when the token is expected to be expired.
        # This avoids a kite.profile() call on every heartbeat/page load.
        self._auth_cache_valid_until: float = 0.0
        # Rate limiter: 8 concurrent to stay under Kite's 10 req/s
        self._rate_limiter = asyncio.Semaphore(8)
        # Circuit breaker: trip after 5 consecutive API failures, 30s cooldown
        self._circuit_breaker = BrokerCircuitBreaker(
            failure_threshold=5, cooldown_sec=30.0,
        )
        # Paper mode state
        self._paper_orders: dict[str, dict[str, Any]] = {}
        self._paper_order_counter = 0

    def get_login_url(self) -> str:
        """Get the Kite Connect login URL for daily re-authentication."""
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={self._api_key}"

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self, request_token: str) -> bool:
        """Exchange request_token for access_token (daily re-auth).

        In paper mode, tries real Kite auth first (for holdings/margins),
        falls back to simulated auth if Kite is unavailable.
        """
        try:
            self._kite = await asyncio.to_thread(
                self._create_kite_session, request_token
            )
            self._access_token = self._kite.access_token
            self._update_auth_cache()
            # Persist token for restart recovery
            if self._db:
                try:
                    await self._db.set_system_state("kite_access_token", self._access_token)
                except Exception:
                    logger.debug("Failed to persist kite access token", exc_info=True)
            logger.info("Kite Connect authenticated successfully (valid until ~6:00 AM IST)")
            return True
        except Exception:
            if self._mode == "paper":
                # Paper mode: Kite auth failed (no API keys or no kiteconnect),
                # fall back to simulated auth for order simulation
                self._access_token = "paper_token"
                logger.info("Paper mode: using simulated auth (Kite unavailable)")
                return True
            logger.exception("Kite authentication failed")
            return False

    async def restore_session(self) -> bool:
        """Restore Kite session from persisted access token (after restart)."""
        if not self._db or not self._api_key:
            return False
        try:
            token = await self._db.get_system_state("kite_access_token")
            if not token:
                return False
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=self._api_key)
            kite.set_access_token(token)
            # Verify the token is still valid
            await asyncio.to_thread(kite.profile)
            self._kite = kite
            self._access_token = token
            self._update_auth_cache()
            logger.info("Kite session restored from persisted token (cached until ~6:00 AM IST)")
            return True
        except Exception as e:
            logger.info("Could not restore Kite session (re-login needed): %s", e)
            # Clear stale token
            if self._db:
                try:
                    await self._db.set_system_state("kite_access_token", "")
                except Exception:
                    logger.debug("Failed to clear stale kite token", exc_info=True)
            return False

    def _create_kite_session(self, request_token: str) -> Any:
        """Synchronous Kite session creation (runs in thread)."""
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=self._api_key)
        data = kite.generate_session(request_token, api_secret=self._api_secret)
        kite.set_access_token(data["access_token"])
        return kite

    def _update_auth_cache(self) -> None:
        """Compute when the current token expires.

        Kite tokens expire at 6:00 AM IST daily. We cache the auth
        result and only re-verify via API after this time passes.
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        self._authenticated_at = time.monotonic()

        # Next 6:00 AM IST
        expiry = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if expiry <= now:
            expiry += timedelta(days=1)

        # Convert to monotonic: seconds until expiry
        seconds_until_expiry = (expiry - now).total_seconds()
        self._auth_cache_valid_until = time.monotonic() + seconds_until_expiry
        logger.debug(
            "Auth cache valid for %.0f seconds (until ~6:00 AM IST)",
            seconds_until_expiry,
        )

    async def is_authenticated(self) -> bool:
        """Check if the broker session is valid.

        Uses cached auth status when the token is known to be valid
        (before 6:00 AM IST expiry). Falls back to an API call
        (kite.profile) when the cache has expired or on first check.
        """
        if self._access_token is None:
            return False
        # Paper-only mode (no real broker connection)
        if self._kite is None:
            return self._access_token == "paper_token"
        # Use cached result if token hasn't expired yet
        if time.monotonic() < self._auth_cache_valid_until:
            return True
        # Cache expired or never set — verify via API
        try:
            async with self._rate_limiter:
                await asyncio.to_thread(self._kite.profile)
            self._update_auth_cache()
            return True
        except Exception:
            logger.debug("Kite auth verification failed, invalidating cache", exc_info=True)
            self._auth_cache_valid_until = 0.0  # Invalidate cache
            return False

    # ------------------------------------------------------------------
    # Order Placement
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
        """Simulate order placement in paper mode."""
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
        """Place order via Kite API with retry."""
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

    async def get_holdings(self) -> list[dict[str, Any]]:
        """Get all CNC/delivery holdings from Kite.

        Works in both paper and live mode — paper mode simulates trades
        but your real Zerodha holdings are still visible.
        """
        if self._kite is None:
            return []

        async with self._rate_limiter:
            holdings = await asyncio.to_thread(self._kite.holdings)
        return [dict[str, Any](h) for h in holdings]

    async def get_margins(self) -> dict[str, Any]:
        """Get available margins/funds.

        Uses real Kite API when authenticated (even in paper mode),
        falls back to paper defaults when not authenticated.
        """
        if self._kite is not None:
            try:
                async with self._rate_limiter:
                    margins = await asyncio.to_thread(self._kite.margins)
                return margins
            except Exception:
                logger.debug("Failed to fetch Kite margins, using fallback", exc_info=True)
        # Fallback for unauthenticated or paper-only
        return {"available": {"cash": 0}, "equity": {"available": {"cash": 0}}}

    # ------------------------------------------------------------------
    # Modify SL Order
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
    # Retry Helper
    # ------------------------------------------------------------------

    async def _retry_api_call(self, fn: Any) -> Any:
        """Retry with exponential backoff and circuit breaker protection."""
        # Fail fast if circuit breaker is open
        self._circuit_breaker.check()

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                async with self._rate_limiter:
                    result = await asyncio.to_thread(fn)
                self._circuit_breaker.record_success()
                return result
            except Exception as e:
                last_error = e
                self._circuit_breaker.record_failure()
                # If circuit just opened, don't retry — fail fast
                if self._circuit_breaker.state == "OPEN":
                    logger.error(
                        "API call failed and circuit breaker tripped: %s", e,
                    )
                    break
                delay = self._retry_base_delay * (2 ** attempt)
                logger.warning(
                    "API call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, self._max_retries, delay, e,
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]
