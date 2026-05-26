"""jugaad-data market data provider (primary, daily/EOD).

Scrapes NSE directly. Built-in caching. History from 2013+.
"""

import asyncio
import logging
import warnings
from datetime import date, datetime, timedelta
from typing import Any

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar, is_valid_ohlc
from yolovest.timezone import IST

logger = logging.getLogger(__name__)


def _ist_trading_date(raw: Any) -> date:
    """Extract the NSE trading date from a jugaad-data DATE value, in IST.

    jugaad-data returns DATE as a (sometimes tz-aware) pandas Timestamp.
    Reading ``.date()`` off a tz-aware value gives the date in that value's
    OWN timezone — for a midnight-IST bar carried as UTC that rolls a day
    back (Mon 00:00 IST == Sun 18:30 UTC), which stamped daily bars on the
    wrong (often weekend) date. Normalise to IST before taking the date so
    the bar lands on its real trading day.
    """
    if getattr(raw, "tzinfo", None) is not None:
        try:
            raw = raw.astimezone(IST)
        except Exception:
            pass
    if hasattr(raw, "date"):
        return raw.date()
    return datetime.fromisoformat(str(raw)).date()


# jugaad-data's util.py emits this on every call because it converts a
# tz-aware datetime to np.datetime64, which numpy doesn't support
# (np strips the tz and warns). Harmless for our use — we already
# normalise timestamps downstream — and there's nothing we can fix at
# the call site without patching the library. Filter it once at
# import so it doesn't spam every heartbeat.
warnings.filterwarnings(
    "ignore",
    message="no explicit representation of timezones available for np.datetime64",
    category=UserWarning,
    module=r"jugaad_data\..*",
)


class JugaadDataProvider(MarketDataBase):
    """Primary data provider using jugaad-data for NSE daily/EOD data."""

    def __init__(self) -> None:
        self._lock = asyncio.Semaphore(3)  # limit concurrent NSE requests

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]:
        """Fetch daily OHLCV via jugaad-data. Only supports daily interval."""
        if interval not in ("daily", "1d"):
            raise ValueError(f"JugaadDataProvider only supports daily interval, got {interval}")

        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        async with self._lock:
            bars = await asyncio.to_thread(
                self._fetch_stock_data, symbol, start_date, end_date
            )
        return bars

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get latest quote by fetching last trading day's data."""
        bars = await self.get_ohlcv(symbol, "daily", days=5)
        if not bars:
            raise ValueError(f"No data returned for {symbol}")
        latest = bars[-1]
        return {
            "ltp": latest.close,
            "volume": latest.volume,
            "timestamp": latest.timestamp.isoformat(),
        }

    async def health_check(self) -> bool:
        """Check if NSE data is accessible via jugaad-data."""
        try:
            bars = await self.get_ohlcv("RELIANCE", "daily", days=5)
            return len(bars) > 0
        except Exception:
            logger.exception("jugaad-data health check failed")
            return False

    @staticmethod
    def _fetch_stock_data(
        symbol: str, start_date: date, end_date: date
    ) -> list[OHLCVBar]:
        """Synchronous fetch using jugaad-data (runs in thread)."""
        from jugaad_data.nse import stock_df

        df = stock_df(
            symbol=symbol,
            from_date=start_date,
            to_date=end_date,
            series="EQ",
        )

        bars = []
        for _, row in df.iterrows():
            # Skip junk bars (NaN / non-positive) so one bad row doesn't
            # fail the whole symbol's fetch (OHLCVBar enforces gt=0).
            if not is_valid_ohlc(row["OPEN"], row["HIGH"], row["LOW"], row["CLOSE"]):
                continue
            bars.append(
                OHLCVBar(
                    timestamp=datetime.combine(
                        _ist_trading_date(row["DATE"]), datetime.min.time()
                    ),
                    open=float(row["OPEN"]),
                    high=float(row["HIGH"]),
                    low=float(row["LOW"]),
                    close=float(row["CLOSE"]),
                    volume=int(row["VOLUME"]) if "VOLUME" in row.index else int(row.get("TOTTRDQTY", 0)),
                )
            )

        bars.sort(key=lambda b: b.timestamp)
        return bars
