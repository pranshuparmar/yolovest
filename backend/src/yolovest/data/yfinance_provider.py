"""yfinance market data provider (fallback, daily/EOD).

Yahoo Finance via .NS suffix. 20 years history. Fragile rate limits.
See REQUIREMENTS.md FR-2.1b.
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar

logger = logging.getLogger(__name__)

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
            # Skip rows with NaN values — yfinance sometimes returns
            # incomplete bars for recently listed or illiquid stocks
            o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
            if any(math.isnan(v) for v in (o, h, l, c)):
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
            "timestamp": datetime.now().isoformat(),
        }
