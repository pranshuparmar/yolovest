"""KiteTicker WebSocket wrapper.

Bridges KiteTicker's sync, Twisted-threaded callbacks into the FastAPI
asyncio loop. Today we use it to cache real-time LTP per symbol so
position-monitor's target/SL check fires sub-second instead of waiting
for the 15-min heartbeat. Order updates flow in too — they're handed to
an optional callback so the same `_apply_order_postback` business logic
can be reused without changes.

KiteTicker has its own reconnect loop (50 tries × exponential backoff),
so this wrapper is intentionally thin: start it once at boot, subscribe
when positions change, read the cached price when you need it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class KiteTickerClient:
    """Asyncio-friendly facade over `kiteconnect.KiteTicker`.

    Lifecycle:
        client = KiteTickerClient(api_key, access_token, kite_data_provider)
        await client.start()                 # spawns the Twisted thread
        await client.subscribe(["RELIANCE"]) # token-by-token
        price = client.get_ltp("RELIANCE")   # returns float or None
        await client.stop()                  # graceful shutdown

    Threading model: KiteTicker runs in `threaded=True` mode, so its
    callbacks (`on_ticks`, `on_order_update`, …) fire on a Twisted
    reactor thread. Each callback marshals back to the asyncio loop
    via `loop.call_soon_threadsafe`. Never await anything in a
    callback — Twisted thread can't await asyncio coroutines.
    """

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        kite_data_provider: Any,
        order_update_callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
        tick_broadcast_callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
        tick_broadcast_throttle_sec: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        # Used to resolve symbol → instrument_token (pre-warmed cache)
        self._data_provider = kite_data_provider
        self._order_update_cb = order_update_callback
        # Optional fan-out to dashboard WebSocket. Throttled to one
        # broadcast per symbol per `tick_broadcast_throttle_sec` so
        # we don't saturate browser sockets when 20 symbols are each
        # ticking multiple times per second.
        self._tick_broadcast_cb = tick_broadcast_callback
        self._tick_throttle = tick_broadcast_throttle_sec
        self._last_tick_broadcast: dict[str, float] = {}
        self._ticker: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # symbol → (last_price, monotonic_timestamp)
        self._ltp_cache: dict[str, tuple[float, float]] = {}
        # symbol → instrument_token; cached so unsubscribe doesn't re-lookup
        self._symbol_to_token: dict[str, int] = {}
        self._token_to_symbol: dict[int, str] = {}
        self._subscribed_tokens: set[int] = set()
        self._connected: bool = False
        self._lock = asyncio.Lock()
        # Flips True when we detect the WebSocket upgrade was rejected
        # with HTTP 403 — meaning the access_token the ticker was
        # constructed with is no longer accepted. KiteTicker's built-in
        # 50-attempt reconnect loop will otherwise hammer the server
        # forever; once this flag is set we tear the ticker down and
        # leave it down until the broker re-auths and explicitly
        # restarts the ticker.
        self._auth_failed: bool = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """Spawn the Twisted reactor thread and connect."""
        if self._ticker is not None:
            return
        # Fresh start clears the previous auth-failure latch — caller
        # is responsible for having supplied a valid access_token
        # before calling start() again (the broker re-auth path
        # constructs a new KiteTickerClient with the fresh token).
        self._auth_failed = False
        try:
            from kiteconnect import KiteTicker
        except ImportError as e:  # pragma: no cover
            logger.warning("KiteTicker import failed: %s", e)
            return

        self._loop = asyncio.get_running_loop()
        self._ticker = KiteTicker(self._api_key, self._access_token)
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_reconnect = self._on_reconnect
        if self._order_update_cb is not None:
            self._ticker.on_order_update = self._on_order_update

        # threaded=True runs the Twisted reactor in a background thread
        # so FastAPI's asyncio loop is untouched.
        await asyncio.to_thread(self._ticker.connect, threaded=True)
        logger.info("KiteTicker: started (threaded mode)")

    async def stop(self) -> None:
        if self._ticker is None:
            return
        try:
            await asyncio.to_thread(self._ticker.close)
        except Exception:
            logger.debug("KiteTicker close failed", exc_info=True)
        self._ticker = None
        self._connected = False
        logger.info("KiteTicker: stopped")

    async def subscribe(self, symbols: list[str]) -> None:
        """Resolve symbols to instrument tokens and subscribe in LTP mode.

        Token resolution goes through the data provider's pre-warmed
        instrument cache — same source used by historical fetches — so
        no extra round-trips. Tokens already subscribed are skipped.
        """
        if not symbols or self._ticker is None:
            return
        new_tokens: list[int] = []
        async with self._lock:
            for sym in symbols:
                token = self._symbol_to_token.get(sym)
                if token is None:
                    token = await self._resolve_token(sym)
                    if token is None:
                        continue
                    self._symbol_to_token[sym] = token
                    self._token_to_symbol[token] = sym
                if token not in self._subscribed_tokens:
                    self._subscribed_tokens.add(token)
                    new_tokens.append(token)
        if new_tokens:
            try:
                await asyncio.to_thread(self._ticker.subscribe, new_tokens)
                await asyncio.to_thread(
                    self._ticker.set_mode, self._ticker.MODE_LTP, new_tokens,
                )
                logger.info(
                    "KiteTicker: subscribed %d new tokens (%d total active)",
                    len(new_tokens), len(self._subscribed_tokens),
                )
            except Exception:
                logger.exception("KiteTicker: subscribe failed")

    async def unsubscribe(self, symbols: list[str]) -> None:
        if not symbols or self._ticker is None:
            return
        tokens: list[int] = []
        async with self._lock:
            for sym in symbols:
                t = self._symbol_to_token.get(sym)
                if t and t in self._subscribed_tokens:
                    tokens.append(t)
                    self._subscribed_tokens.discard(t)
        if tokens:
            try:
                await asyncio.to_thread(self._ticker.unsubscribe, tokens)
                logger.info("KiteTicker: unsubscribed %d tokens", len(tokens))
            except Exception:
                logger.exception("KiteTicker: unsubscribe failed")

    def get_ltp(self, symbol: str, max_age_sec: float = 5.0) -> float | None:
        """Return the most recent cached LTP for `symbol`, or None when
        nothing is cached or the data is stale. Synchronous — safe to
        call from anywhere.
        """
        entry = self._ltp_cache.get(symbol)
        if entry is None:
            return None
        price, ts = entry
        if time.monotonic() - ts > max_age_sec:
            return None
        return price

    async def _resolve_token(self, symbol: str) -> int | None:
        """Use the KiteDataProvider's pre-warmed instrument cache."""
        try:
            return await self._data_provider.get_instrument_token(symbol)
        except Exception:
            logger.debug("KiteTicker: token lookup failed for %s", symbol, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Sync callbacks — run on Twisted thread. Marshal everything to the
    # asyncio loop via call_soon_threadsafe.
    # ------------------------------------------------------------------

    def _on_ticks(self, _ws: Any, ticks: list[dict[str, Any]]) -> None:
        # Lightweight: update the cache, then fan out a throttled
        # `tick_update` event to dashboard clients. Both happen on the
        # Twisted thread; cache write is fine here (atomic dict ops),
        # broadcast is marshalled to the asyncio loop.
        now = time.monotonic()
        for tick in ticks or []:
            tok = tick.get("instrument_token")
            price = tick.get("last_price")
            if tok is None or price is None:
                continue
            sym = self._token_to_symbol.get(int(tok))
            if not sym:
                continue
            self._ltp_cache[sym] = (float(price), now)

            if self._tick_broadcast_cb is None or self._loop is None:
                continue
            # Throttle: at most one broadcast per symbol per N seconds.
            last = self._last_tick_broadcast.get(sym, 0.0)
            if now - last < self._tick_throttle:
                continue
            self._last_tick_broadcast[sym] = now
            try:
                asyncio.run_coroutine_threadsafe(
                    self._tick_broadcast_cb({
                        "symbol": sym, "ltp": float(price),
                    }),
                    self._loop,
                )
            except Exception:
                logger.debug("tick broadcast bridge failed", exc_info=True)

    def _on_connect(self, _ws: Any, _resp: Any) -> None:
        self._connected = True
        logger.info("KiteTicker: connected")
        # Re-subscribe on reconnect — KiteTicker keeps the token list
        # internally across reconnects, but resync our mode just in case.
        if self._subscribed_tokens and self._ticker is not None:
            try:
                tokens = list(self._subscribed_tokens)
                self._ticker.subscribe(tokens)
                self._ticker.set_mode(self._ticker.MODE_LTP, tokens)
            except Exception:
                logger.exception("KiteTicker: resubscribe on connect failed")

    def _on_close(self, _ws: Any, code: int, reason: str) -> None:
        self._connected = False
        logger.warning("KiteTicker: closed code=%d reason=%s", code, reason)
        self._maybe_handle_auth_failure(reason)

    def _on_error(self, _ws: Any, code: int, reason: str) -> None:
        logger.warning("KiteTicker: error code=%d reason=%s", code, reason)
        self._maybe_handle_auth_failure(reason)

    def _on_reconnect(self, _ws: Any, attempts: int) -> None:
        # The kiteconnect ticker's reconnect runs on its Twisted thread
        # so the auth-failure tear-down (which marshals to the asyncio
        # loop via run_coroutine_threadsafe) may race with this. If the
        # flag is set, just log and let the pending shutdown complete.
        if self._auth_failed:
            logger.debug(
                "KiteTicker: reconnect attempt %d ignored — auth failed",
                attempts,
            )
            return
        logger.info("KiteTicker: reconnect attempt %d", attempts)

    def _maybe_handle_auth_failure(self, reason: str) -> None:
        """When the WebSocket upgrade was rejected with HTTP 403, the
        access_token this ticker holds is dead. KiteTicker's internal
        reconnect loop will otherwise retry forever (we've seen 40+
        attempts in production); detect the case once and schedule a
        clean shutdown on the asyncio loop. The broker re-auth path
        is the only thing that brings the ticker back up.

        Runs on the Twisted reactor thread, so any actual shutdown
        has to be marshalled back to the asyncio loop.
        """
        if self._auth_failed:
            return  # already handled
        marker = reason.lower() if reason else ""
        if "403" not in marker and "forbidden" not in marker:
            return
        self._auth_failed = True
        logger.error(
            "KiteTicker: WebSocket upgrade rejected (%s) — access_token "
            "is dead, stopping reconnect loop. Ticker stays down until "
            "broker re-authenticates and explicitly restarts it.",
            reason,
        )
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.stop(), self._loop)
        except Exception:
            logger.debug(
                "KiteTicker: failed to schedule stop() after auth fail",
                exc_info=True,
            )

    def _on_order_update(self, _ws: Any, order: dict[str, Any]) -> None:
        if self._order_update_cb is None or self._loop is None:
            return
        # Bridge to asyncio: schedule the coroutine onto the FastAPI loop
        # from this Twisted-thread callback.
        try:
            asyncio.run_coroutine_threadsafe(
                self._order_update_cb(order), self._loop,
            )
        except Exception:
            logger.debug("KiteTicker: order_update bridge failed", exc_info=True)
