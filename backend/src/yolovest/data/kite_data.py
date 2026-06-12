"""Kite Connect market data provider.

Optional drop-in provider for users with the ₹500/month Kite data plan.
Provides real-time streaming quotes and full historical data via
kite.historical_data(). Plugs into the MarketDataBase abstraction and
can be used as primary or fallback in the ingester's provider chain.

Requires:
- Kite Connect API key + access token (daily re-auth)
- Data plan subscription on Zerodha account
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar, is_valid_ohlc
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)

# Map our interval names to Kite interval strings
_INTERVAL_MAP = {
    "daily": "day",
    "1d": "day",
    "5minute": "5minute",
    "15minute": "15minute",
    "1m": "minute",
    "60minute": "60minute",
}

# Kite's per-call date-range limits for historical_data().
# Source: https://kite.trade/docs/connect/v3/historical/
# Exceeding these returns "Date range exceeds maximum allowed".
_KITE_MAX_DAYS_PER_CALL = {
    "day": 2000,
    "60minute": 400,
    "30minute": 200,
    "15minute": 200,
    "10minute": 100,
    "5minute": 100,
    "3minute": 100,
    "minute": 60,
}


class KiteDataProvider(MarketDataBase):
    """Market data provider using Kite Connect historical data API.

    This provider requires the paid Kite data plan. It supports both
    daily and intraday intervals, making it suitable as a unified
    provider replacing jugaad-data + tvDatafeed.
    """

    def __init__(
        self,
        api_key: str,
        access_token: str | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        rate_limiter: Any = None,
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._kite: Any = None
        # Shared rate limiter. Pass the same instance the broker uses so
        # quote, order, and historical calls all draw from one budget.
        if rate_limiter is None:
            from yolovest.broker.kite_rate_limiter import KiteRateLimiter
            rate_limiter = KiteRateLimiter(calls_per_second=10.0, concurrency=8)
        self._rate_limiter = rate_limiter
        # Lock to prevent race condition when refreshing the kite client.
        # Also used to serialize the one-shot instrument cache warm-up.
        self._init_lock = asyncio.Lock()
        # Instrument token cache: symbol -> instrument_token
        self._token_cache: dict[str, int] = {}
        # Tracks whether _prewarm_token_cache has populated the cache from
        # the full NSE instrument master. Cleared on set_access_token().
        self._token_cache_warmed: bool = False
        # Flips to True the first time Kite rejects us with a token
        # error. Subsequent calls short-circuit via _assert_kite_authed
        # so a stale-token heartbeat doesn't burn through retries on
        # every symbol. Cleared on set_access_token() with a real token.
        self._token_known_invalid: bool = False
        # Time-based throttle for historical_data. The historical endpoint
        # has a tighter per-second limit than the general quote/order quota,
        # and a semaphore alone doesn't enforce inter-call spacing.
        self._historical_lock = asyncio.Lock()
        self._historical_last_call: float = 0.0
        # Minimum interval between historical fetches.
        self._historical_min_interval_sec: float = 0.4
        # Back-off interval applied after a 429 response.
        self._historical_cooldown_sec: float = 10.0

    def set_access_token(self, token: str) -> None:
        """Update the access token after daily re-authentication.

        Sets _kite to None so _get_kite() re-creates it with the new token.
        Safe against concurrent use: _get_kite() handles None atomically.
        """
        self._access_token = token
        self._kite = None  # _get_kite() will re-create with new token
        self._token_cache.clear()  # instrument tokens may change across sessions
        self._token_cache_warmed = False
        # A new token is being installed — clear the "known invalid"
        # short-circuit so the next call goes to Kite again. If the
        # caller is wiping the token (set_access_token("")), this
        # leaves _access_token falsy and _assert_kite_authed below
        # will short-circuit regardless.
        self._token_known_invalid = False

    def _assert_kite_authed(self) -> None:
        """Raise a fast, well-known error when we already know Kite
        won't accept us — either no token has ever been set, or a
        previous call hit a TokenException and flipped the
        "known invalid" flag. Skips the 3-retry × 8s backoff dance
        on every symbol of a heartbeat when the broker is logged out
        (weekends, expired token, post-logout), which was filling logs
        with the same `Incorrect api_key or access_token` warning.
        Cleared on the next successful set_access_token().
        """
        if not self._access_token:
            raise RuntimeError("Kite access_token unset; skipping Kite call")
        if self._token_known_invalid:
            raise RuntimeError(
                "Kite token previously rejected by server; skipping until re-auth"
            )

    @staticmethod
    def _is_token_error(exc: BaseException) -> bool:
        """Detect TokenException by class name + message substring.

        Class-name check avoids a hard import dependency on
        kiteconnect.exceptions (the module is optional). Message
        check is the fallback for transports that wrap the original.
        """
        if type(exc).__name__ == "TokenException":
            return True
        msg = str(exc).lower()
        return (
            "api_key" in msg or "access_token" in msg
            or "invalid token" in msg or "token expired" in msg
        )

    def _get_kite(self) -> Any:
        """Lazy-init Kite Connect client.

        Thread-safe: if _kite is None after token refresh, re-creates it.
        The _init_lock prevents concurrent re-initialization.
        """
        if self._kite is not None:
            return self._kite
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=self._api_key)
        if self._access_token:
            kite.set_access_token(self._access_token)
        self._kite = kite
        return self._kite

    _INDEX_SYMBOLS = {"NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY NEXT 50"}

    async def _prewarm_token_cache(self) -> None:
        """Fetch the NSE instrument master once and cache every
        tradingsymbol -> instrument_token mapping.

        Without this, every cache miss in _get_instrument_token would
        re-download the full instrument master (~5k entries, multi-MB),
        making bulk operations like ingest-universe N+1 expensive AND
        burning through the Kite rate-limit budget.
        """
        self._assert_kite_authed()
        kite = self._get_kite()
        async with self._rate_limiter:
            try:
                instruments = await asyncio.to_thread(kite.instruments, "NSE")
            except Exception as e:
                if self._is_token_error(e):
                    self._token_known_invalid = True
                raise
        for inst in instruments:
            sym = inst.get("tradingsymbol")
            token = inst.get("instrument_token")
            if sym and token and sym not in self._token_cache:
                self._token_cache[sym] = token
        self._token_cache_warmed = True
        logger.info(
            "Kite instrument cache warmed: %d tradingsymbols indexed",
            len(self._token_cache),
        )

    async def get_instrument_token(self, symbol: str) -> int | None:
        """Public alias for the cached symbol → token lookup. Returns
        None on failure (the private form raises) — convenient for
        consumers like KiteTickerClient that want a soft miss."""
        try:
            return await self._get_instrument_token(symbol)
        except Exception:
            return None

    async def _get_instrument_token(self, symbol: str) -> int:
        """Resolve NSE symbol to Kite instrument token.

        Pre-warms the full instrument master on first miss, then serves
        all subsequent lookups from memory. Handles both regular NSE
        stocks and NSE indices (NIFTY 50, etc.).
        """
        if symbol in self._token_cache:
            return self._token_cache[symbol]

        # First miss — populate cache from a single instruments() call.
        # Use the init_lock so concurrent first-misses don't all download.
        async with self._init_lock:
            if symbol not in self._token_cache and not getattr(
                self, "_token_cache_warmed", False,
            ):
                await self._prewarm_token_cache()

        if symbol in self._token_cache:
            return self._token_cache[symbol]

        if symbol in self._INDEX_SYMBOLS:
            raise ValueError(f"Index instrument token not found for {symbol}")
        raise ValueError(f"Instrument token not found for {symbol}")

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]:
        """Fetch OHLCV via Kite historical_data API.

        Supports daily and intraday intervals. Automatically paginates
        when the requested window exceeds Kite's per-interval limit
        (see _KITE_MAX_DAYS_PER_CALL).
        """
        # Validate the interval BEFORE the auth check — it's pure
        # input validation that shouldn't depend on token state, and
        # surfacing "unsupported interval" is more useful than masking
        # it behind "token unset" when a caller passes a bad value.
        kite_interval = _INTERVAL_MAP.get(interval)
        if kite_interval is None:
            raise ValueError(
                f"Unsupported interval '{interval}'. "
                f"Supported: {list(_INTERVAL_MAP.keys())}"
            )
        self._assert_kite_authed()

        instrument_token = await self._get_instrument_token(symbol)
        end_date = now_ist().date()
        start_date = end_date - timedelta(days=days)

        max_days = _KITE_MAX_DAYS_PER_CALL.get(kite_interval, 30)
        if days <= max_days:
            return await self._fetch_historical(
                instrument_token, kite_interval, start_date, end_date,
            )

        # Window exceeds Kite's per-call limit — chunk it.
        all_bars: list[OHLCVBar] = []
        chunk_start = start_date
        while chunk_start <= end_date:
            chunk_end = min(chunk_start + timedelta(days=max_days - 1), end_date)
            chunk = await self._fetch_historical(
                instrument_token, kite_interval, chunk_start, chunk_end,
            )
            all_bars.extend(chunk)
            chunk_start = chunk_end + timedelta(days=1)
        return all_bars

    async def _fetch_historical(
        self,
        token: int,
        interval: str,
        start: date,
        end: date,
    ) -> list[OHLCVBar]:
        """Fetch historical data with retry logic and rate limiting.

        Enforces a minimum interval between calls (time-based throttle)
        in addition to the semaphore. When Kite returns 429 ("Too many
        requests"), back off for self._historical_cooldown_sec before
        the next attempt to let the server-side rate window reset.
        """
        kite = self._get_kite()
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                await self._throttle_historical()
                async with self._rate_limiter:
                    data = await asyncio.to_thread(
                        kite.historical_data,
                        token,
                        start,
                        end,
                        interval,
                    )
                # Kite returns 0.0-OHLC placeholder bars for pre-listing /
                # no-trade days (e.g. requesting deep history for a recently
                # IPO'd symbol like MAZDOCK before its 2020 listing). Skip
                # any bar with a non-positive O/H/L/C instead of letting one
                # bad row fail the entire symbol's fetch (OHLCVBar enforces
                # gt=0). Volume may legitimately be 0, so it isn't filtered.
                bars: list[OHLCVBar] = []
                skipped = 0
                for row in data:
                    o, h, lo, cl = (
                        row.get("open"), row.get("high"),
                        row.get("low"), row.get("close"),
                    )
                    if not is_valid_ohlc(o, h, lo, cl):
                        skipped += 1
                        continue
                    ts = (
                        row["date"] if isinstance(row["date"], datetime)
                        else datetime.combine(row["date"], datetime.min.time())
                    )
                    bars.append(OHLCVBar(
                        timestamp=ts,
                        open=float(o), high=float(h), low=float(lo),
                        close=float(cl), volume=int(row.get("volume") or 0),
                    ))
                if skipped:
                    logger.debug(
                        "Kite: skipped %d non-positive/placeholder bars for token %s",
                        skipped, token,
                    )
                return bars
            except Exception as e:
                last_error = e
                # A TokenException is permanent within this token's
                # lifetime — flipping the flag short-circuits every
                # subsequent symbol in the same heartbeat instead of
                # retrying through the same wall.
                if self._is_token_error(e):
                    self._token_known_invalid = True
                    raise
                if attempt < self._max_retries - 1:
                    if self._is_rate_limit_error(e):
                        # Hard back-off: server-side window needs time to
                        # clear. Don't double-tap with a tight retry.
                        delay = self._historical_cooldown_sec
                        logger.warning(
                            "Kite historical rate-limited (attempt %d/%d), "
                            "cooling down %.1fs: %s",
                            attempt + 1, self._max_retries, delay, e,
                        )
                    else:
                        delay = self._retry_base_delay * (2 ** attempt)
                        logger.warning(
                            "Kite historical fetch failed (attempt %d/%d), "
                            "retrying in %.1fs: %s",
                            attempt + 1, self._max_retries, delay, e,
                        )
                    await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

    async def _throttle_historical(self) -> None:
        """Ensure at least _historical_min_interval_sec since the last call."""
        async with self._historical_lock:
            now = time.monotonic()
            elapsed = now - self._historical_last_call
            wait = self._historical_min_interval_sec - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._historical_last_call = time.monotonic()

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Detect Kite's 'Too many requests' response across error types."""
        msg = str(exc).lower()
        return (
            "too many requests" in msg
            or "rate limit" in msg
            or "429" in msg
        )

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get real-time quote via Kite API."""
        self._assert_kite_authed()
        kite = self._get_kite()
        nse_symbol = f"NSE:{symbol}"

        try:
            async with self._rate_limiter:
                quotes = await asyncio.to_thread(kite.quote, nse_symbol)

            quote = quotes.get(nse_symbol, {})
            ltp = quote.get("last_price")
            if not ltp or ltp <= 0:
                raise ValueError(f"Kite returned invalid LTP ({ltp}) for {symbol}")

            # Extract bid/ask safely — depth lists can be empty
            depth = quote.get("depth", {})
            buy_depth = depth.get("buy") or []
            sell_depth = depth.get("sell") or []

            # Aggregate top-5 levels into single quantities. The full
            # total_buy_quantity / total_sell_quantity from the quote
            # body cover the entire book; the top-5 sums proxy "what's
            # close to the touch and likely to clear within the
            # session". Order-flow features the OHLCV-only feature set
            # can't see.
            top5_buy_qty = sum(int(l.get("quantity") or 0) for l in buy_depth[:5])
            top5_sell_qty = sum(int(l.get("quantity") or 0) for l in sell_depth[:5])

            ohlc = quote.get("ohlc", {}) or {}
            return {
                "ltp": ltp,
                "volume": quote.get("volume", 0),
                "timestamp": quote.get("timestamp", now_ist().isoformat()),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "close": ohlc.get("close"),  # previous close
                "average_price": quote.get("average_price"),
                "upper_circuit": quote.get("upper_circuit_limit"),
                "lower_circuit": quote.get("lower_circuit_limit"),
                "bid": buy_depth[0].get("price") if buy_depth else None,
                "ask": sell_depth[0].get("price") if sell_depth else None,
                "depth": depth,
                "total_buy_quantity": int(quote.get("buy_quantity") or 0),
                "total_sell_quantity": int(quote.get("sell_quantity") or 0),
                "top5_buy_qty": top5_buy_qty,
                "top5_sell_qty": top5_sell_qty,
                "last_quantity": int(quote.get("last_quantity") or 0),
            }
        except Exception as e:
            if self._is_token_error(e):
                self._token_known_invalid = True
            logger.warning("Kite quote failed for %s: %s", symbol, e)
            raise

    async def get_quotes_batch(
        self, symbols: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Batched depth quotes for many symbols in few API calls.

        Kite's quote endpoint accepts up to ~500 instruments per call —
        one heartbeat's whole watchlist costs a single request instead
        of N. Returns {symbol: payload} with the depth-snapshot fields;
        symbols with invalid/missing quotes are silently absent (this
        feeds best-effort data collection, not trading decisions).
        """
        self._assert_kite_authed()
        kite = self._get_kite()
        out: dict[str, dict[str, Any]] = {}
        _BATCH = 450  # margin under Kite's per-call instrument cap
        for start in range(0, len(symbols), _BATCH):
            chunk = symbols[start:start + _BATCH]
            keys = [f"NSE:{sym}" for sym in chunk]
            try:
                async with self._rate_limiter:
                    quotes = await asyncio.to_thread(kite.quote, keys)
            except Exception as e:
                if self._is_token_error(e):
                    self._token_known_invalid = True
                logger.warning(
                    "Kite batch quote failed for %d symbols: %s",
                    len(chunk), e,
                )
                continue
            for sym in chunk:
                quote = quotes.get(f"NSE:{sym}") or {}
                ltp = quote.get("last_price")
                if not ltp or ltp <= 0:
                    continue
                depth = quote.get("depth", {}) or {}
                buy_depth = depth.get("buy") or []
                sell_depth = depth.get("sell") or []
                out[sym] = {
                    "ltp": float(ltp),
                    "bid": (buy_depth[0].get("price") if buy_depth else None),
                    "ask": (sell_depth[0].get("price") if sell_depth else None),
                    "total_buy_qty": int(quote.get("buy_quantity") or 0),
                    "total_sell_qty": int(quote.get("sell_quantity") or 0),
                    "top5_buy_qty": sum(
                        int(level.get("quantity") or 0)
                        for level in buy_depth[:5]
                    ),
                    "top5_sell_qty": sum(
                        int(level.get("quantity") or 0)
                        for level in sell_depth[:5]
                    ),
                    "volume": int(quote.get("volume") or 0),
                }
        return out

    async def health_check(self) -> bool:
        """Check if Kite API is accessible."""
        if not self._access_token:
            return False
        try:
            kite = self._get_kite()
            async with self._rate_limiter:
                await asyncio.to_thread(kite.profile)
            return True
        except Exception:
            logger.debug("Kite data health check failed", exc_info=True)
            return False

    def is_available(self) -> bool:
        """Skip the provider entirely in the ingester fallback chain
        when we know Kite won't accept us — either the access token
        has never been set or a prior call already hit a
        TokenException. This stops the per-symbol "Kite token
        previously rejected" WARNING storm during an unauth'd
        heartbeat. Cleared automatically when set_access_token is
        called with a fresh token.
        """
        return bool(self._access_token) and not self._token_known_invalid
