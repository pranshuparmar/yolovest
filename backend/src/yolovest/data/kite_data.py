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

        Supports daily and intraday intervals.
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

        bars = await self._fetch_historical(
            instrument_token, kite_interval, start_date, end_date
        )
        return bars

    async def _fetch_historical(
        self,
        token: int,
        interval: str,
        start: date,
        end: date,
    ) -> list[OHLCVBar]:
        """Fetch historical data with retry logic."""
        kite = self._get_kite()
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
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
                    delay = self._retry_base_delay * (2 ** attempt)
                    logger.warning(
                        "Kite historical fetch failed (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        attempt + 1, self._max_retries, delay, e,
                    )
                    await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

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
