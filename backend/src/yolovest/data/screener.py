"""Screener.in fundamental data scraper.

Fetches PE, PB, debt-to-equity, promoter holdings, and quarterly results
for NSE stocks from Screener.in's public pages.

Screener.in provides stock data on pages like:
    https://www.screener.in/company/RELIANCE/consolidated/

Data is extracted from the HTML tables. Rate-limited to respect the site.
"""

import asyncio
import logging
import re
from typing import Any

import aiohttp

from yolovest.http_utils import scraper_headers

logger = logging.getLogger(__name__)

# Rate limit: be gentle with Screener.in to avoid 429s
_RATE_LIMIT_DELAY = 2.0


class ScreenerScraper:
    """Scrape fundamental data from Screener.in."""

    BASE_URL = "https://www.screener.in/company/{symbol}/consolidated/"

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=scraper_headers({"Accept": "text/html"}),
            )
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def fetch_fundamentals(self, symbol: str) -> dict[str, Any] | None:
        """Fetch fundamental data for a single symbol.

        Returns dict with keys: pe_ratio, pb_ratio, debt_to_equity,
        promoter_holding_pct, quarterly_revenue_growth_pct.
        Returns None if data unavailable.
        """
        url = self.BASE_URL.format(symbol=symbol)
        session = await self._get_session()

        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    logger.debug("Screener.in: %s not found (404)", symbol)
                    return None
                if resp.status != 200:
                    logger.warning("Screener.in: %s returned %d", symbol, resp.status)
                    return None

                html = await resp.text()
                return self._parse_fundamentals(html, symbol)

        except aiohttp.ClientError as e:
            logger.warning("Screener.in fetch failed for %s: %s", symbol, e)
            return None

    async def fetch_batch(
        self, symbols: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch fundamentals for multiple symbols with rate limiting.

        Returns dict of symbol -> fundamental data.
        """
        results: dict[str, dict[str, Any]] = {}

        for symbol in symbols:
            try:
                data = await self.fetch_fundamentals(symbol)
                if data:
                    results[symbol] = data
            except Exception as e:
                logger.warning("Screener.in batch: %s failed: %s", symbol, e)

            await asyncio.sleep(_RATE_LIMIT_DELAY)

        return results

    @staticmethod
    def _parse_fundamentals(html: str, symbol: str) -> dict[str, Any] | None:
        """Extract fundamental metrics from Screener.in HTML.

        Parses the key ratios section and quarterly results table.
        """
        data: dict[str, Any] = {}

        # --- Stock PE ---
        pe_match = re.search(
            r'Stock\s+P/E[^<]*</span>\s*<span[^>]*>\s*([\d.]+)', html
        )
        if pe_match:
            try:
                data["pe_ratio"] = float(pe_match.group(1))
            except ValueError:
                pass

        # --- Price to Book ---
        # Try "Book Value" approach: find book value, compute PB from current price
        pb_match = re.search(
            r'Price\s+to\s+book\s+value[^<]*</span>\s*<span[^>]*>\s*([\d.]+)', html
        )
        if pb_match:
            try:
                data["pb_ratio"] = float(pb_match.group(1))
            except ValueError:
                pass

        # --- Debt to Equity ---
        dte_match = re.search(
            r'Debt\s+to\s+equity[^<]*</span>\s*<span[^>]*>\s*([\d.]+)', html
        )
        if dte_match:
            try:
                data["debt_to_equity"] = float(dte_match.group(1))
            except ValueError:
                pass

        # --- Promoter Holding ---
        promoter_match = re.search(
            r'Promoter\s+holding[^<]*</span>\s*<span[^>]*>\s*([\d.]+)\s*%', html
        )
        if promoter_match:
            try:
                data["promoter_holding_pct"] = float(promoter_match.group(1))
            except ValueError:
                pass

        # --- Quarterly Revenue Growth (QoQ) ---
        # Look for "Sales growth" or "Revenue growth" in ratios section
        growth_match = re.search(
            r'(?:Sales|Revenue)\s+growth[^<]*</span>\s*<span[^>]*>\s*([-\d.]+)\s*%', html
        )
        if growth_match:
            try:
                data["quarterly_revenue_growth_pct"] = float(growth_match.group(1))
            except ValueError:
                pass

        if not data:
            logger.debug("Screener.in: no fundamentals parsed for %s", symbol)
            return None

        return data
