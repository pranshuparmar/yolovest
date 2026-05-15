"""NSE official data source.

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

from yolovest.http_utils import random_user_agent
from yolovest.models.schemas import NewsArticle
from yolovest.news.base import NewsSource

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.nseindia.com"
_WARMUP_PATH = "/market-data/live-equity-market"
_API_REFERER = f"{_BASE_URL}/get-quotes/equity?symbol=RELIANCE"

# Max 3 requests per second to NSE
_RATE_LIMIT_DELAY = 0.34


class NSEOfficialSource(NewsSource):
    """NSE official data source -- corporate actions, bulk deals, FII/DII data.

    Provides corporate actions (dividends, splits, bonuses), bulk/block deals,
    FII/DII activity, delivery percentages, and corporate announcements
    as news headlines.
    """

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        self._cookies_initialized = False
        self._cookies_failed = False  # True if cookie init failed — skip further attempts
        # Pin one User-Agent for the lifetime of the session — NSE ties its
        # cookies to the fingerprint of the warmup request, so swapping UA
        # between warmup and API calls triggers the bot filter.
        self._user_agent = random_user_agent()

    def _session_default_headers(self) -> dict[str, str]:
        # Only headers that are safe across navigation + XHR. Avoid baking
        # Accept / Referer / Sec-Fetch-* into session defaults — those must
        # vary per request so the homepage hit looks like a navigation and
        # API hits look like XHR.
        return {
            "User-Agent": self._user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            # Stick to gzip/deflate so we don't depend on brotli being
            # installed in the image.
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session with NSE cookies.

        Hits the NSE homepage first to obtain session cookies that are
        required for subsequent API calls.
        """
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=self._session_default_headers(),
            )
            self._owns_session = True

        if not self._cookies_initialized and not self._cookies_failed:
            await self._initialize_cookies()

        return self._session

    async def _initialize_cookies(self) -> None:
        """Warm up an NSE session by mimicking a real browser navigation.

        Real Chrome navigates: external -> www.nseindia.com -> click into
        a market-data page -> XHR to /api/*. The bot filter checks for
        consistent Sec-Fetch-* metadata + cookies acquired across at
        least two hops, so we replicate the sequence.
        """
        if self._session is None:
            return
        try:
            # Hop 1: fresh navigation to the homepage. No Referer (we're
            # arriving cold), Sec-Fetch-Site=none which is what a browser
            # sends when you type a URL or open from a bookmark.
            homepage_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }
            async with self._session.get(_BASE_URL, headers=homepage_headers) as resp:
                await resp.read()
                if resp.status != 200:
                    logger.warning(
                        "NSE homepage returned status %d — NSE data will be unavailable this session",
                        resp.status,
                    )
                    self._cookies_failed = True
                    return

            # Hop 2: same-origin navigation to a market-data page. This
            # mints the deeper cookies that /api/* actually checks.
            await asyncio.sleep(_RATE_LIMIT_DELAY)
            warmup_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Referer": f"{_BASE_URL}/",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-User": "?1",
            }
            async with self._session.get(
                f"{_BASE_URL}{_WARMUP_PATH}", headers=warmup_headers,
            ) as resp:
                await resp.read()
                if resp.status != 200:
                    logger.warning(
                        "NSE market-data warmup returned status %d — NSE data will be unavailable this session",
                        resp.status,
                    )
                    self._cookies_failed = True
                    return

            self._cookies_initialized = True
            logger.debug("NSE cookies initialized successfully")
        except Exception as e:
            logger.warning("Failed to initialize NSE cookies: %s — NSE data will be unavailable this session", e)
            self._cookies_failed = True

    async def _api_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """Make a rate-limited GET request to NSE API.

        Args:
            path: API path (e.g., "/api/corporate-announcements").
            params: Optional query parameters.

        Returns:
            Parsed JSON response, or None on failure.
        """
        # Skip all API calls if cookie initialization failed (NSE is blocking us)
        if self._cookies_failed:
            return None

        session = await self._get_session()
        url = f"{_BASE_URL}{path}"
        api_headers = {
            "Accept": "*/*",
            "Referer": _API_REFERER,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
            async with session.get(url, params=params, headers=api_headers) as resp:
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

                # Also match against provided symbols list — word-boundary
                # so ITC doesn't snag BITCOIN / POLITICS.
                if not matched_symbols:
                    import re as _re
                    headline_upper = headline.upper()
                    matched_symbols = [
                        s for s in symbols
                        if _re.search(
                            rf"(?<![A-Z0-9]){_re.escape(s.upper())}(?![A-Z0-9])",
                            headline_upper,
                        )
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
        """Check if NSE API is accessible.

        Reuses the same warmup the session does on first use. If the
        cookie handshake already failed, we don't re-try here — that
        flag persists for the lifetime of the session.
        """
        if self._cookies_failed:
            return False
        try:
            await self._get_session()
            return self._cookies_initialized
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Extended methods
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
        """Normalize a bulk/block deal entry to a consistent dict.

        NSE has shipped at least two different field naming
        conventions for this endpoint over time:
          - lowercase camel: symbol / clientName / buySell / quantity / tradePrice
          - prefixed upper:   BD_SYMBOL / BD_CLIENT_NAME / BD_BUY_SELL /
                              BD_QTY_TRD / BD_TP_WATP
        Try the candidates in order and use the first non-empty hit.
        Without this, a schema change silently fills the table with
        rows that have only a symbol and "block"/"bulk" type, every
        other field empty.
        """
        def _first(*keys: str, default: Any = "") -> Any:
            for k in keys:
                v = item.get(k)
                if v not in (None, ""):
                    return v
            return default

        return {
            "symbol": str(_first("symbol", "BD_SYMBOL", "tradingSymbol", default="")),
            "deal_type": deal_type,
            "client_name": str(_first("clientName", "BD_CLIENT_NAME", default="")),
            "buy_sell": str(_first("buySell", "BD_BUY_SELL", default="")),
            "quantity": _first("quantity", "qty", "BD_QTY_TRD", default=None),
            "trade_price": _first(
                "tradePrice", "weightedAvgPrice", "BD_TP_WATP", default=None,
            ),
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
