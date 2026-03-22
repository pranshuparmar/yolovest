"""Market data ingester with automatic fallback chain.

Wraps multiple MarketDataBase providers with:
- Automatic failover (primary → fallback → error)
- Data staleness validation (FR-10.4)
- Data quality validation (high >= low, close in range)
- Implements MarketDataProtocol so it can be used as ctx.market_data

See REQUIREMENTS.md FR-2.1 for the fallback chain design.
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar

logger = logging.getLogger(__name__)


class MarketDataIngester(MarketDataBase):
    """Fallback chain orchestrator for market data providers."""

    def __init__(
        self,
        daily_providers: list[MarketDataBase],
        intraday_provider: MarketDataBase | None = None,
        stale_threshold_minutes: int = 30,
    ) -> None:
        """Initialize with ordered list of daily providers and optional intraday provider.

        Args:
            daily_providers: Ordered list — first is primary, rest are fallbacks.
            intraday_provider: Separate provider for intraday intervals (e.g., tvDatafeed).
            stale_threshold_minutes: Reject data older than this (FR-10.4).
        """
        if not daily_providers:
            raise ValueError("At least one daily provider is required")
        self._daily_providers = daily_providers
        self._intraday_provider = intraday_provider
        self._stale_minutes = stale_threshold_minutes

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]:
        """Fetch OHLCV with automatic fallback and validation."""
        providers = self._select_providers(interval)
        last_error: Exception | None = None

        for provider in providers:
            try:
                bars = await provider.get_ohlcv(symbol, interval, days)
                bars = self._validate_bars(bars)
                if bars and not self._is_stale(bars, interval):
                    return bars
                if bars and self._is_stale(bars, interval):
                    logger.warning(
                        "Stale data from %s for %s (latest: %s)",
                        type(provider).__name__, symbol,
                        bars[-1].timestamp if bars else "none",
                    )
                    last_error = ValueError(f"Stale data from {type(provider).__name__}")
                    continue
            except Exception as e:
                logger.warning(
                    "Provider %s failed for %s: %s",
                    type(provider).__name__, symbol, e,
                )
                last_error = e
                continue

        if last_error:
            raise last_error
        raise ValueError(f"No providers returned data for {symbol}/{interval}")

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get latest quote with fallback."""
        providers = self._daily_providers.copy()
        if self._intraday_provider:
            providers.insert(0, self._intraday_provider)

        last_error: Exception | None = None
        for provider in providers:
            try:
                return await provider.get_quote(symbol)
            except Exception as e:
                logger.warning(
                    "Quote from %s failed for %s: %s",
                    type(provider).__name__, symbol, e,
                )
                last_error = e
                continue

        if last_error:
            raise last_error
        raise ValueError(f"No providers returned quote for {symbol}")

    async def health_check(self) -> bool:
        """Return True if at least one provider is up."""
        for provider in self._all_providers():
            try:
                if await provider.health_check():
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _select_providers(self, interval: str) -> list[MarketDataBase]:
        """Select providers based on interval type."""
        if interval in ("5minute", "15minute", "1m"):
            if self._intraday_provider:
                return [self._intraday_provider]
            raise ValueError(f"No intraday provider configured for interval {interval}")
        return self._daily_providers

    def _all_providers(self) -> list[MarketDataBase]:
        """All providers for health check."""
        providers = self._daily_providers.copy()
        if self._intraday_provider:
            providers.append(self._intraday_provider)
        return providers

    def _is_stale(self, bars: list[OHLCVBar], interval: str) -> bool:
        """Check if the most recent bar is too old (FR-10.4).

        Uses IST-aware comparison. Naive timestamps from providers are
        treated as IST (Indian market data convention).
        """
        if not bars:
            return True
        latest = bars[-1].timestamp
        ist = ZoneInfo("Asia/Kolkata")
        now = datetime.now(ist)
        # Normalize naive timestamps to IST for comparison
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=ist)
        # For daily data, stale means no data from today or yesterday
        if interval in ("daily", "1d"):
            threshold = timedelta(days=2)
        else:
            threshold = timedelta(minutes=self._stale_minutes)
        return (now - latest) > threshold

    @staticmethod
    def _validate_bars(bars: list[OHLCVBar]) -> list[OHLCVBar]:
        """Filter out bars with invalid data quality."""
        valid = []
        for bar in bars:
            if bar.high < bar.low:
                logger.warning("Dropping bar with high < low: %s", bar)
                continue
            if bar.close < bar.low or bar.close > bar.high:
                logger.warning("Dropping bar with close outside [low, high]: %s", bar)
                continue
            if bar.open < bar.low or bar.open > bar.high:
                logger.warning("Dropping bar with open outside [low, high]: %s", bar)
                continue
            valid.append(bar)
        return valid
