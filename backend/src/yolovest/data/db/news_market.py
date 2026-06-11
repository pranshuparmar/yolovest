"""Premarket snapshots, sentiment, news articles, economic calendar.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import json
import logging
from datetime import timedelta
from typing import Any

from yolovest.models.schemas import NewsArticle, SentimentResult
from yolovest.timezone import now_ist, now_utc

logger = logging.getLogger(__name__)


class NewsMarketMixin:
    # Pre-market Data
    # ------------------------------------------------------------------

    async def upsert_premarket(self, data: dict[str, Any]) -> None:
        """Insert or update today's pre-market context."""
        from datetime import date

        today = date.today().isoformat()
        gift = data.get("gift_nifty", {})
        us = data.get("us_markets", {})
        llm = data.get("llm_summary")
        # Extract bias from LLM summary if it's a WebGroundingResult-like object
        bias = None
        summary_text = None
        if llm is not None and hasattr(llm, "summary"):
            summary_text = llm.summary
        elif isinstance(llm, dict):
            summary_text = llm.get("summary")
            bias = llm.get("bias")
        elif isinstance(llm, str):
            summary_text = llm

        await self.conn.execute(
            "INSERT INTO premarket (date, gift_nifty_change_pct, us_sp500_change_pct, "
            "market_bias, llm_summary, created_at) VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(date) DO UPDATE SET gift_nifty_change_pct=excluded.gift_nifty_change_pct, "
            "us_sp500_change_pct=excluded.us_sp500_change_pct, market_bias=excluded.market_bias, "
            "llm_summary=excluded.llm_summary",
            (
                today,
                gift.get("change_pct"),
                us.get("sp500_change_pct"),
                bias,
                summary_text,
            ),
        )
        await self.conn.commit()

    async def get_latest_premarket(self) -> dict[str, Any]:
        """Get the most recent pre-market context."""
        cursor = await self.conn.execute(
            "SELECT * FROM premarket ORDER BY date DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else {}

    # ------------------------------------------------------------------
    # Sentiment
    # ------------------------------------------------------------------

    async def upsert_sentiment(self, symbol: str, result: SentimentResult) -> None:
        """Insert or update sentiment for a symbol."""
        await self.conn.execute(
            "INSERT INTO sentiment (symbol, sentiment, confidence, key_drivers, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(symbol) DO UPDATE SET sentiment=excluded.sentiment, "
            "confidence=excluded.confidence, key_drivers=excluded.key_drivers, "
            "created_at=excluded.created_at",
            (
                symbol,
                result.sentiment,
                result.confidence,
                json.dumps(result.key_drivers),
            ),
        )
        await self.conn.commit()

    async def get_sentiment(
        self, symbol: str, max_age_hours: int = 48,
    ) -> SentimentResult | None:
        """Get latest sentiment for a symbol. Returns None if older than max_age_hours."""
        cutoff = (now_utc() - timedelta(hours=max_age_hours)).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT symbol, sentiment, confidence, key_drivers "
            "FROM sentiment WHERE symbol = ? AND created_at >= ?",
            (symbol, cutoff),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        drivers = json.loads(row["key_drivers"]) if row["key_drivers"] else []
        return SentimentResult(
            symbol=row["symbol"],
            sentiment=row["sentiment"],
            confidence=row["confidence"],
            key_drivers=drivers,
        )

    # ------------------------------------------------------------------
    # News Articles
    # ------------------------------------------------------------------

    async def upsert_news_articles(self, articles: list[NewsArticle]) -> int:
        """Insert news articles, skipping duplicates. Returns count inserted."""
        inserted = 0
        for article in articles:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO news_articles "
                    "(content_hash, headline, source, url, symbols, published_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        article.content_hash,
                        article.headline,
                        article.source,
                        article.url,
                        json.dumps(article.symbols),
                        article.published_at.isoformat() if article.published_at else None,
                    ),
                )
                inserted += 1
            except Exception:
                logger.debug("Skipped duplicate news article", exc_info=True)
        await self.conn.commit()
        return inserted

    async def get_news_articles(
        self,
        symbol: str | None = None,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Retrieve recent news articles with optional filters."""
        query = (
            "SELECT content_hash, headline, source, url, symbols, published_at "
            "FROM news_articles WHERE 1=1"
        )
        params: list[Any] = []
        if symbol:
            # Match the symbol as a standalone JSON-string element so
            # the filter for "ITC" doesn't also return rows tagged
            # ["BITCOIN"]. symbols is stored as `["ITC", ...]` so we
            # search for the quoted form.
            query += " AND symbols LIKE ?"
            params.append(f'%"{symbol}"%')
        if source:
            query += " AND source = ?"
            params.append(source)
        if date_from:
            # ISO 8601 strings are lexicographically sortable, so string
            # comparison with 'YYYY-MM-DD' works correctly.
            query += " AND published_at >= ?"
            params.append(date_from)
        if date_to:
            query += " AND published_at < ?"
            params.append(date_to)
        query += " ORDER BY published_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        results = []
        for r in rows:
            symbols_raw = r[4]
            try:
                symbols_parsed = json.loads(symbols_raw) if symbols_raw else []
            except (json.JSONDecodeError, TypeError):
                symbols_parsed = []
            results.append({
                "content_hash": r[0],
                "headline": r[1],
                "source": r[2],
                "url": r[3],
                "symbols": symbols_parsed,
                "published_at": r[5],
            })
        return results

    # ------------------------------------------------------------------
    # Economic Calendar
    # ------------------------------------------------------------------

    async def upsert_economic_events(self, events: list[dict[str, Any]]) -> int:
        """Insert economic calendar events, skipping duplicates. Returns count inserted."""
        inserted = 0
        for event in events:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO economic_events "
                    "(event_date, event_type, title, country, impact, source, symbol, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["event_date"],
                        event["event_type"],
                        event["title"],
                        event["country"],
                        event.get("impact", "medium"),
                        event["source"],
                        event.get("symbol"),
                        event["content_hash"],
                    ),
                )
                inserted += 1
            except Exception:
                logger.debug("Skipped duplicate economic event", exc_info=True)
        await self.conn.commit()
        return inserted

    async def get_upcoming_economic_events(
        self, days: int = 7, country: str | None = None, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get economic events within the next N days, optionally filtered."""

        today = now_ist().date().isoformat()
        end = (now_ist().date() + timedelta(days=days)).isoformat()

        query = (
            "SELECT event_date, event_type, title, country, impact, source, symbol "
            "FROM economic_events WHERE event_date >= ? AND event_date <= ?"
        )
        params: list[str] = [today, end]

        if country:
            query += " AND country = ?"
            params.append(country)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        query += " ORDER BY event_date ASC"

        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_earnings_events(self, symbol: str | None = None, days: int = 30) -> list[dict[str, Any]]:
        """Get upcoming earnings/board meeting dates, optionally for a specific symbol."""

        today = now_ist().date().isoformat()
        end = (now_ist().date() + timedelta(days=days)).isoformat()

        query = (
            "SELECT event_date, title, symbol, impact, source "
            "FROM economic_events WHERE event_type = 'earnings' "
            "AND event_date >= ? AND event_date <= ?"
        )
        params: list[str] = [today, end]

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        query += " ORDER BY event_date ASC"

        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
