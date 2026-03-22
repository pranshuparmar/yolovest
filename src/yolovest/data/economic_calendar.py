"""Economic calendar ingestion for RBI/Fed/earnings dates (FR-2.6).

Ingests macroeconomic events (RBI policy, US Fed, GDP, earnings dates) from:
1. RBI calendar — scraped from RBI website RSS/announcements
2. US Fed — FOMC meeting dates (known schedule, updated yearly)
3. Earnings dates — from NSE corporate filings / moneycontrol

Events are stored in the economic_events table and consumed by:
- Pre-market skill (macro context)
- LLM trade review (event-aware decisions)
- Risk check (reduce sizing around high-impact events)
"""

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Known FOMC meeting dates are published yearly. This is a static fallback.
# Updated annually or fetched dynamically.
_FOMC_2026 = [
    "2026-01-27", "2026-01-28",
    "2026-03-17", "2026-03-18",
    "2026-04-28", "2026-04-29",
    "2026-06-16", "2026-06-17",
    "2026-07-28", "2026-07-29",
    "2026-09-15", "2026-09-16",
    "2026-10-27", "2026-10-28",
    "2026-12-15", "2026-12-16",
]


class EconomicCalendarSource:
    """Fetches economic calendar events from multiple sources."""

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def fetch_all_events(
        self, lookback_days: int = 7, lookahead_days: int = 30
    ) -> list[dict[str, Any]]:
        """Fetch events from all sources within the date window.

        Returns list of dicts with keys:
            event_date, event_type, title, country, impact, source, content_hash
        """
        events: list[dict[str, Any]] = []

        # Fetch from each source, catching failures individually
        fetchers = [
            ("rbi", self._fetch_rbi_events),
            ("fed", self._fetch_fed_events),
            ("earnings", self._fetch_earnings_dates),
        ]

        for source_name, fetcher in fetchers:
            try:
                source_events = await fetcher(lookback_days, lookahead_days)
                events.extend(source_events)
            except Exception as e:
                logger.warning("Economic calendar fetch failed for %s: %s", source_name, e)

        return events

    async def _fetch_rbi_events(
        self, lookback_days: int, lookahead_days: int
    ) -> list[dict[str, Any]]:
        """Fetch RBI monetary policy and key announcement dates.

        Uses RBI's public press release RSS feed as primary source.
        Falls back to known scheduled dates if RSS is unavailable.
        """
        events: list[dict[str, Any]] = []
        today = date.today()
        window_start = today - timedelta(days=lookback_days)
        window_end = today + timedelta(days=lookahead_days)

        try:
            session = await self._get_session()
            async with session.get(
                "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx",
                headers={"User-Agent": "YoloVest/1.0"},
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    events.extend(
                        self._parse_rbi_announcements(text, window_start, window_end)
                    )
        except Exception as e:
            logger.debug("RBI RSS fetch failed, using scheduled dates: %s", e)

        # Always add known RBI MPC scheduled dates (published in advance)
        # RBI MPC typically meets 6 times a year, dates announced beforehand
        rbi_mpc_2026 = [
            "2026-02-05", "2026-02-07",  # Feb MPC
            "2026-04-07", "2026-04-09",  # Apr MPC
            "2026-06-04", "2026-06-06",  # Jun MPC
            "2026-08-05", "2026-08-07",  # Aug MPC
            "2026-09-29", "2026-10-01",  # Oct MPC
            "2026-12-03", "2026-12-05",  # Dec MPC
        ]

        for date_str in rbi_mpc_2026:
            event_date = date.fromisoformat(date_str)
            if window_start <= event_date <= window_end:
                events.append(self._make_event(
                    event_date=date_str,
                    event_type="monetary_policy",
                    title="RBI MPC Meeting",
                    country="IN",
                    impact="high",
                    source="rbi_schedule",
                ))

        return self._deduplicate(events)

    async def _fetch_fed_events(
        self, lookback_days: int, lookahead_days: int
    ) -> list[dict[str, Any]]:
        """Fetch US Federal Reserve FOMC meeting dates.

        Uses known FOMC schedule (published yearly by the Fed).
        """
        events: list[dict[str, Any]] = []
        today = date.today()
        window_start = today - timedelta(days=lookback_days)
        window_end = today + timedelta(days=lookahead_days)

        for date_str in _FOMC_2026:
            event_date = date.fromisoformat(date_str)
            if window_start <= event_date <= window_end:
                events.append(self._make_event(
                    event_date=date_str,
                    event_type="monetary_policy",
                    title="US Fed FOMC Meeting",
                    country="US",
                    impact="high",
                    source="fed_schedule",
                ))

        return events

    async def _fetch_earnings_dates(
        self, lookback_days: int, lookahead_days: int
    ) -> list[dict[str, Any]]:
        """Fetch upcoming corporate earnings dates from NSE filings.

        Scrapes NSE corporate announcements for board meeting / results dates.
        Falls back gracefully if NSE is unreachable.
        """
        events: list[dict[str, Any]] = []
        today = date.today()
        window_end = today + timedelta(days=lookahead_days)

        try:
            session = await self._get_session()
            # NSE corporate announcements API
            url = (
                "https://www.nseindia.com/api/corporate-announcements"
                f"?index=equities&from_date={today.strftime('%d-%m-%Y')}"
                f"&to_date={window_end.strftime('%d-%m-%Y')}"
            )
            async with session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data if isinstance(data, list) else []:
                        subject = (item.get("subject") or "").lower()
                        if "board meeting" in subject or "financial result" in subject:
                            symbol = item.get("symbol", "")
                            bm_date = item.get("bm_date") or item.get("an_dt", "")
                            if bm_date and symbol:
                                parsed_date = self._try_parse_date(bm_date)
                                if parsed_date:
                                    events.append(self._make_event(
                                        event_date=parsed_date.isoformat(),
                                        event_type="earnings",
                                        title=f"{symbol} Board Meeting / Results",
                                        country="IN",
                                        impact="medium",
                                        source="nse_announcements",
                                        symbol=symbol,
                                    ))
        except Exception as e:
            logger.debug("NSE earnings fetch failed: %s", e)

        return events

    @staticmethod
    def _make_event(
        event_date: str,
        event_type: str,
        title: str,
        country: str,
        impact: str,
        source: str,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Create a normalized event dict with content hash for dedup."""
        content = f"{event_date}:{event_type}:{title}:{country}"
        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()
        event: dict[str, Any] = {
            "event_date": event_date,
            "event_type": event_type,
            "title": title,
            "country": country,
            "impact": impact,
            "source": source,
            "content_hash": content_hash,
        }
        if symbol:
            event["symbol"] = symbol
        return event

    @staticmethod
    def _deduplicate(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate events by content_hash."""
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for e in events:
            h = e["content_hash"]
            if h not in seen:
                seen.add(h)
                result.append(e)
        return result

    @staticmethod
    def _parse_rbi_announcements(
        html: str, window_start: date, window_end: date
    ) -> list[dict[str, Any]]:
        """Extract RBI press release dates from HTML page.

        Simple keyword-based extraction — looks for monetary policy related items.
        """
        events: list[dict[str, Any]] = []
        keywords = ["monetary policy", "policy rate", "repo rate", "mpc", "credit policy"]

        # Basic extraction: find date-like patterns near keywords
        import re

        # Match patterns like "February 07, 2026" or "07-02-2026"
        date_patterns = [
            (r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
             r"September|October|November|December)\s+(\d{4})", "%d %B %Y"),
            (r"(\d{2}-\d{2}-\d{4})", "%d-%m-%Y"),
        ]

        lower_html = html.lower()
        for keyword in keywords:
            if keyword not in lower_html:
                continue

            for pattern, date_fmt in date_patterns:
                for match in re.finditer(pattern, html, re.IGNORECASE):
                    try:
                        date_str = match.group(0)
                        parsed = datetime.strptime(date_str, date_fmt).date()
                        if window_start <= parsed <= window_end:
                            events.append(EconomicCalendarSource._make_event(
                                event_date=parsed.isoformat(),
                                event_type="monetary_policy",
                                title=f"RBI Announcement: {keyword.title()}",
                                country="IN",
                                impact="high",
                                source="rbi_website",
                            ))
                    except ValueError:
                        continue

        return events

    @staticmethod
    def _try_parse_date(value: str) -> date | None:
        """Try multiple date formats."""
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
        return None
