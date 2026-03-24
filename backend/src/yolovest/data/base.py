"""Abstract market data provider interface (ABC).

All data providers (jugaad-data, yfinance, tvDatafeed, etc.) extend MarketDataBase.
See REQUIREMENTS.md FR-2.1 for the data source abstraction layer.
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
