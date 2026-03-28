"""Economic calendar ingestion for Indian market events.

Ingests macroeconomic events relevant to Indian stock markets from:
1. RBI MPC — scraped from RBI website RSS/announcements (primary)
2. NSE corporate earnings — board meetings, results dates
3. US Fed FOMC — secondary context only (impacts FII flows into India)

Events are stored in the economic_events table and consumed by:
- Pre-market skill (macro context)
- LLM trade review (event-aware decisions)
- Risk check (reduce sizing around high-impact events)
"""

import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from yolovest.http_utils import scraper_headers

logger = logging.getLogger(__name__)

# Static FOMC schedules by year. Used as fallback when web scraping fails.
# Updated when new schedules are published by the Fed.
_FOMC_SCHEDULES: dict[int, list[str]] = {
    2025: [
        "2025-01-28", "2025-01-29",
        "2025-03-18", "2025-03-19",
        "2025-05-06", "2025-05-07",
        "2025-06-17", "2025-06-18",
        "2025-07-29", "2025-07-30",
        "2025-09-16", "2025-09-17",
        "2025-10-28", "2025-10-29",
        "2025-12-09", "2025-12-10",
    ],
    2026: [
        "2026-01-27", "2026-01-28",
        "2026-03-17", "2026-03-18",
        "2026-04-28", "2026-04-29",
        "2026-06-16", "2026-06-17",
        "2026-07-28", "2026-07-29",
        "2026-09-15", "2026-09-16",
        "2026-10-27", "2026-10-28",
        "2026-12-15", "2026-12-16",
    ],
}

# Static RBI MPC schedules by year. Fallback when RBI website is unreachable.
_RBI_MPC_SCHEDULES: dict[int, list[str]] = {
    2025: [
        "2025-02-05", "2025-02-07",
        "2025-04-07", "2025-04-09",
        "2025-06-04", "2025-06-06",
        "2025-08-06", "2025-08-08",
        "2025-09-29", "2025-10-01",
        "2025-12-03", "2025-12-05",
    ],
    2026: [
        "2026-02-05", "2026-02-07",
        "2026-04-07", "2026-04-09",
        "2026-06-04", "2026-06-06",
        "2026-08-05", "2026-08-07",
        "2026-09-29", "2026-10-01",
        "2026-12-03", "2026-12-05",
    ],
}


def _get_fomc_dates(year: int) -> list[str]:
    """Get FOMC meeting dates for a year. Falls back to empty if unknown."""
    return _FOMC_SCHEDULES.get(year, [])


def _get_rbi_mpc_dates(year: int) -> list[str]:
    """Get RBI MPC meeting dates for a year. Falls back to empty if unknown."""
    return _RBI_MPC_SCHEDULES.get(year, [])


class EconomicCalendarSource:
    """Fetches economic calendar events from multiple sources.

    Uses dynamic year handling: scrapes official websites for current dates,
    falls back to static schedule tables when scraping fails.
    """

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        self._fomc_cache: dict[int, list[str]] = {}
        self._rbi_cache: dict[int, list[str]] = {}

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

        Tries two dynamic sources before falling back to static schedules:
        1. RBI MPC schedule page (dedicated calendar)
        2. RBI press releases (keyword-based extraction)
        3. Static _RBI_MPC_SCHEDULES fallback
        """
        events: list[dict[str, Any]] = []
        today = date.today()
        window_start = today - timedelta(days=lookback_days)
        window_end = today + timedelta(days=lookahead_days)
        years = {window_start.year, window_end.year}

        # Try scraping RBI MPC schedule page for authoritative dates
        scraped_dates: list[str] = []
        try:
            scraped_dates = await self._scrape_rbi_mpc_schedule()
            if scraped_dates:
                logger.debug("RBI MPC dates scraped: %d dates", len(scraped_dates))
                for date_str in scraped_dates:
                    event_date = date.fromisoformat(date_str)
                    if window_start <= event_date <= window_end:
                        events.append(self._make_event(
                            event_date=date_str,
                            event_type="monetary_policy",
                            title="RBI MPC Meeting",
                            country="IN",
                            impact="high",
                            source="rbi_website",
                        ))
        except Exception as e:
            logger.debug("RBI MPC schedule scrape failed: %s", e)

        # Also try press releases for additional announcements
        try:
            session = await self._get_session()
            async with session.get(
                "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx",
                headers=scraper_headers(),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    events.extend(
                        self._parse_rbi_announcements(text, window_start, window_end)
                    )
        except Exception as e:
            logger.debug("RBI press release fetch failed: %s", e)

        # Fall back to static schedule if no dynamic dates found
        if not scraped_dates:
            for year in years:
                rbi_dates = _get_rbi_mpc_dates(year)
                if not rbi_dates:
                    logger.info(
                        "No RBI MPC schedule for %d — scrape failed and no static "
                        "dates available. Update _RBI_MPC_SCHEDULES when published.",
                        year,
                    )
                for date_str in rbi_dates:
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

    async def _scrape_rbi_mpc_schedule(self) -> list[str]:
        """Scrape RBI MPC meeting dates from the RBI website.

        Targets the RBI's monetary policy page which lists upcoming
        and past MPC meeting dates.
        """
        session = await self._get_session()
        # RBI publishes MPC schedule on this page
        url = "https://www.rbi.org.in/Scripts/BS_MonetaryPolicyCalendar.aspx"
        async with session.get(
            url, headers=scraper_headers(),
        ) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        dates: list[str] = []
        current_year = date.today().year

        # RBI page has dates like "February 5 to 7, 2026" or "April 7-9, 2025"
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }

        # Pattern: "Month DD to DD, YYYY" or "Month DD-DD, YYYY"
        pattern = (
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+"
            r"(\d{1,2})\s*(?:to|[-–])\s*(\d{1,2})\s*,?\s*(\d{4})"
        )
        for match in re.finditer(pattern, html, re.IGNORECASE):
            month_name = match.group(1).lower()
            day_start = int(match.group(2))
            day_end = int(match.group(3))
            year = int(match.group(4))

            if year < current_year - 1 or year > current_year + 1:
                continue

            month = months.get(month_name)
            if not month:
                continue

            try:
                dates.append(date(year, month, day_start).isoformat())
                dates.append(date(year, month, day_end).isoformat())
            except ValueError:
                continue

        return sorted(set(dates))

    async def _fetch_fed_events(
        self, lookback_days: int, lookahead_days: int
    ) -> list[dict[str, Any]]:
        """Fetch US FOMC meeting dates as secondary context for Indian markets.

        FOMC decisions affect FII flows into India, so they are tracked as
        medium-impact context events (not primary like RBI MPC).

        Tries scraping the Fed website first, falls back to static schedules.
        """
        events: list[dict[str, Any]] = []
        today = date.today()
        window_start = today - timedelta(days=lookback_days)
        window_end = today + timedelta(days=lookahead_days)

        # Try scraping Federal Reserve website for dynamic dates
        fomc_dates: list[str] = []
        try:
            scraped = await self._scrape_fomc_dates()
            if scraped:
                fomc_dates = scraped
                logger.debug("FOMC dates scraped from Fed website: %d dates", len(scraped))
        except Exception as e:
            logger.debug("FOMC scrape failed, using static schedule: %s", e)

        # Fall back to static schedules if scraping yielded nothing
        if not fomc_dates:
            years = {window_start.year, window_end.year}
            for year in years:
                year_dates = _get_fomc_dates(year)
                if not year_dates:
                    logger.info(
                        "No FOMC schedule for %d — update _FOMC_SCHEDULES "
                        "or ensure Fed website is reachable", year
                    )
                fomc_dates.extend(year_dates)

        for date_str in fomc_dates:
            event_date = date.fromisoformat(date_str)
            if window_start <= event_date <= window_end:
                events.append(self._make_event(
                    event_date=date_str,
                    event_type="global_monetary_policy",
                    title="US Fed FOMC Meeting (global context)",
                    country="US",
                    impact="medium",  # medium for India, not high
                    source="fed_schedule",
                ))

        return events

    async def _scrape_fomc_dates(self) -> list[str]:
        """Scrape FOMC meeting dates from the Federal Reserve website.

        Parses the Fed's calendar page for meeting dates in YYYY-MM-DD format.
        Returns dates for the current and next year.
        """
        session = await self._get_session()
        url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
        async with session.get(
            url, headers=scraper_headers(),
        ) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        return self._parse_fomc_html(html)

    @staticmethod
    def _parse_fomc_html(html: str) -> list[str]:
        """Extract FOMC meeting dates from the Fed calendar HTML.

        The Fed page has meeting dates in various formats. We look for
        patterns like 'January 28-29' within year-contextualized sections.
        """
        dates: list[str] = []
        current_year = date.today().year

        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }

        # Find year markers in the HTML
        for year in range(current_year, current_year + 2):
            # Match patterns like "January 28-29" or "March 18-19*"
            pattern = (
                r"(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+(\d{1,2})(?:\s*[-/]\s*(\d{1,2}))?"
            )
            for match in re.finditer(pattern, html, re.IGNORECASE):
                month_name = match.group(1).lower()
                day1 = int(match.group(2))
                day2 = int(match.group(3)) if match.group(3) else day1

                month = months.get(month_name)
                if not month:
                    continue

                try:
                    d1 = date(year, month, day1)
                    dates.append(d1.isoformat())
                    if day2 != day1:
                        d2 = date(year, month, day2)
                        dates.append(d2.isoformat())
                except ValueError:
                    continue

        # Deduplicate and sort
        return sorted(set(dates))

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
                headers=scraper_headers({"Accept": "application/json"}),
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
