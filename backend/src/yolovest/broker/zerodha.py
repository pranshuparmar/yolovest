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
    - HALF_OPEN: cooldown expired, allow ONE probe request through

    Prevents hammering a failing/rate-limited Kite API, which would
    compound the problem and potentially trigger IP bans.

    No lock is needed: every method here is synchronous (no ``await``), so on
    the single-threaded event loop each runs atomically w.r.t. other
    coroutines. The two cross-call hazards are handled explicitly:
    - exactly one HALF_OPEN probe is admitted at a time (the rest fail fast),
      so callers don't flood the still-fragile API at the cooldown boundary;
    - a stale success from a call that was already in flight when the breaker
      opened cannot reset a deliberately-open breaker.
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
        # True while a single HALF_OPEN probe is in flight; further callers
        # fail fast until it resolves (success → CLOSED, failure → OPEN).
        self._probe_in_flight = False

    def _refresh_state(self) -> None:
        """Flip OPEN → HALF_OPEN once the cooldown has elapsed."""
        if self._state == "OPEN" and (
            time.monotonic() - self._opened_at >= self._cooldown_sec
        ):
            self._state = "HALF_OPEN"

    @property
    def state(self) -> str:
        self._refresh_state()
        return self._state

    def record_success(self) -> None:
        # A success that lands while the breaker is OPEN is stale — it came
        # from a call already in flight when the breaker tripped, so it must
        # NOT reset a deliberately-open breaker. The cooldown + HALF_OPEN
        # probe is the only path back to CLOSED.
        if self._state == "OPEN":
            return
        self._consecutive_failures = 0
        self._state = "CLOSED"
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        # Re-open immediately if a HALF_OPEN probe just failed, or if the
        # consecutive-failure threshold is breached.
        if (
            self._state == "HALF_OPEN"
            or self._consecutive_failures >= self._failure_threshold
        ):
            if self._state != "OPEN":
                logger.warning(
                    "Broker circuit breaker OPEN after %d consecutive failures "
                    "(cooldown: %.0fs)",
                    self._consecutive_failures, self._cooldown_sec,
                )
            self._state = "OPEN"
            self._opened_at = time.monotonic()
        self._probe_in_flight = False

    def check(self) -> None:
        """Raise if requests should fail fast.

        CLOSED passes. OPEN fails fast. Once the cooldown elapses the breaker
        is HALF_OPEN and admits a SINGLE probe (returns normally, marking a
        probe in flight); any further caller fails fast until that probe
        resolves via record_success / record_failure.
        """
        self._refresh_state()
        if self._state == "OPEN":
            remaining = self._cooldown_sec - (time.monotonic() - self._opened_at)
            raise RuntimeError(
                f"Broker circuit breaker is OPEN — API calls blocked for "
                f"{remaining:.0f}s after {self._consecutive_failures} consecutive failures"
            )
        if self._state == "HALF_OPEN":
            if self._probe_in_flight:
                raise RuntimeError(
                    "Broker circuit breaker is HALF_OPEN — a probe request is "
                    "already in flight; failing fast until it resolves"
                )
            self._probe_in_flight = True


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
        kite_data_enabled: bool = False,
        market_data: Any = None,
        rate_limiter: Any = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._mode = mode
        self._paper_slippage_pct = paper_slippage_pct
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._kite_data_enabled = kite_data_enabled
        self._market_data = market_data
        self._access_token: str | None = None
        self._kite: Any = None
        self._db = db  # For persisting access token across restarts
        self._authenticated_at: float = 0.0  # monotonic time of last successful auth
        # Kite tokens expire at 6:00 AM IST daily. We cache the auth status
        # and only re-verify via API when the token is expected to be expired.
        # This avoids a kite.profile() call on every heartbeat/page load.
        self._auth_cache_valid_until: float = 0.0
        # Shared rate limiter for Kite API calls. Accepts an injected
        # instance so it can be shared with KiteDataProvider.
        if rate_limiter is None:
            from yolovest.broker.kite_rate_limiter import KiteRateLimiter
            rate_limiter = KiteRateLimiter(calls_per_second=10.0, concurrency=8)
        self._rate_limiter = rate_limiter
        # Circuit breaker: trip after 5 consecutive API failures, 30s cooldown
        self._circuit_breaker = BrokerCircuitBreaker(
            failure_threshold=5, cooldown_sec=30.0,
        )
        # Per-symbol tick-size cache built from kite.instruments("NSE")
        # on first use. NSE equity tick sizes are not uniform — most are
        # 0.05 but several (price < 250, F&O underlyings) use 0.10, and
        # a handful of penny stocks use 0.01. Sending an order with a
        # price/trigger that isn't a multiple of the symbol's tick size
        # gets rejected by Kite with "Tick size for this script is X.YY".
        # Default to 0.05 on cache miss to match the legacy behaviour
        # for the common case.
        self._tick_size_cache: dict[str, float] = {}
        self._tick_size_cache_warmed: bool = False
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
                    pass
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

    async def logout(self) -> None:
        """Drop the cached access token and clear the persisted one so
        the next is_authenticated() check returns False and the UI
        flips to "Not authenticated".

        Idempotent. Does NOT call any Kite logout endpoint — Kite
        Connect's REST API has no per-token invalidate, tokens expire
        at the next 6:00 AM IST cycle regardless. What we're doing
        here is purely local: forget the token, invalidate the auth
        cache, and wipe the system_state row that restore_session()
        reads from on next boot.
        """
        self._access_token = None
        self._auth_cache_valid_until = 0.0
        self._kite = None
        if self._db is not None:
            try:
                await self._db.set_system_state("kite_access_token", "")
            except Exception:
                logger.debug("Failed to clear persisted Kite token", exc_info=True)
        logger.info("Kite session cleared locally")

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
                    pass
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
        tag: str | None = None,
    ) -> str:
        if self._mode == "paper":
            return self._paper_place_order(
                symbol, side, quantity, order_type, product, price, trigger_price
            )

        return await self._live_place_order(
            symbol, side, quantity, order_type, product, price, trigger_price, tag,
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

    async def _fetch_ltp_for_limit(self, symbol: str) -> float:
        """Fetch LTP for MARKET→LIMIT conversion.

        Tries sources in order:
        1. market_data ingester (TVDatafeed/JugaadData — always available)
        2. kite.ltp() (requires paid data plan)
        3. kite.ohlc() (requires paid data plan)
        Returns 0 if all sources fail.
        """
        # 1. Market data ingester (no paid plan needed)
        if self._market_data:
            try:
                return await self._market_data.get_ltp(symbol)
            except Exception as e:
                logger.debug("market_data.get_ltp failed for %s: %s", symbol, e)

        nse_key = f"NSE:{symbol}"

        # 2. kite.ltp() (paid plan)
        if self._kite_data_enabled and self._kite:
            try:
                async with self._rate_limiter:
                    data = await asyncio.to_thread(self._kite.ltp, nse_key)
                ltp = data.get(nse_key, {}).get("last_price", 0)
                if ltp and ltp > 0:
                    return float(ltp)
            except Exception as e:
                logger.debug("kite.ltp failed for %s: %s", symbol, e)

        # 3. kite.ohlc() (paid plan)
        if self._kite:
            try:
                async with self._rate_limiter:
                    data = await asyncio.to_thread(self._kite.ohlc, nse_key)
                quote = data.get(nse_key, {})
                ltp = quote.get("last_price") or quote.get("ohlc", {}).get("close", 0)
                if ltp and ltp > 0:
                    return float(ltp)
            except Exception as e:
                logger.debug("kite.ohlc failed for %s: %s", symbol, e)

        logger.warning("All LTP sources failed for %s MARKET→LIMIT conversion", symbol)
        return 0.0

    @staticmethod
    def _tick_round(price: float, tick: float = 0.05) -> float:
        """Snap a price to a tick grid. Caller is responsible for
        passing the right tick; defaults to 0.05 (the most common NSE
        equity tick) when the per-symbol tick is unknown.
        """
        if tick <= 0:
            tick = 0.05
        return round(round(price / tick) * tick, 2)

    async def _ensure_tick_size_cache(self) -> None:
        """Lazy-warm the per-symbol tick-size cache from
        kite.instruments("NSE"). Returns once the cache is populated;
        safe to call repeatedly — subsequent calls are no-ops.

        The instrument master is ~5MB and only downloaded once per
        broker lifetime. On failure (no auth, transient), cache stays
        empty and callers fall back to the default 0.05 tick. Better to
        place an order with a wrong tick that Kite rejects with a clear
        message than to silently block trading on a transient API hiccup.
        """
        if self._tick_size_cache_warmed or self._kite is None:
            return
        try:
            async with self._rate_limiter:
                instruments = await asyncio.to_thread(self._kite.instruments, "NSE")
            for inst in instruments:
                sym = inst.get("tradingsymbol")
                tick = inst.get("tick_size")
                if sym and tick:
                    self._tick_size_cache[sym] = float(tick)
            self._tick_size_cache_warmed = True
            distinct = sorted({round(v, 2) for v in self._tick_size_cache.values()})
            logger.info(
                "Tick-size cache warmed: %d symbols, distinct ticks=%s",
                len(self._tick_size_cache), distinct,
            )
        except Exception:
            logger.warning(
                "Failed to warm tick-size cache; falling back to 0.05 default",
                exc_info=True,
            )

    def _tick_for(self, symbol: str) -> float:
        """Return the cached tick size for `symbol`, or 0.05 on miss."""
        return self._tick_size_cache.get(symbol, 0.05)

    def tick_for(self, symbol: str) -> float:
        """Public wrapper around the warmed per-symbol tick cache.
        Concrete override of BrokerBase.tick_for. Falls back to 0.05
        when the cache hasn't been warmed yet (e.g. before the first
        order placement or while the broker isn't authenticated)."""
        return self._tick_for(symbol)

    def round_to_tick(self, symbol: str, price: float) -> float:
        """Snap price to the per-symbol tick grid using the warmed
        cache. Override of BrokerBase.round_to_tick so signal-time
        target / SL match the grid that _live_place_order will enforce
        at order placement — no more 34.43 targets on 0.05-tick stocks
        that get silently rounded to 34.45 when the order goes out."""
        return self._tick_round_for(symbol, price)

    def _tick_round_for(self, symbol: str, price: float) -> float:
        """Snap `price` to the symbol's tick grid using the warmed cache."""
        return self._tick_round(price, self._tick_for(symbol))

    async def _live_place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        product: str,
        price: float | None,
        trigger_price: float | None,
        tag: str | None = None,
    ) -> str:
        """Place order via Kite API with retry.

        Kite rejects MARKET and SL-M orders that don't carry a
        `market_protection` value. We always convert MARKET → LIMIT (at
        LTP ± buffer) and SL-M → SL (at trigger ± buffer) — those carry
        explicit prices and need no protection. If LTP fetch fails for
        a MARKET order we ABORT rather than fall back to a raw MARKET
        with exchange-defined protection — on illiquid names the
        exchange band can be 3-5% wide and we'd rather miss the trade
        than eat that slippage blind. SL-M with no trigger is a
        different shape and still falls through to market_protection=-1
        below, because that path is only reachable from manual/legacy
        callers that build orders without a trigger.
        """
        if self._kite is None:
            raise RuntimeError("Not authenticated")

        # Warm the per-symbol tick-size cache once. After this returns,
        # _tick_round_for uses the symbol's real tick instead of the
        # 0.05 default.
        await self._ensure_tick_size_cache()

        # Convert MARKET → LIMIT at LTP ± buffer (Zerodha API restriction).
        # Try sources in order: market_data (ingester), kite.ltp (paid), kite.ohlc (paid).
        if order_type == "MARKET":
            ltp = await self._fetch_ltp_for_limit(symbol)
            if ltp and ltp > 0:
                buffer = 0.005 if self._kite_data_enabled else 0.01
                if side == "BUY":
                    raw = ltp * (1 + buffer)
                    price = self._tick_round_for(symbol, raw)
                else:
                    raw = ltp * (1 - buffer)
                    price = self._tick_round_for(symbol, raw)
                order_type = "LIMIT"
                logger.info(
                    "MARKET→LIMIT conversion: %s %s LTP=%.2f → price=%.2f",
                    side, symbol, ltp, price,
                )
            else:
                # No LTP — the previous behaviour was to fall back to a
                # raw MARKET order with market_protection=-1, leaving
                # slippage entirely to the exchange band (typically 3-5%
                # on thinly traded names). On a name we've already
                # failed to fetch LTP for, that band is exactly where
                # things are most likely to be ugly. Refuse the trade;
                # the heartbeat retry path will get another go once
                # data is healthy.
                raise RuntimeError(
                    f"MARKET→LIMIT conversion failed for {symbol}: no LTP "
                    "available from any data source. Refusing to submit a "
                    "raw MARKET order — slippage protection would be left "
                    "to the exchange band."
                )

        # Convert SL-M → SL (Zerodha disabled SL-M for retail API; it errors
        # with "Market orders without market protection are not allowed").
        # SL is a stop-loss with a limit price: the order rests at the
        # exchange and converts to a LIMIT order when trigger_price is hit.
        # We set the limit slightly past the trigger so the order is highly
        # likely to fill once triggered.
        if order_type == "SL-M" and trigger_price is not None:
            buffer = 0.005  # 0.5% past trigger for fill probability
            if side == "SELL":
                # SL on a long position — trigger fires when price drops; we
                # want to sell on the way down, so limit price BELOW trigger.
                price = trigger_price * (1 - buffer)
            else:
                # SL on a short position — trigger fires on the way up.
                price = trigger_price * (1 + buffer)
            order_type = "SL"
            logger.info(
                "SL-M→SL conversion: %s %s trigger=%.2f → limit=%.2f",
                side, symbol, trigger_price, price,
            )

        # Final tick alignment — Kite rejects any price/trigger that isn't a
        # multiple of the instrument's tick size. NSE equity is 0.05 by
        # default; values computed from ATR, percentages, or model outputs
        # rarely land on the tick grid.
        if price is not None:
            price = self._tick_round_for(symbol, price)
        if trigger_price is not None:
            trigger_price = self._tick_round_for(symbol, trigger_price)

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
        # Any residual MARKET or SL-M must carry market_protection or Kite
        # rejects the order. -1 instructs Zerodha to apply the exchange's
        # own protection band (typically ~3% on equity cash).
        if order_type in ("MARKET", "SL-M"):
            params["market_protection"] = -1
        # Tag flows back through orders() and postbacks so we can tell
        # which skill / code path placed any given order. Kite enforces
        # ≤20 chars; we truncate defensively.
        if tag:
            params["tag"] = tag[:20]

        # Order creation is non-idempotent: Kite can error after the
        # exchange accepted the order, so a blind retry would place a
        # duplicate. Single attempt; the skill layer reconciles on failure.
        return str(await self._retry_api_call(
            lambda: self._kite.place_order(variety="regular", **params),
            idempotent=False,
        ))

    # ------------------------------------------------------------------
    # Order Management
    # ------------------------------------------------------------------

    # Order statuses where cancellation is a no-op — the order is
    # already in a final state at the broker. Kite returns either
    # "Order cannot be cancelled as it is being processed" (transition
    # state) or a hard error from these; we treat them as
    # already-cancelled rather than logging a traceback.
    _TERMINAL_ORDER_STATUSES = {
        "CANCELLED", "COMPLETE", "REJECTED", "AMO REQ RECEIVED",
    }

    async def cancel_order(self, order_id: str) -> bool:
        if self._mode == "paper":
            if order_id in self._paper_orders:
                self._paper_orders[order_id]["status"] = "cancelled"
                return True
            return False

        # Pre-check status — when the user (or a parallel skill) has
        # already cancelled this order, Kite responds with
        # "Order cannot be cancelled as it is being processed"
        # which is just transition-state noise. Skip the call if
        # the order is already terminal.
        try:
            status_info = await self.get_order_status(order_id)
            status = (status_info.get("status") or "").upper()
            if status in self._TERMINAL_ORDER_STATUSES:
                logger.debug(
                    "cancel_order %s: already in terminal state %s, skipping",
                    order_id, status,
                )
                return True
        except Exception:
            # Best-effort — if we can't read status, fall through to
            # the cancel attempt and let it surface any real error.
            logger.debug(
                "cancel_order %s: status pre-check failed, attempting cancel anyway",
                order_id, exc_info=True,
            )

        try:
            async with self._rate_limiter:
                await asyncio.to_thread(
                    self._kite.cancel_order, variety="regular", order_id=order_id
                )
            return True
        except Exception as exc:
            # Common race: a parallel actor (Kite web UI, broker
            # auto-square-off, another heartbeat) cancelled or
            # completed the order while we were preparing. Kite
            # returns "Order cannot be cancelled as it is being
            # processed" — that's terminal-state ambiguity, not a
            # real failure. Demote to INFO so logs stay quiet.
            msg = str(exc).lower()
            if (
                "being processed" in msg
                or "already" in msg
                or "cannot be cancelled" in msg
            ):
                logger.info(
                    "cancel_order %s: broker reports order already settling "
                    "(%s) — treating as cancelled",
                    order_id, exc,
                )
                return True
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
                # Don't fail silently — a zeroed cash figure the operator
                # can't explain is worse than the transient error itself.
                # (Display-only: risk sizing uses the ledger, not this.)
                logger.warning(
                    "get_margins: Kite margins fetch failed; returning cash=0 "
                    "fallback", exc_info=True,
                )
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

    async def modify_order(
        self,
        order_id: str,
        *,
        price: float | None = None,
        quantity: int | None = None,
        trigger_price: float | None = None,
        order_type: str | None = None,
    ) -> bool:
        """Generic order modification — adjust price / qty / trigger /
        order_type on a still-open broker order.

        Used by the dashboard's order-book "Modify" action so the user
        can move a queued LIMIT price or resize an SL trigger without
        going to Kite. Tick rounding applies the same way as the
        original place_order. None-valued fields are passed through
        unchanged.
        """
        if self._mode == "paper":
            logger.info(
                "[PAPER] Modify order %s price=%s qty=%s trigger=%s type=%s",
                order_id, price, quantity, trigger_price, order_type,
            )
            if order_id in self._paper_orders:
                po = self._paper_orders[order_id]
                if price is not None:
                    po["price"] = price
                if quantity is not None:
                    po["quantity"] = int(quantity)
                if trigger_price is not None:
                    po["trigger_price"] = trigger_price
                if order_type is not None:
                    po["order_type"] = order_type
            return True

        if not self._kite:
            raise RuntimeError("Not authenticated")

        # Round to symbol tick (same path as place_order). `sym` is bound
        # up front so the trigger-rounding branch below can't raise
        # UnboundLocalError when the kite.orders() lookup throws.
        sym: str | None = None
        if price is not None:
            try:
                # Best-effort symbol resolution from kite.orders(); skip
                # cache warm if it fails — broker still rejects on
                # bad tick which surfaces as an error to the caller.
                async with self._rate_limiter:
                    orders = await asyncio.to_thread(self._kite.orders)
                sym = next(
                    (o.get("tradingsymbol") for o in orders if o.get("order_id") == order_id),
                    None,
                )
                if sym:
                    price = self._tick_round_for(sym, float(price))
            except Exception:
                logger.debug(
                    "modify_order: tick-rounding lookup failed for %s",
                    order_id, exc_info=True,
                )
        if trigger_price is not None and price is not None:
            # Mirror place_order's logic — trigger uses same tick as price
            trigger_price = self._tick_round_for(sym, float(trigger_price)) if sym else trigger_price

        kwargs: dict[str, Any] = {"variety": "regular", "order_id": order_id}
        if price is not None:
            kwargs["price"] = price
        if quantity is not None:
            kwargs["quantity"] = int(quantity)
        if trigger_price is not None:
            kwargs["trigger_price"] = trigger_price
        if order_type is not None:
            kwargs["order_type"] = order_type

        def _modify() -> None:
            self._kite.modify_order(**kwargs)

        await self._retry_api_call(_modify)
        return True

    async def get_orders(self) -> list[dict[str, Any]]:
        """Return every order from today (open / executed / cancelled /
        rejected / trigger-pending). Mirrors Kite's order book.
        """
        if self._mode == "paper":
            return list(self._paper_orders.values())
        async with self._rate_limiter:
            orders = await asyncio.to_thread(self._kite.orders)
        return list(orders)

    async def initiate_holdings_auth(
        self, holdings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Kick off the CDSL TPIN authorisation flow for a list of
        holdings the user is about to sell.

        Returns a dict from kiteconnect (typically
        `{"request_id": ..., "redirect_url": ...}`) that the UI can
        open in a new tab so the user can complete CDSL TPIN entry.
        Returns None when the kiteconnect client doesn't expose this
        method (older library version) or when called in paper mode.

        `holdings` shape (per Kite docs):
            [{"isin": "INE...", "quantity": 5}, ...]
        When omitted, defaults to authorising EVERY current holding
        (Kite accepts the empty/None call to mean "all holdings").
        """
        if self._mode == "paper":
            return None
        if not self._kite or not hasattr(self._kite, "initiate_holdings_auth"):
            return None

        def _call() -> Any:
            kwargs: dict[str, Any] = {}
            if holdings:
                kwargs["holdings"] = holdings
            return self._kite.initiate_holdings_auth(**kwargs)

        try:
            async with self._rate_limiter:
                result = await asyncio.to_thread(_call)
            return result if isinstance(result, dict) else None
        except Exception:
            logger.warning(
                "initiate_holdings_auth failed — falling back to static URL",
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # GTT (Good Till Triggered) orders
    # ------------------------------------------------------------------
    #
    # GTT orders sit at the broker until a trigger price is hit, then
    # place a real order. The two-leg "OCO" variant places a stoploss
    # leg AND a target leg simultaneously; firing one cancels the other.
    # Only CNC (delivery) is supported by Zerodha — MIS positions can't
    # use GTT and continue to rely on client-side detection.

    async def place_oco_gtt(
        self,
        symbol: str,
        side: str,
        quantity: int,
        stoploss_trigger: float,
        stoploss_limit: float,
        target_trigger: float,
        target_limit: float,
        last_price: float,
    ) -> int:
        """Place a two-leg OCO GTT for an existing position.

        `side` is the EXIT side — "SELL" closes a long, "BUY" closes a short.

        Returns the GTT trigger_id assigned by the broker (use this with
        delete_gtt / modify_gtt).
        """
        if self._mode == "paper":
            logger.info(
                "[PAPER] place_oco_gtt %s %s qty=%d sl=%.2f→%.2f target=%.2f→%.2f",
                side, symbol, quantity, stoploss_trigger, stoploss_limit,
                target_trigger, target_limit,
            )
            return 0
        if self._kite is None:
            raise RuntimeError("Not authenticated")

        await self._ensure_tick_size_cache()
        kite_side = "BUY" if side == "BUY" else "SELL"
        st_trig = self._tick_round_for(symbol, stoploss_trigger)
        st_lim = self._tick_round_for(symbol, stoploss_limit)
        tg_trig = self._tick_round_for(symbol, target_trigger)
        tg_lim = self._tick_round_for(symbol, target_limit)

        legs = [
            {
                "transaction_type": kite_side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "price": st_lim,
                "product": "CNC",
            },
            {
                "transaction_type": kite_side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "price": tg_lim,
                "product": "CNC",
            },
        ]

        def _place() -> dict[str, Any]:
            return self._kite.place_gtt(
                trigger_type=self._kite.GTT_TYPE_OCO,
                tradingsymbol=symbol,
                exchange="NSE",
                trigger_values=[st_trig, tg_trig],
                last_price=float(self._tick_round_for(symbol, last_price)),
                orders=legs,
            )

        # GTT creation is non-idempotent — a retry would leave a duplicate
        # resting OCO trigger. Single attempt; caller handles attach failure.
        result = await self._retry_api_call(_place, idempotent=False)
        trigger_id = int(result.get("trigger_id") or 0)
        logger.info(
            "GTT placed: %s %s qty=%d trigger_id=%d (sl_trig=%.2f sl_lim=%.2f "
            "tgt_trig=%.2f tgt_lim=%.2f)",
            kite_side, symbol, quantity, trigger_id,
            st_trig, st_lim, tg_trig, tg_lim,
        )
        return trigger_id

    async def modify_gtt(
        self,
        gtt_id: int,
        symbol: str,
        side: str,
        quantity: int,
        stoploss_trigger: float,
        stoploss_limit: float,
        target_trigger: float,
        target_limit: float,
        last_price: float,
    ) -> bool:
        """Modify an existing two-leg OCO GTT. Kite's modify_gtt requires
        re-supplying BOTH legs in full (you can't update only one side),
        so the signature mirrors place_oco_gtt with the trigger_id added.

        Returns True on success; raises on hard failure. Paper mode is
        a no-op.
        """
        if self._mode == "paper":
            logger.info(
                "[PAPER] modify_gtt %d %s qty=%d sl=%.2f→%.2f target=%.2f→%.2f",
                gtt_id, symbol, quantity, stoploss_trigger, stoploss_limit,
                target_trigger, target_limit,
            )
            return True
        if self._kite is None:
            raise RuntimeError("Not authenticated")

        await self._ensure_tick_size_cache()
        kite_side = "BUY" if side == "BUY" else "SELL"
        st_trig = self._tick_round_for(symbol, stoploss_trigger)
        st_lim = self._tick_round_for(symbol, stoploss_limit)
        tg_trig = self._tick_round_for(symbol, target_trigger)
        tg_lim = self._tick_round_for(symbol, target_limit)

        legs = [
            {
                "transaction_type": kite_side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "price": st_lim,
                "product": "CNC",
            },
            {
                "transaction_type": kite_side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "price": tg_lim,
                "product": "CNC",
            },
        ]

        def _modify() -> dict[str, Any]:
            return self._kite.modify_gtt(
                trigger_id=int(gtt_id),
                trigger_type=self._kite.GTT_TYPE_OCO,
                tradingsymbol=symbol,
                exchange="NSE",
                trigger_values=[st_trig, tg_trig],
                last_price=float(self._tick_round_for(symbol, last_price)),
                orders=legs,
            )

        await self._retry_api_call(_modify)
        logger.info(
            "GTT modified: %d %s qty=%d sl_trig=%.2f sl_lim=%.2f "
            "tgt_trig=%.2f tgt_lim=%.2f",
            gtt_id, symbol, quantity, st_trig, st_lim, tg_trig, tg_lim,
        )
        return True

    async def delete_gtt(self, gtt_id: int) -> bool:
        """Delete a GTT by trigger_id. Idempotent — already-deleted /
        already-fired GTTs return True silently."""
        if self._mode == "paper":
            logger.info("[PAPER] delete_gtt %s", gtt_id)
            return True
        if self._kite is None or not gtt_id:
            return False

        try:
            async with self._rate_limiter:
                await asyncio.to_thread(self._kite.delete_gtt, trigger_id=gtt_id)
            return True
        except Exception as e:
            msg = str(e).lower()
            if "not found" in msg or "already" in msg:
                return True
            logger.warning("delete_gtt failed for %s: %s", gtt_id, e)
            return False

    async def get_gtts(self) -> list[dict[str, Any]]:
        """List active GTTs at the broker."""
        if self._mode == "paper" or self._kite is None:
            return []
        try:
            async with self._rate_limiter:
                return list(await asyncio.to_thread(self._kite.get_gtts))
        except Exception as e:
            logger.debug("get_gtts failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Executed trades (for ghost-position recovery)
    # ------------------------------------------------------------------

    async def convert_position(
        self,
        symbol: str,
        quantity: int,
        from_product: str,
        to_product: str,
        side: str = "BUY",
    ) -> bool:
        """Convert open position via kite.convert_position.
        Paper mode is a no-op returning True so test paths still flow.
        """
        if self._mode == "paper":
            logger.info(
                "[PAPER] convert_position %s qty=%d %s -> %s",
                symbol, quantity, from_product, to_product,
            )
            return True
        if self._kite is None:
            raise RuntimeError("Not authenticated")

        kite_side = "BUY" if side.upper() == "BUY" else "SELL"

        def _convert() -> Any:
            return self._kite.convert_position(
                tradingsymbol=symbol,
                exchange="NSE",
                transaction_type=kite_side,
                position_type="day",
                quantity=int(quantity),
                old_product=from_product,
                new_product=to_product,
            )

        try:
            await self._retry_api_call(_convert)
            logger.info(
                "convert_position: %s qty=%d %s -> %s OK",
                symbol, quantity, from_product, to_product,
            )
            return True
        except Exception as e:
            logger.warning(
                "convert_position failed for %s (%s -> %s): %s",
                symbol, from_product, to_product, e,
            )
            return False

    async def estimate_margin(
        self, legs: list[dict[str, Any]],
    ) -> dict[str, float] | None:
        """Pre-trade margin via kite.order_margins. Falls back to None
        when paper/offline so the caller uses the naive notional check.
        """
        if self._mode == "paper" or self._kite is None or not legs:
            return None
        try:
            async with self._rate_limiter:
                resp = await asyncio.to_thread(self._kite.order_margins, legs)
        except Exception as e:
            logger.debug("kite.order_margins failed: %s", e)
            return None
        if not isinstance(resp, list) or not resp:
            return None
        total = 0.0
        for leg in resp:
            try:
                total += float(leg.get("total") or 0.0)
            except (TypeError, ValueError):
                continue
        # Return the broker's per-leg list under "legs" plus the rolled-up
        # `total` so callers can choose granularity.
        return {"total": round(total, 2), "legs": resp}  # type: ignore[dict-item]

    async def get_order_history(self, order_id: str) -> list[dict[str, Any]]:
        """State-transition timeline for a single order via kite.order_history."""
        if self._mode == "paper" or self._kite is None or not order_id:
            return []
        try:
            async with self._rate_limiter:
                rows = await asyncio.to_thread(
                    self._kite.order_history, order_id,
                )
            return list(rows or [])
        except Exception as e:
            logger.debug("kite.order_history(%s) failed: %s", order_id, e)
            return []

    async def get_order_trades(self, order_id: str) -> list[dict[str, Any]]:
        """Per-fill records for a single order via kite.order_trades."""
        if self._mode == "paper" or self._kite is None or not order_id:
            return []
        try:
            async with self._rate_limiter:
                rows = await asyncio.to_thread(
                    self._kite.order_trades, order_id,
                )
            return list(rows or [])
        except Exception as e:
            logger.debug("kite.order_trades(%s) failed: %s", order_id, e)
            return []

    async def get_executed_trades(self) -> list[dict[str, Any]]:
        """Today's executed trades from Kite. Empty in paper or when offline."""
        if self._mode == "paper" or self._kite is None:
            return []
        try:
            async with self._rate_limiter:
                trades = await asyncio.to_thread(self._kite.trades)
            return list(trades or [])
        except Exception as e:
            logger.debug("kite.trades failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Charges (virtual contract note)
    # ------------------------------------------------------------------

    async def compute_charges(
        self, legs: list[dict[str, Any]]
    ) -> list[dict[str, float]] | None:
        """Fetch actual charges per leg from Kite's /charges/orders endpoint.

        Returns None in paper mode, when not authenticated, or if the SDK
        rejects the request — caller then falls back to the config estimate.
        """
        if self._mode == "paper" or self._kite is None or not legs:
            return None
        try:
            async with self._rate_limiter:
                resp = await asyncio.to_thread(
                    self._kite.get_virtual_contract_note, legs,
                )
        except Exception as e:
            logger.debug("get_virtual_contract_note failed: %s", e)
            return None

        if not isinstance(resp, list) or len(resp) != len(legs):
            return None

        out: list[dict[str, float]] = []
        for entry in resp:
            charges = (entry or {}).get("charges") or {}
            brokerage = float(charges.get("brokerage") or 0.0)
            stt = float(charges.get("transaction_tax") or 0.0)
            gst_total = float((charges.get("gst") or {}).get("total") or 0.0)
            other = (
                float(charges.get("exchange_turnover_charge") or 0.0)
                + float(charges.get("sebi_turnover_charge") or 0.0)
                + float(charges.get("stamp_duty") or 0.0)
                + gst_total
            )
            total = float(charges.get("total") or (brokerage + stt + other))
            out.append({
                "brokerage": round(brokerage, 2),
                "stt": round(stt, 2),
                "other_charges": round(other, 2),
                "total": round(total, 2),
            })
        return out

    # ------------------------------------------------------------------
    # Retry Helper
    # ------------------------------------------------------------------

    async def _retry_api_call(self, fn: Any, *, idempotent: bool = True) -> Any:
        """Retry with exponential backoff and circuit breaker protection.

        ``idempotent`` defaults True — safe for reads and absolute-state
        modifies (set SL trigger to X, modify a GTT) where re-running the
        call converges to the same broker state. Pass ``idempotent=False``
        for order/GTT *creation* calls: Kite is known to return an error
        AFTER the exchange has already accepted the order, so a blind retry
        places a duplicate. For those we make a single attempt and let the
        caller — which has order-reconciliation logic
        (`_find_recently_placed_order`) — decide whether the order landed.
        """
        # Fail fast if circuit breaker is open
        self._circuit_breaker.check()

        last_error: Exception | None = None
        attempts = self._max_retries if idempotent else 1
        for attempt in range(attempts):
            try:
                async with self._rate_limiter:
                    result = await asyncio.to_thread(fn)
                self._circuit_breaker.record_success()
                return result
            except Exception as e:
                last_error = e
                self._circuit_breaker.record_failure()
                # Never auto-retry a non-idempotent mutation — a duplicate
                # live order is worse than surfacing the error to the caller.
                if not idempotent:
                    break
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
