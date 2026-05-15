"""LiveMint RSS news scraper."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from yolovest.models.schemas import NewsArticle
from yolovest.news.base import NewsSource

logger = logging.getLogger(__name__)

RSS_URL = "https://www.livemint.com/rss/markets"


def _parse_rss(url: str) -> dict[str, Any]:
    """Parse RSS feed using feedparser (blocking call)."""
    import feedparser

    result: dict[str, Any] = feedparser.parse(url)
    return result


class LiveMintSource(NewsSource):
    """Fetches headlines from LiveMint Markets RSS feed."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(2)

    async def fetch_headlines(self, symbols: list[str]) -> list[NewsArticle]:
        """Parse LiveMint RSS and match symbols to headlines."""
        try:
            async with self._semaphore:
                feed = await asyncio.to_thread(_parse_rss, RSS_URL)
            return self._parse_feed(feed, symbols)
        except Exception:
            logger.warning("LiveMint fetch failed", exc_info=True)
            return []

    def _parse_feed(
        self, feed: dict[str, Any], symbols: list[str]
    ) -> list[NewsArticle]:
        """Convert feedparser entries to NewsArticle list."""
        articles: list[NewsArticle] = []
        for entry in feed.get("entries", []):
            headline = entry.get("title", "").strip()
            if not headline:
                continue
            url = entry.get("link")
            published_at = self._parse_date(entry)
            matched = _match_symbols(headline, symbols)
            articles.append(
                NewsArticle(
                    headline=headline,
                    source="livemint",
                    url=url,
                    symbols=matched,
                    published_at=published_at,
                )
            )
        return articles

    @staticmethod
    def _parse_date(entry: dict[str, Any]) -> datetime | None:
        """Extract published date from feedparser entry."""
        parsed = entry.get("published_parsed")
        if parsed:
            try:
                return datetime(*parsed[:6])
            except (TypeError, ValueError):
                pass
        return None

    async def health_check(self) -> bool:
        """Check if the RSS feed is reachable."""
        try:
            async with self._semaphore:
                feed = await asyncio.to_thread(_parse_rss, RSS_URL)
            return bool(feed.get("entries"))
        except Exception:
            return False


import re as _re


def _match_symbols(headline: str, symbols: list[str]) -> list[str]:
    """Return symbols whose names appear in the headline as standalone
    words (case-insensitive). Word-boundary matching prevents false
    positives like ITC matching inside BITCOIN, or ITC inside POLITICS.
    """
    headline_upper = headline.upper()
    matched: list[str] = []
    for s in symbols:
        if _re.search(rf"(?<![A-Z0-9]){_re.escape(s.upper())}(?![A-Z0-9])", headline_upper):
            matched.append(s)
    return matched
