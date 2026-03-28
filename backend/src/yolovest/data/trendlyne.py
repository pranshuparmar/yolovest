"""Trendlyne technical screener scraper.

Fetches momentum scores, volume breakouts, and technical signals from
Trendlyne's public pages and API endpoints.

Trendlyne provides:
- Momentum score (0-10 composite)
- Volume breakout detection
- Technical signals (RSI, MACD crossover, etc.)
- DMA signals (price vs 50/200 DMA)

Data is stored in the fundamentals table alongside Screener.in data,
or used directly by the market-scan skill for scoring.
"""

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

from yolovest.http_utils import scraper_headers

logger = logging.getLogger(__name__)

_RATE_LIMIT_DELAY = 0.5


class TrendlyneScraper:
    """Scrape technical screener data from Trendlyne."""

    BASE_URL = "https://trendlyne.com/equity/{symbol}/"
    # Trendlyne has some JSON API endpoints for stock data
    API_URL = "https://trendlyne.com/api/eq/stock-data/{symbol}/"

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=scraper_headers({"Accept": "text/html,application/json"}),
            )
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def fetch_technicals(self, symbol: str) -> dict[str, Any] | None:
        """Fetch technical screener data for a single symbol.

        Returns dict with keys: momentum_score, volume_breakout,
        dma_50_signal, dma_200_signal, technical_signals.
        Returns None if data unavailable.
        """
        # Try API endpoint first (faster, structured)
        data = await self._fetch_from_api(symbol)
        if data:
            return data

        # Fallback to HTML scraping
        return await self._fetch_from_html(symbol)

    async def fetch_batch(
        self, symbols: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch technicals for multiple symbols with rate limiting."""
        results: dict[str, dict[str, Any]] = {}

        for symbol in symbols:
            try:
                data = await self.fetch_technicals(symbol)
                if data:
                    results[symbol] = data
            except Exception as e:
                logger.warning("Trendlyne batch: %s failed: %s", symbol, e)

            await asyncio.sleep(_RATE_LIMIT_DELAY)

        return results

    async def _fetch_from_api(self, symbol: str) -> dict[str, Any] | None:
        """Try Trendlyne's JSON API for stock data."""
        url = self.API_URL.format(symbol=symbol)
        session = await self._get_session()

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None

                content_type = resp.headers.get("Content-Type", "")
                if "json" not in content_type:
                    return None

                raw = await resp.json()
                return self._parse_api_response(raw, symbol)

        except (aiohttp.ClientError, json.JSONDecodeError):
            return None

    async def _fetch_from_html(self, symbol: str) -> dict[str, Any] | None:
        """Fallback: scrape Trendlyne HTML page."""
        url = self.BASE_URL.format(symbol=symbol)
        session = await self._get_session()

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None

                html = await resp.text()
                return self._parse_html(html, symbol)

        except aiohttp.ClientError as e:
            logger.warning("Trendlyne HTML fetch failed for %s: %s", symbol, e)
            return None

    @staticmethod
    def _parse_api_response(data: dict[str, Any], symbol: str) -> dict[str, Any] | None:
        """Parse Trendlyne API JSON response."""
        result: dict[str, Any] = {}

        # Momentum score (Trendlyne's proprietary 0-10 score)
        if "momentum_score" in data:
            result["momentum_score"] = float(data["momentum_score"])
        elif "trendlyne_score" in data:
            result["momentum_score"] = float(data["trendlyne_score"])

        # Volume breakout
        if "volume_breakout" in data:
            result["volume_breakout"] = bool(data["volume_breakout"])

        # DMA signals
        if "dma_50" in data:
            result["dma_50_signal"] = data["dma_50"]
        if "dma_200" in data:
            result["dma_200_signal"] = data["dma_200"]

        # Technical signals list
        if "signals" in data and isinstance(data["signals"], list):
            result["technical_signals"] = data["signals"]

        if not result:
            return None
        return result

    @staticmethod
    def _parse_html(html: str, symbol: str) -> dict[str, Any] | None:
        """Extract technical data from Trendlyne HTML page."""
        data: dict[str, Any] = {}

        # --- Momentum Score ---
        # Trendlyne shows a "Trendlyne Momentum Score" on the page
        momentum_match = re.search(
            r'(?:momentum|trendlyne)\s+score[^<]*?(\d+(?:\.\d+)?)\s*/\s*10',
            html, re.IGNORECASE,
        )
        if momentum_match:
            try:
                data["momentum_score"] = float(momentum_match.group(1))
            except ValueError:
                pass

        # --- Volume Breakout ---
        volume_match = re.search(
            r'volume\s+breakout', html, re.IGNORECASE
        )
        data["volume_breakout"] = volume_match is not None

        # --- DMA Signals ---
        # "Above 50 DMA" or "Below 50 DMA"
        dma50_match = re.search(
            r'(above|below)\s+50\s*(?:day\s+)?(?:DMA|SMA)', html, re.IGNORECASE
        )
        if dma50_match:
            data["dma_50_signal"] = dma50_match.group(1).lower()

        dma200_match = re.search(
            r'(above|below)\s+200\s*(?:day\s+)?(?:DMA|SMA)', html, re.IGNORECASE
        )
        if dma200_match:
            data["dma_200_signal"] = dma200_match.group(1).lower()

        # --- Technical Signals ---
        signals = []
        signal_patterns = [
            (r'(?:bullish|bearish)\s+(?:MACD|macd)\s+crossover', "macd_crossover"),
            (r'RSI\s+(?:overbought|oversold)', "rsi_signal"),
            (r'golden\s+cross', "golden_cross"),
            (r'death\s+cross', "death_cross"),
            (r'bullish\s+engulfing', "bullish_engulfing"),
            (r'bearish\s+engulfing', "bearish_engulfing"),
        ]
        for pattern, signal_name in signal_patterns:
            if re.search(pattern, html, re.IGNORECASE):
                signals.append(signal_name)

        if signals:
            data["technical_signals"] = signals

        if not data or data == {"volume_breakout": False}:
            return None

        return data
