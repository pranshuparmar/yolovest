"""Kite Connect market data provider (FR-2.1e).

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
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._kite: Any = None
        self._rate_limiter = asyncio.Semaphore(8)  # stay under 10 req/s
        # Instrument token cache: symbol -> instrument_token
        self._token_cache: dict[str, int] = {}

    def set_access_token(self, token: str) -> None:
        """Update the access token after daily re-authentication."""
        self._access_token = token
        self._kite = None  # force re-init

    def _get_kite(self) -> Any:
        """Lazy-init Kite Connect client."""
        if self._kite is None:
            from kiteconnect import KiteConnect

            self._kite = KiteConnect(api_key=self._api_key)
            if self._access_token:
                self._kite.set_access_token(self._access_token)
        return self._kite

    async def _get_instrument_token(self, symbol: str) -> int:
        """Resolve NSE symbol to Kite instrument token.

        Caches results to avoid repeated API calls.
        """
        if symbol in self._token_cache:
            return self._token_cache[symbol]

        kite = self._get_kite()
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
        end_date = date.today()
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
            return {
                "ltp": quote.get("last_price", 0),
                "volume": quote.get("volume", 0),
                "timestamp": quote.get("timestamp", datetime.now().isoformat()),
                "open": quote.get("ohlc", {}).get("open"),
                "high": quote.get("ohlc", {}).get("high"),
                "low": quote.get("ohlc", {}).get("low"),
                "close": quote.get("ohlc", {}).get("close"),
                "bid": quote.get("depth", {}).get("buy", [{}])[0].get("price"),
                "ask": quote.get("depth", {}).get("sell", [{}])[0].get("price"),
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
            return False
