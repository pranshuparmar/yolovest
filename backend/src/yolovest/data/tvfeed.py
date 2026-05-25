"""tvDatafeed market data provider (intraday, 5min/15min).

Unofficial TradingView API. Free tier: 5min bars, last 15 days.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar, is_valid_ohlc

logger = logging.getLogger(__name__)

# Interval mapping: our names → tvDatafeed Interval enum values
_TV_INTERVALS = {
    "5minute": "in_5_minute",
    "15minute": "in_15_minute",
    "daily": "in_daily",
    "1d": "in_daily",
}


class TVDatafeedProvider(MarketDataBase):
    """Intraday data provider using tvDatafeed for TradingView data."""

    def __init__(self, username: str = "", password: str = "") -> None:
        self._username = username
        self._password = password
        self._lock = asyncio.Semaphore(1)  # sequential requests only
        self._tv = None

    def _get_client(self) -> Any:
        """Lazy-init tvDatafeed client."""
        if self._tv is None:
            from tvDatafeed import TvDatafeed

            if self._username and self._password:
                self._tv = TvDatafeed(username=self._username, password=self._password)
            else:
                self._tv = TvDatafeed()  # anonymous, limited
        return self._tv

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 15
    ) -> list[OHLCVBar]:
        """Fetch intraday OHLCV via tvDatafeed."""
        tv_interval_str = _TV_INTERVALS.get(interval)
        if tv_interval_str is None:
            raise ValueError(f"Unsupported interval for tvDatafeed: {interval}")

        # Cap days for free tier
        days = min(days, 15)
        # Estimate bars needed: ~75 5min bars per day
        n_bars = days * 75 if "minute" in interval else days

        async with self._lock:
            bars = await asyncio.to_thread(
                self._fetch_data, symbol, tv_interval_str, n_bars
            )
        return bars

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get latest quote from most recent intraday bar."""
        bars = await self.get_ohlcv(symbol, "5minute", days=1)
        if not bars:
            raise ValueError(f"No intraday data for {symbol}")
        latest = bars[-1]
        return {
            "ltp": latest.close,
            "volume": latest.volume,
            "timestamp": latest.timestamp.isoformat(),
        }

    async def health_check(self) -> bool:
        """Check if tvDatafeed is accessible."""
        try:
            bars = await self.get_ohlcv("NIFTY", "5minute", days=1)
            return len(bars) > 0
        except Exception:
            logger.exception("tvDatafeed health check failed")
            return False

    def _fetch_data(
        self, symbol: str, tv_interval_str: str, n_bars: int
    ) -> list[OHLCVBar]:
        """Synchronous fetch using tvDatafeed (runs in thread)."""
        from tvDatafeed import Interval

        tv = self._get_client()
        tv_interval = getattr(Interval, tv_interval_str)

        df = tv.get_hist(
            symbol=symbol,
            exchange="NSE",
            interval=tv_interval,
            n_bars=n_bars,
        )

        if df is None or df.empty:
            return []

        bars = []
        for idx, row in df.iterrows():
            # Skip junk bars (NaN / non-positive) so one bad row doesn't
            # fail the whole fetch (OHLCVBar enforces gt=0).
            if not is_valid_ohlc(row["open"], row["high"], row["low"], row["close"]):
                continue
            ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else datetime.fromisoformat(str(idx))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            bars.append(
                OHLCVBar(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )
            )

        bars.sort(key=lambda b: b.timestamp)
        return bars
