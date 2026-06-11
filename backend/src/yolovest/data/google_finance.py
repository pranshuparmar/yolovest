"""Google Finance scraper for Indian market data and global cues.

Fetches trending NSE stocks, Indian index performance (Nifty/Sensex),
and global index context from Google Finance. Uses httpx for async HTTP.
All methods return empty results on failure — never crash the pipeline.
"""

import asyncio
import logging
import re
from typing import Any

from yolovest.http_utils import scraper_headers
from yolovest.models.schemas import NewsArticle
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)

BASE_URL = "https://www.google.com/finance"

# Primary: Indian market indices
INDIAN_INDICES = {
    "NIFTY_50": ".NSEI",
    "SENSEX": ".BSESN",
}

# Secondary: Global indices tracked for sentiment context only
# (FII flows, global risk appetite — not direct trading signals)
GLOBAL_CONTEXT_INDICES = {
    "HANG_SENG": ".HSI",
    "NIKKEI_225": ".N225",
    "S&P_500": ".INX",
    "NASDAQ": ".IXIC",
    "FTSE_100": "UKX",
}

# Rotating headers to avoid being blocked
def _headers() -> dict[str, str]:
    return scraper_headers()


class GoogleFinanceScraper:
    """Scrapes Google Finance for market sentiment data.

    Provides:
    - Market index performance (global indices)
    - Trending tickers on NSE
    - Market news headlines
    """

    def __init__(self, rate_limit_delay: float = 1.0) -> None:
        self._rate_limit_delay = rate_limit_delay
        self._session: Any = None
        self._owns_session = False

    async def _get_session(self) -> Any:
        if self._session is None:
            import httpx

            self._session = httpx.AsyncClient(
                headers=_headers(),
                follow_redirects=True,
                timeout=15.0,
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session and self._owns_session:
            await self._session.aclose()
            self._session = None

    async def _fetch_page(self, url: str) -> str | None:
        """Fetch a page and return HTML, or None on failure."""
        try:
            client = await self._get_session()
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text
            logger.warning("Google Finance returned %d for %s", resp.status_code, url)
            return None
        except Exception as e:
            logger.warning("Google Finance fetch failed for %s: %s", url, e)
            return None

    async def fetch_market_indices(self) -> dict[str, dict[str, Any]]:
        """Fetch current index values and changes.

        Returns dict mapping index name to {value, change, change_pct}.
        """
        indices: dict[str, dict[str, Any]] = {}

        html = await self._fetch_page(f"{BASE_URL}/markets")
        if not html:
            return indices

        # Parse index data — Indian indices first, then global context
        all_indices = {**INDIAN_INDICES, **GLOBAL_CONTEXT_INDICES}
        for name, ticker in all_indices.items():
            try:
                data = self._extract_index_data(html, ticker, name)
                if data:
                    indices[name] = data
            except Exception as e:
                logger.debug("Failed to parse index %s: %s", name, e)

        return indices

    def _extract_index_data(
        self, html: str, ticker: str, name: str
    ) -> dict[str, Any] | None:
        """Extract index price and change from HTML."""
        # Look for the ticker in the page content
        # Google Finance uses data attributes and aria labels
        escaped = re.escape(ticker)
        pattern = rf'{escaped}.*?(?:data-value|aria-label)[=:][\s"]*([0-9,.]+)'
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
                return {"name": name, "ticker": ticker, "value": value}
            except (ValueError, IndexError):
                pass

        # Fallback: try matching by name
        name_clean = name.replace("_", " ")
        pattern = rf'{re.escape(name_clean)}.*?([0-9,]+\.[0-9]+)'
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            try:
                value = float(match.group(1).replace(",", ""))
                return {"name": name, "ticker": ticker, "value": value}
            except (ValueError, IndexError):
                pass

        return None

    async def fetch_trending_tickers(self, market: str = "IN") -> list[dict[str, Any]]:
        """Fetch trending/most active tickers for a market.

        Args:
            market: Market region code (IN for India, US for US)

        Returns list of dicts with {symbol, name, price, change_pct}.
        """
        tickers: list[dict[str, Any]] = []

        url = f"{BASE_URL}/markets/most-active?hl=en&gl={market}"
        html = await self._fetch_page(url)
        if not html:
            return tickers

        # Extract ticker rows — Google Finance lists active stocks in a table-like structure
        # Pattern: NSE ticker symbols followed by price data
        ticker_pattern = re.compile(
            r'data-symbol="([A-Z0-9]+):NSE".*?'
            r'(?:data-value|>)\s*₹?\s*([0-9,]+\.?\d*)',
            re.DOTALL,
        )
        for match in ticker_pattern.finditer(html):
            try:
                symbol = match.group(1)
                price = float(match.group(2).replace(",", ""))
                tickers.append({"symbol": symbol, "price": price})
            except (ValueError, IndexError):
                continue

        # Fallback: simpler pattern for stock names
        if not tickers:
            simple_pattern = re.compile(
                r'/quote/([A-Z0-9]+):NSE', re.IGNORECASE
            )
            seen = set()
            for match in simple_pattern.finditer(html):
                symbol = match.group(1).upper()
                if symbol not in seen:
                    seen.add(symbol)
                    tickers.append({"symbol": symbol})

        return tickers[:20]  # cap at 20

    async def fetch_market_news(self, symbols: list[str]) -> list[NewsArticle]:
        """Fetch market news headlines from Google Finance.

        Checks the main finance page and symbol-specific pages.
        """
        articles: list[NewsArticle] = []

        # Main market news
        html = await self._fetch_page(f"{BASE_URL}/?hl=en")
        if html:
            articles.extend(self._extract_news(html, symbols))

        # Symbol-specific news (rate-limited)
        for symbol in symbols[:5]:  # limit to avoid rate limiting
            await asyncio.sleep(self._rate_limit_delay)
            url = f"{BASE_URL}/quote/{symbol}:NSE?hl=en"
            html = await self._fetch_page(url)
            if html:
                symbol_articles = self._extract_news(html, symbols)
                # Tag articles with the queried symbol
                for a in symbol_articles:
                    if symbol not in a.symbols:
                        a.symbols.append(symbol)
                articles.extend(symbol_articles)

        return articles

    def _extract_news(self, html: str, symbols: list[str]) -> list[NewsArticle]:
        """Extract news headlines from Google Finance HTML."""
        articles: list[NewsArticle] = []

        # Google Finance news sections use specific patterns
        # Look for headline text in article/news sections
        headline_patterns = [
            # Pattern 1: news article titles in divs
            re.compile(
                r'<div[^>]*class="[^"]*[Nn]ews[^"]*"[^>]*>.*?'
                r'<(?:a|div)[^>]*>([^<]{20,150})</(?:a|div)>',
                re.DOTALL,
            ),
            # Pattern 2: article titles with aria labels
            re.compile(
                r'aria-label="([^"]{20,150})"[^>]*class="[^"]*[Aa]rticle',
                re.DOTALL,
            ),
            # Pattern 3: headlines in data attributes
            re.compile(
                r'data-article-title="([^"]{20,150})"',
                re.DOTALL,
            ),
        ]

        seen_headlines: set[str] = set()
        for pattern in headline_patterns:
            for match in pattern.finditer(html):
                headline = match.group(1).strip()
                # Clean HTML entities
                headline = (
                    headline.replace("&amp;", "&")
                    .replace("&quot;", '"')
                    .replace("&#39;", "'")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                )
                # Skip if too short or already seen
                if len(headline) < 20 or headline in seen_headlines:
                    continue
                seen_headlines.add(headline)

                # Word-boundary match so ITC doesn't snag BITCOIN /
                # POLITICS. Mirror the regex used by the other news
                # scrapers.
                headline_upper = headline.upper()
                matched_symbols = [
                    s for s in symbols
                    if re.search(
                        rf"(?<![A-Z0-9]){re.escape(s.upper())}(?![A-Z0-9])",
                        headline_upper,
                    )
                ]
                articles.append(
                    NewsArticle(
                        headline=headline,
                        source="google_finance",
                        symbols=matched_symbols,
                        published_at=now_ist(),
                    )
                )

        return articles

    async def fetch_all(self, symbols: list[str]) -> dict[str, Any]:
        """Fetch all Google Finance data in one call.

        Returns a dict with indices, trending tickers, and news.
        """
        result: dict[str, Any] = {}

        try:
            indices = await self.fetch_market_indices()
            result["indices"] = indices
        except Exception as e:
            logger.warning("Google Finance indices fetch failed: %s", e)
            result["indices"] = {}

        await asyncio.sleep(self._rate_limit_delay)

        try:
            trending = await self.fetch_trending_tickers()
            result["trending_tickers"] = trending
        except Exception as e:
            logger.warning("Google Finance trending fetch failed: %s", e)
            result["trending_tickers"] = []

        await asyncio.sleep(self._rate_limit_delay)

        try:
            news = await self.fetch_market_news(symbols)
            result["news"] = news
        except Exception as e:
            logger.warning("Google Finance news fetch failed: %s", e)
            result["news"] = []

        return result

    async def health_check(self) -> bool:
        """Check if Google Finance is reachable."""
        html = await self._fetch_page(BASE_URL)
        return html is not None and len(html) > 0
