"""Market data ingester with automatic fallback chain.

Wraps multiple MarketDataBase providers with:
- Automatic failover (primary → fallback → error)
- Data staleness validation
- Data quality validation (high >= low, close in range)
- Implements MarketDataProtocol so it can be used as ctx.market_data
"""

import logging
import statistics
from datetime import timedelta
from typing import Any

from yolovest.data.base import MarketDataBase
from yolovest.models.schemas import OHLCVBar
from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)

# A daily bar whose close is more than this multiple (or less than its
# reciprocal) of the fetch's median close is treated as wrong-symbol /
# wrong-scale corruption. Set lenient (5×) so a genuinely volatile name's
# intra-window move survives while 7×–227× junk (the observed range) is
# dropped.
_DAILY_OUTLIER_FACTOR = 5.0


class MarketDataIngester(MarketDataBase):
    """Fallback chain orchestrator for market data providers."""

    def __init__(
        self,
        daily_providers: list[MarketDataBase],
        intraday_provider: MarketDataBase | None = None,
        intraday_fallback: MarketDataBase | None = None,
        stale_threshold_minutes: int = 30,
    ) -> None:
        """Initialize with ordered list of daily providers and optional intraday provider.

        Args:
            daily_providers: Ordered list — first is primary, rest are fallbacks.
            intraday_provider: Separate provider for intraday intervals (e.g., tvDatafeed).
            intraday_fallback: Fallback intraday provider if primary fails.
            stale_threshold_minutes: Reject data older than this.
        """
        if not daily_providers:
            raise ValueError("At least one daily provider is required")
        self._daily_providers = daily_providers
        self._intraday_provider = intraday_provider
        self._intraday_fallback = intraday_fallback
        self._stale_minutes = stale_threshold_minutes
        # Per-symbol metadata from the last fetch (provider errors, empties)
        self._last_fetch_meta: dict[str, dict[str, Any]] = {}

    def get_fetch_meta(self, symbol: str) -> dict[str, Any]:
        """Get metadata from the last fetch for a symbol.

        Returns dict with:
        - provider_errors: number of providers that raised exceptions
        - providers_empty: number that returned empty data (e.g. delisted)
        - all_providers_tried: True if every provider was tried (fallback chain exhausted)
        """
        return self._last_fetch_meta.get(symbol, {})

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30,
        *, skip_stale_check: bool = False,
    ) -> list[OHLCVBar]:
        """Fetch OHLCV with automatic fallback and validation.

        Args:
            skip_stale_check: If True, accept data regardless of age.
                Use for backfill/universe ingestion where historical data is fine.
        """
        providers = self._select_providers(interval)
        # Skip providers that report themselves unavailable up front so a
        # known-dead provider (e.g. KiteDataProvider after auth rejection)
        # doesn't emit one WARNING per symbol for the rest of the
        # heartbeat. is_available() is a cheap synchronous flag check;
        # providers without an override default True.
        available = [p for p in providers if p.is_available()]
        if not available:
            available = providers  # nothing reported available — try anyway
        providers = available
        last_error: Exception | None = None
        best_stale_bars: list[OHLCVBar] | None = None
        best_stale_source: str | None = None
        provider_errors = 0
        providers_empty = 0

        for provider in providers:
            try:
                bars = await provider.get_ohlcv(symbol, interval, days)
                bars = self._validate_bars(bars, interval, provider.source_name, symbol)
                if not bars:
                    providers_empty += 1
                    logger.debug(
                        "Provider %s returned empty for %s",
                        type(provider).__name__, symbol,
                    )
                    continue
                if skip_stale_check or not self._is_stale(bars, interval):
                    # Track provider health for quarantine decisions + the
                    # winning provider so callers can stamp ohlcv.source.
                    self._last_fetch_meta[symbol] = {
                        "provider_errors": provider_errors,
                        "providers_empty": providers_empty,
                        "all_providers_tried": False,
                        "source": provider.source_name,
                    }
                    return bars
                # Stale but valid — keep as fallback
                logger.warning(
                    "Stale data from %s for %s (latest: %s)",
                    type(provider).__name__, symbol,
                    bars[-1].timestamp,
                )
                if best_stale_bars is None or len(bars) > len(best_stale_bars):
                    best_stale_bars = bars
                    best_stale_source = provider.source_name
                last_error = ValueError(f"Stale data from {type(provider).__name__}")
                continue
            except Exception as e:
                provider_errors += 1
                logger.warning(
                    "Provider %s failed for %s: %s",
                    type(provider).__name__, symbol, e,
                )
                last_error = e
                continue

        # All providers tried — record metadata. `source` is the provider
        # behind best_stale_bars (None if nothing was returned at all).
        self._last_fetch_meta[symbol] = {
            "provider_errors": provider_errors,
            "providers_empty": providers_empty,
            "all_providers_tried": True,
            "source": best_stale_source,
        }

        # If all providers returned stale data, return the best one anyway
        # (better to have stale data in DB than nothing)
        if best_stale_bars:
            return best_stale_bars

        if last_error:
            raise last_error
        raise ValueError(f"No providers returned data for {symbol}/{interval}")

    async def get_quotes_batch(
        self, symbols: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Batched depth quotes — Kite-only (the free providers expose no
        order book). Returns {} when the Kite provider is absent or
        unavailable, so callers can treat depth collection as strictly
        best-effort."""
        for provider in [
            *self._daily_providers,
            *( [self._intraday_provider] if self._intraday_provider else [] ),
        ]:
            fn = getattr(provider, "get_quotes_batch", None)
            if fn is None or not provider.is_available():
                continue
            try:
                return await fn(symbols)
            except Exception:
                logger.warning("batch depth quote failed", exc_info=True)
                return {}
        return {}

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get latest quote with fallback."""
        providers = self._daily_providers.copy()
        if self._intraday_provider and self._intraday_provider not in providers:
            providers.insert(0, self._intraday_provider)
        # Same availability gate the OHLCV path uses — skip dead
        # providers up front instead of catching their errors per symbol.
        available = [p for p in providers if p.is_available()]
        if available:
            providers = available

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

    async def get_ltp(self, symbol: str) -> float:
        """Get last traded price for a symbol.

        Fetches quote and extracts LTP. Falls back through providers.
        """
        quote = await self.get_quote(symbol)
        ltp = quote.get("ltp") or quote.get("last_price") or quote.get("close")
        if ltp is None:
            raise ValueError(f"No LTP available for {symbol}")
        return float(ltp)

    async def health_check(self) -> bool:
        """Return True if at least one provider is up."""
        for provider in self._all_providers():
            try:
                if await provider.health_check():
                    return True
            except Exception:
                logger.debug("Health check failed for provider %s", type(provider).__name__, exc_info=True)
                continue
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _select_providers(self, interval: str) -> list[MarketDataBase]:
        """Select providers based on interval type."""
        if interval in ("5minute", "15minute", "1m"):
            providers = []
            if self._intraday_provider:
                providers.append(self._intraday_provider)
            if self._intraday_fallback and self._intraday_fallback not in providers:
                providers.append(self._intraday_fallback)
            if not providers:
                raise ValueError(f"No intraday provider configured for interval {interval}")
            return providers
        return self._daily_providers

    def _all_providers(self) -> list[MarketDataBase]:
        """All providers for health check."""
        providers = self._daily_providers.copy()
        if self._intraday_provider:
            providers.append(self._intraday_provider)
        return providers

    def _is_stale(self, bars: list[OHLCVBar], interval: str) -> bool:
        """Check if the most recent bar is too old.

        Uses IST-aware comparison. Naive timestamps from providers are
        treated as IST (Indian market data convention).
        """
        if not bars:
            return True
        latest = bars[-1].timestamp
        now = now_ist()
        # Normalize naive timestamps to IST for comparison
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=IST)
        if interval in ("daily", "1d"):
            # Weekend/holiday-aware: count TRADING days elapsed (Mon-Fri), not
            # calendar days. The old flat 2-calendar-day threshold flagged the
            # last Friday bar as stale every Monday (3 calendar days) and on
            # Sundays via intraday clock drift right at the boundary — causing
            # spurious "stale" warnings, extra provider calls, and a flaky
            # test. "Fresh" = within ~2 trading sessions. Date-based, so the
            # noisy intraday-time comparison is dropped. Holidays aren't
            # modelled, but the 2-session tolerance absorbs a single one.
            cal_days = (now.date() - latest.date()).days
            if cal_days <= 0:
                return False
            if cal_days > 8:  # over a week of calendar days: unambiguously stale
                return True
            weekend_days = sum(
                1
                for i in range(1, cal_days + 1)
                if (latest.date() + timedelta(days=i)).weekday() >= 5
            )
            return (cal_days - weekend_days) > 2
        threshold = timedelta(minutes=self._stale_minutes)
        return (now - latest) > threshold

    @staticmethod
    def _validate_bars(
        bars: list[OHLCVBar], interval: str = "daily",
        source: str = "", symbol: str = "",
    ) -> list[OHLCVBar]:
        """Filter out bars with invalid data quality.

        Beyond per-bar OHLC sanity (high ≥ low, close/open in range), two
        inter-bar guards catch the systematic corruption seen from the
        free fallback providers (kite, the paid primary, is trusted and
        exempt from both):

        - **Weekend reject** (daily, non-kite): a daily bar dated Sat/Sun is
          almost always a mis-dated bar (e.g. a UTC↔IST off-by-one pushing
          Monday's session onto Sunday). Real special sessions (Muhurat /
          Budget Saturdays) come from kite, which is exempt.
        - **Price-outlier reject** (daily, ≥5 bars): a bar whose close is
          wildly off the fetch's median close is a wrong-symbol / wrong-scale
          bar (e.g. jugaad returning M&M's ~₹2330 for M&MFIN's ~₹310). These
          poison ATR and produce nonsense target/SL, so drop them at ingest.
        """
        valid = []
        # Sub-tick OHLC repair tolerance. A 1-min feed often prints an
        # open/close a hair outside [low, high] (aggregation rounding) —
        # dropping the whole bar loses data the labelers need. Clamp the
        # offending value into range when the violation is within the
        # larger of one default tick (0.05) or 0.1% of price; gross
        # violations (wrong scale / wrong symbol) still hard-drop.
        _tick_tol = 0.05
        _rel_tol = 0.001
        for bar in bars:
            if bar.high < bar.low:
                logger.warning("Dropping bar with high < low: %s", bar)
                continue
            tol = max(_tick_tol, bar.high * _rel_tol)
            if bar.close < bar.low or bar.close > bar.high:
                clamped = min(max(bar.close, bar.low), bar.high)
                if abs(bar.close - clamped) <= tol:
                    bar.close = clamped
                else:
                    logger.warning("Dropping bar with close outside [low, high]: %s", bar)
                    continue
            if bar.open < bar.low or bar.open > bar.high:
                clamped = min(max(bar.open, bar.low), bar.high)
                if abs(bar.open - clamped) <= tol:
                    bar.open = clamped
                else:
                    logger.warning("Dropping bar with open outside [low, high]: %s", bar)
                    continue
            if (
                interval in ("daily", "1d")
                and source != "kite"
                and bar.timestamp.weekday() >= 5
            ):
                logger.warning(
                    "Dropping weekend-dated daily bar (%s, %s) from %s: %s",
                    symbol, bar.timestamp.date(), source or "?", bar,
                )
                continue
            valid.append(bar)

        if interval in ("daily", "1d") and source != "kite" and len(valid) >= 5:
            median_close = statistics.median(b.close for b in valid)
            if median_close > 0:
                kept = []
                for bar in valid:
                    ratio = bar.close / median_close
                    if ratio > _DAILY_OUTLIER_FACTOR or ratio < 1 / _DAILY_OUTLIER_FACTOR:
                        logger.warning(
                            "Dropping price-outlier daily bar (%s, %s) from %s: "
                            "close=%.2f is %.1fx the fetch median %.2f — likely "
                            "wrong-symbol data",
                            symbol, bar.timestamp.date(), source or "?",
                            bar.close, ratio, median_close,
                        )
                        continue
                    kept.append(bar)
                valid = kept
        return valid
