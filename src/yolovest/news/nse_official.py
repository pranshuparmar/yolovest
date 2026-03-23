"""NSE official data source (FR-2.2).

Fetches corporate actions, bulk/block deals, FII/DII activity, delivery data,
and corporate announcements from NSE India's JSON APIs.

NSE uses anti-scraping measures (cookie-based). The approach:
1. Hit the homepage to obtain session cookies.
2. Use those cookies for subsequent API calls.
3. Rate-limit to max 3 req/s.
4. Graceful fallback: return empty results on failure (never crash).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp

from yolovest.models.schemas import NewsArticle
from yolovest.news.base import NewsSource

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.nseindia.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

# Max 3 requests per second to NSE
_RATE_LIMIT_DELAY = 0.34


class NSEOfficialSource(NewsSource):
    """NSE official data source -- corporate actions, bulk deals, FII/DII data.

    Implements the full FR-2.2 spec: corporate actions (dividends, splits,
    bonuses), bulk/block deals, FII/DII activity, delivery percentages,
    and corporate announcements as news headlines.
    """

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        self._cookies_initialized = False

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session with NSE cookies.

        Hits the NSE homepage first to obtain session cookies that are
        required for subsequent API calls.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=_HEADERS,
            )
            self._owns_session = True

        if not self._cookies_initialized:
            await self._initialize_cookies()

        return self._session

    async def _initialize_cookies(self) -> None:
        """Hit NSE homepage to obtain session cookies."""
        if self._session is None:
            return
        try:
            async with self._session.get(
                _BASE_URL,
                headers={**_HEADERS, "Accept": "text/html"},
            ) as resp:
                # We just need the cookies from the response, don't need body
                await resp.read()
                if resp.status == 200:
                    self._cookies_initialized = True
                    logger.debug("NSE cookies initialized successfully")
                else:
                    logger.warning("NSE homepage returned status %d", resp.status)
        except Exception as e:
            logger.warning("Failed to initialize NSE cookies: %s", e)

    async def _api_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """Make a rate-limited GET request to NSE API.

        Args:
            path: API path (e.g., "/api/corporate-announcements").
            params: Optional query parameters.

        Returns:
            Parsed JSON response, or None on failure.
        """
        session = await self._get_session()
        url = f"{_BASE_URL}{path}"

        try:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("NSE API %s returned status %d", path, resp.status)
                    return None
                return await resp.json(content_type=None)
        except TimeoutError:
            logger.warning("NSE API %s timed out", path)
            return None
        except aiohttp.ClientError as e:
            logger.warning("NSE API %s client error: %s", path, e)
            return None
        except Exception as e:
            logger.warning("NSE API %s unexpected error: %s", path, e)
            return None

    # ------------------------------------------------------------------
    # NewsSource interface
    # ------------------------------------------------------------------

    async def fetch_headlines(self, symbols: list[str]) -> list[NewsArticle]:
        """Fetch corporate announcements from NSE as news headlines.

        Queries the corporate-announcements API and converts each
        announcement into a NewsArticle with source="nse".
        """
        articles: list[NewsArticle] = []

        try:
            data = await self._api_get(
                "/api/corporate-announcements",
                params={"index": "equities"},
            )
            if not data or not isinstance(data, list):
                return articles

            for item in data:
                headline = self._extract_announcement_headline(item)
                if not headline:
                    continue

                symbol = str(item.get("symbol", "")).strip()
                matched_symbols = [symbol] if symbol else []

                # Also match against provided symbols list
                if not matched_symbols:
                    headline_upper = headline.upper()
                    matched_symbols = [
                        s for s in symbols if s.upper() in headline_upper
                    ]

                published = self._parse_nse_date(item.get("an_dt"))

                articles.append(
                    NewsArticle(
                        headline=headline,
                        source="nse",
                        url=f"{_BASE_URL}/companies-listing/corporate-filings"
                             f"-announcements",
                        symbols=matched_symbols,
                        published_at=published,
                    )
                )
        except Exception:
            logger.warning("NSE fetch_headlines failed", exc_info=True)

        return articles

    async def health_check(self) -> bool:
        """Check if NSE API is accessible by hitting the homepage."""
        try:
            session = await self._get_session()
            async with session.get(
                _BASE_URL,
                headers={**_HEADERS, "Accept": "text/html"},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # FR-2.2 extended methods
    # ------------------------------------------------------------------

    async def fetch_corp_actions(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch corporate actions (dividends, splits, bonuses) for a symbol.

        Args:
            symbol: NSE symbol (e.g., "RELIANCE").

        Returns:
            List of corporate action dicts with keys: subject, exDate,
            bcStartDate, bcEndDate, symbol, series, faceVal, etc.
        """
        data = await self._api_get(
            "/api/corporates/corporateActions",
            params={"index": "equities", "symbol": symbol},
        )
        if not data or not isinstance(data, list):
            return []

        actions: list[dict[str, Any]] = []
        for item in data:
            action: dict[str, Any] = {
                "symbol": str(item.get("symbol", symbol)),
                "subject": str(item.get("subject", "")),
                "ex_date": str(item.get("exDate", "")),
                "record_date": str(item.get("recDate", "")),
                "series": str(item.get("series", "EQ")),
                "face_value": item.get("faceVal"),
            }
            # Classify action type
            subject_lower = action["subject"].lower()
            if "dividend" in subject_lower:
                action["action_type"] = "dividend"
            elif "split" in subject_lower:
                action["action_type"] = "split"
            elif "bonus" in subject_lower:
                action["action_type"] = "bonus"
            else:
                action["action_type"] = "other"

            actions.append(action)

        return actions

    async def fetch_bulk_deals(self) -> list[dict[str, Any]]:
        """Fetch today's bulk and block deals from NSE.

        Returns:
            List of deal dicts with keys: symbol, dealType, clientName,
            quantity, tradePrice.
        """
        deals: list[dict[str, Any]] = []

        # Fetch block deals
        block_data = await self._api_get("/api/block-deal")
        if block_data and isinstance(block_data, dict):
            for item in block_data.get("data", []):
                deals.append(self._normalize_deal(item, "block"))

        # Fetch bulk deals
        await asyncio.sleep(_RATE_LIMIT_DELAY)
        bulk_data = await self._api_get("/api/bulk-deal")
        if bulk_data and isinstance(bulk_data, dict):
            for item in bulk_data.get("data", []):
                deals.append(self._normalize_deal(item, "bulk"))

        return deals

    async def fetch_fii_dii(self) -> dict[str, Any]:
        """Fetch FII/DII buy/sell activity data for the day.

        Returns:
            Dict with keys: date, fii_buy, fii_sell, fii_net, dii_buy,
            dii_sell, dii_net. All values in crores. Returns empty dict
            on failure.
        """
        data = await self._api_get("/api/fiidiiTradeReact")
        if not data or not isinstance(data, list) or len(data) == 0:
            return {}

        result: dict[str, Any] = {"date": "", "fii": {}, "dii": {}}

        for entry in data:
            category = str(entry.get("category", "")).upper()
            if "FII" in category or "FPI" in category:
                result["fii"] = {
                    "buy_value": self._safe_float(entry.get("buyValue")),
                    "sell_value": self._safe_float(entry.get("sellValue")),
                    "net_value": self._safe_float(entry.get("netValue")),
                }
                result["date"] = str(entry.get("date", ""))
            elif "DII" in category:
                result["dii"] = {
                    "buy_value": self._safe_float(entry.get("buyValue")),
                    "sell_value": self._safe_float(entry.get("sellValue")),
                    "net_value": self._safe_float(entry.get("netValue")),
                }

        return result

    async def fetch_delivery_data(self, symbol: str) -> float | None:
        """Fetch delivery percentage for a symbol.

        Uses the quote-equity API and extracts deliveryToTradedQuantity.

        Args:
            symbol: NSE symbol (e.g., "RELIANCE").

        Returns:
            Delivery percentage as float (0-100 scale), or None if unavailable.
        """
        data = await self._api_get(
            "/api/quote-equity",
            params={"symbol": symbol},
        )
        if not data or not isinstance(data, dict):
            return None

        # Try securityWiseDP -> deliveryToTradedQuantity
        sec_dp = data.get("securityWiseDP")
        if isinstance(sec_dp, dict):
            delivery_pct = sec_dp.get("deliveryToTradedQuantity")
            if delivery_pct is not None:
                return self._safe_float(delivery_pct)

        # Fallback: preOpenMarket or other sections
        pre_open = data.get("preOpenMarket")
        if isinstance(pre_open, dict):
            delivery_pct = pre_open.get("deliveryToTradedQuantity")
            if delivery_pct is not None:
                return self._safe_float(delivery_pct)

        return None

    async def close(self) -> None:
        """Close the aiohttp session if we own it."""
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None
            self._cookies_initialized = False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_announcement_headline(item: dict[str, Any]) -> str:
        """Build a headline string from a corporate announcement item."""
        symbol = str(item.get("symbol", "")).strip()
        subject = str(item.get("subject", "")).strip()
        desc = str(item.get("desc", "")).strip()

        if subject and symbol:
            return f"{symbol}: {subject}"
        if desc and symbol:
            return f"{symbol}: {desc}"
        if subject:
            return subject
        return desc

    @staticmethod
    def _parse_nse_date(value: Any) -> datetime | None:
        """Parse NSE date strings (multiple formats).

        NSE uses formats like "22-Mar-2026", "22-03-2026", "2026-03-22".
        """
        if not value or not isinstance(value, str):
            return None

        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y %H:%M"):
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _normalize_deal(item: dict[str, Any], deal_type: str) -> dict[str, Any]:
        """Normalize a bulk/block deal entry to a consistent dict."""
        return {
            "symbol": str(item.get("symbol", "")),
            "deal_type": deal_type,
            "client_name": str(item.get("clientName", "")),
            "buy_sell": str(item.get("buySell", "")),
            "quantity": item.get("quantity") or item.get("qty"),
            "trade_price": item.get("tradePrice")
            or item.get("weightedAvgPrice"),
        }

    @staticmethod
    def _safe_float(value: Any) -> float:
        """Safely convert a value to float, returning 0.0 on failure."""
        if value is None:
            return 0.0
        try:
            # Handle strings with commas like "1,234.56"
            if isinstance(value, str):
                value = value.replace(",", "")
            return float(value)
        except (ValueError, TypeError):
            return 0.0
