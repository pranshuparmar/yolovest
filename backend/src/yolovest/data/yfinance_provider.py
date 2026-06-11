"""yfinance market data provider (fallback, daily/EOD).

Yahoo Finance via .NS suffix. 20 years history. Fragile rate limits.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar, is_valid_ohlc
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)

_yf_cache_configured = False


def configure_yfinance_cache() -> None:
    """Point yfinance's timezone cache at a writable temp dir created
    with exist_ok=True.

    yfinance (some versions) does an mkdir WITHOUT exist_ok on its
    default ~/.cache/py-yfinance dir, so when the dir already exists it
    logs "Error creating TzCache folder ... [Errno 17] File exists" and
    silently disables tz caching (re-fetching timezones every call).
    Redirecting to a pre-created dir both silences the recurring warning
    and restores the cache. Best-effort + run-once; yfinance works
    regardless if this fails.
    """
    global _yf_cache_configured
    if _yf_cache_configured:
        return
    _yf_cache_configured = True
    try:
        import os
        import tempfile

        import yfinance as yf

        cache_dir = os.path.join(tempfile.gettempdir(), "yfinance_tz_cache")
        os.makedirs(cache_dir, exist_ok=True)
        yf.set_tz_cache_location(cache_dir)
    except Exception:
        logger.debug("yfinance tz-cache configuration skipped", exc_info=True)


# Interval mapping: our names → yfinance names
_YF_INTERVALS = {
    "daily": "1d",
    "1d": "1d",
    "5minute": "5m",
    "15minute": "15m",
}


class YFinanceProvider(MarketDataBase):
    """Fallback data provider using yfinance for NSE data via .NS suffix."""

    def __init__(self) -> None:
        self._lock = asyncio.Semaphore(2)  # max 2 concurrent requests
        self._last_request = 0.0
        configure_yfinance_cache()

    def _nse_symbol(self, symbol: str) -> str:
        """Convert NSE symbol to yfinance format."""
        if not symbol.endswith(".NS"):
            return f"{symbol}.NS"
        return symbol

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]:
        """Fetch OHLCV via yfinance."""
        yf_interval = _YF_INTERVALS.get(interval)
        if yf_interval is None:
            raise ValueError(f"Unsupported interval for yfinance: {interval}")

        async with self._lock:
            # Rate limit: 0.5s between requests
            await asyncio.sleep(0.5)
            bars = await asyncio.to_thread(
                self._fetch_data, symbol, yf_interval, days
            )
        return bars

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get latest quote via yfinance fast_info."""
        async with self._lock:
            await asyncio.sleep(0.5)
            quote = await asyncio.to_thread(self._fetch_quote, symbol)
        return quote

    async def health_check(self) -> bool:
        """Check if yfinance is accessible."""
        try:
            bars = await self.get_ohlcv("RELIANCE", "daily", days=5)
            return len(bars) > 0
        except Exception:
            logger.exception("yfinance health check failed")
            return False

    def _fetch_data(
        self, symbol: str, yf_interval: str, days: int
    ) -> list[OHLCVBar]:
        """Synchronous fetch using yfinance (runs in thread)."""
        import yfinance as yf

        ticker = yf.Ticker(self._nse_symbol(symbol))
        period = f"{days}d" if days <= 730 else "max"
        df = ticker.history(period=period, interval=yf_interval)

        if df.empty:
            return []

        bars = []
        for idx, row in df.iterrows():
            # Skip junk bars (NaN / non-positive) — yfinance returns
            # incomplete or 0.0 rows for recently-listed / illiquid stocks.
            o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
            if not is_valid_ohlc(o, h, l, c):
                continue

            ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else datetime.fromisoformat(str(idx))
            # Strip timezone for consistency
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            bars.append(
                OHLCVBar(
                    timestamp=ts,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=int(row["Volume"]),
                )
            )

        bars.sort(key=lambda b: b.timestamp)
        return bars

    def _fetch_quote(self, symbol: str) -> dict[str, Any]:
        """Synchronous quote fetch (runs in thread)."""
        import yfinance as yf

        ticker = yf.Ticker(self._nse_symbol(symbol))
        info = ticker.fast_info
        return {
            "ltp": float(info.last_price) if hasattr(info, "last_price") else 0.0,
            "volume": int(info.last_volume) if hasattr(info, "last_volume") else 0,
            "timestamp": now_ist().isoformat(),
        }
