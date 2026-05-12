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

from yolovest.timezone import now_ist
from typing import Any

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar

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
        rate_limiter: asyncio.Semaphore | None = None,
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._kite: Any = None
        # Share rate limiter with broker to respect Kite's 10 req/s aggregate limit
        self._rate_limiter = rate_limiter or asyncio.Semaphore(8)
        # Lock to prevent race condition when refreshing the kite client
        self._init_lock = asyncio.Lock()
        # Instrument token cache: symbol -> instrument_token
        self._token_cache: dict[str, int] = {}
        # Time-based throttle for historical_data. Kite's historical API has
        # a tighter per-second limit (~3 req/s) than the general 10 req/s
        # quote/order quota. A semaphore alone doesn't enforce time — long
        # sequential runs (e.g. /run ingest-universe over 500 symbols) saw
        # "Too many requests" errors despite no concurrent calls. Lock +
        # last-call timestamp enforces a minimum interval between requests.
        self._historical_lock = asyncio.Lock()
        self._historical_last_call: float = 0.0
        # 0.4s -> max 2.5 req/s, safely under Kite's 3 req/s historical cap.
        self._historical_min_interval_sec: float = 0.4
        # When a 429 ("Too many requests") fires, back off this much before
        # the next attempt. Reset to default on a successful call.
        self._historical_cooldown_sec: float = 10.0

    def set_access_token(self, token: str) -> None:
        """Update the access token after daily re-authentication.

        Sets _kite to None so _get_kite() re-creates it with the new token.
        Safe against concurrent use: _get_kite() handles None atomically.
        """
        self._access_token = token
        self._kite = None  # _get_kite() will re-create with new token
        self._token_cache.clear()  # instrument tokens may change across sessions

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

    async def _get_instrument_token(self, symbol: str) -> int:
        """Resolve NSE symbol to Kite instrument token.

        Handles both regular NSE stocks and NSE indices (NIFTY 50, etc.).
        Caches results to avoid repeated API calls.
        """
        if symbol in self._token_cache:
            return self._token_cache[symbol]

        kite = self._get_kite()

        # Index symbols live on a separate "indices" exchange in Kite
        if symbol in self._INDEX_SYMBOLS:
            instruments = await asyncio.to_thread(kite.instruments, "NSE")
            for inst in instruments:
                if inst["tradingsymbol"] == symbol and inst["instrument_type"] == "EQ":
                    self._token_cache[symbol] = inst["instrument_token"]
                    return inst["instrument_token"]
            # Fallback: try with "INDICES" segment (Kite uses instrument_type)
            for inst in instruments:
                if inst["tradingsymbol"] == symbol:
                    self._token_cache[symbol] = inst["instrument_token"]
                    return inst["instrument_token"]
            raise ValueError(f"Index instrument token not found for {symbol}")

        instruments = await asyncio.to_thread(kite.instruments, "NSE")
        for inst in instruments:
            if inst["tradingsymbol"] == symbol:
                self._token_cache[symbol] = inst["instrument_token"]
                return inst["instrument_token"]

        raise ValueError(f"Instrument token not found for {symbol}")

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]:
        """Fetch OHLCV via Kite historical_data API.

        Supports daily and intraday intervals. Automatically paginates
        when the requested window exceeds Kite's per-interval limit
        (see _KITE_MAX_DAYS_PER_CALL).
        """
        kite_interval = _INTERVAL_MAP.get(interval)
        if kite_interval is None:
            raise ValueError(
                f"Unsupported interval '{interval}'. "
                f"Supported: {list(_INTERVAL_MAP.keys())}"
            )

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
                return [
                    OHLCVBar(
                        timestamp=row["date"] if isinstance(row["date"], datetime)
                        else datetime.combine(row["date"], datetime.min.time()),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(row["volume"]),
                    )
                    for row in data
                ]
            except Exception as e:
                last_error = e
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

            return {
                "ltp": ltp,
                "volume": quote.get("volume", 0),
                "timestamp": quote.get("timestamp", now_ist().isoformat()),
                "open": quote.get("ohlc", {}).get("open"),
                "high": quote.get("ohlc", {}).get("high"),
                "low": quote.get("ohlc", {}).get("low"),
                "close": quote.get("ohlc", {}).get("close"),
                "bid": buy_depth[0].get("price") if buy_depth else None,
                "ask": sell_depth[0].get("price") if sell_depth else None,
            }
        except Exception as e:
            logger.warning("Kite quote failed for %s: %s", symbol, e)
            raise

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
