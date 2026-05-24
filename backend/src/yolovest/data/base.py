"""Abstract market data provider interface (ABC).

All data providers (jugaad-data, yfinance, tvDatafeed, etc.) extend MarketDataBase.
"""

from abc import ABC, abstractmethod
from typing import Any

from yolovest.models.schemas import OHLCVBar


class MarketDataBase(ABC):
    """Abstract base for market data providers.

    Provides a unified interface for fetching OHLCV data and quotes
    with automatic fallback chain support.
    """

    @abstractmethod
    async def get_ohlcv(
        self,
        symbol: str,
        interval: str,
        days: int = 30,
    ) -> list[OHLCVBar]:
        """Fetch OHLCV bars for a symbol.

        Args:
            symbol: NSE symbol (e.g., "RELIANCE").
            interval: Candle interval (e.g., "1d", "5minute", "15minute").
            days: Number of days of history to fetch.

        Returns:
            List of OHLCVBar sorted by timestamp ascending.
        """
        ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get the latest quote/LTP for a symbol.

        Returns a dict with at minimum: ltp, volume, timestamp.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if this data provider is reachable and responding."""
        ...

    def is_available(self) -> bool:
        """Synchronous, cheap availability check used by the ingester
        to skip dead providers without paying the per-symbol error
        cost. Default True — providers that can know they're down
        (e.g. KiteDataProvider when the access token has been
        rejected) should override and return False until the cause
        clears.
        """
        return True

    @property
    def source_name(self) -> str:
        """Short, stable provider identifier stamped into `ohlcv.source`
        so data provenance is auditable (e.g. tell Kite-sourced bars from
        free-provider ones). Derived from the class name by default:
        `KiteDataProvider` -> "kite", `JugaadDataProvider` -> "jugaad",
        `YFinanceProvider` -> "yfinance", `TVDatafeedProvider` ->
        "tvdatafeed". Override if the derivation is wrong.
        """
        name = type(self).__name__
        for suffix in ("DataProvider", "Provider"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        return name.lower()

