"""Economic calendar, earnings, holidays, news, sentiment.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)

from yolovest.context import MarketHoursChecker

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

    # ------------------------------------------------------------------
    # Economic Calendar & Earnings
    # ------------------------------------------------------------------

    @app.get("/api/economic-calendar")
    async def get_economic_calendar(
        days: int = Query(30, ge=1, le=90),
        country: str | None = Query(None),
        event_type: str | None = Query(None),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Upcoming economic events (RBI MPC, FOMC, NSE earnings)."""
        return await ctx.db.get_upcoming_economic_events(
            days=days, country=country, event_type=event_type
        )

    @app.get("/api/earnings")
    async def get_earnings(
        symbol: str | None = Query(None),
        days: int = Query(30, ge=1, le=90),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Upcoming earnings events, optionally filtered by symbol."""
        return await ctx.db.get_earnings_events(symbol=symbol, days=days)

    # ------------------------------------------------------------------
    # Holidays & Early Close Days
    # ------------------------------------------------------------------

    @app.get("/api/holidays")
    async def get_holidays(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Return holidays and early close days from live config."""
        return {
            "holidays": ctx.config.market_hours.holidays,
            "early_close_days": ctx.config.market_hours.early_close_days,
        }

    @app.post("/api/holidays")
    async def add_holiday(
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Add a holiday or early close day.

        Body: {"date": "YYYY-MM-DD"} for full holiday
              {"date": "YYYY-MM-DD", "early_close": "13:00"} for early close
        """
        import re as _re

        body = await request.json()
        date_str: str = body.get("date", "").strip()
        early_close: str | None = body.get("early_close")

        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")

        if early_close:
            if not _re.match(r"^\d{2}:\d{2}$", early_close):
                raise HTTPException(400, "Invalid time format, expected HH:MM")
            ec = dict(ctx.config.market_hours.early_close_days)
            ec[date_str] = early_close
            ctx.config.market_hours.early_close_days = ec
            # Persist to DB
            import json as _json
            await ctx.db.set_config("market_hours.early_close_days", _json.dumps(ec))
        else:
            holidays = list(ctx.config.market_hours.holidays)
            if date_str not in holidays:
                holidays.append(date_str)
                holidays.sort()
            ctx.config.market_hours.holidays = holidays
            import json as _json
            await ctx.db.set_config("market_hours.holidays", _json.dumps(holidays))

        # Refresh market hours checker
        ctx.market_hours = MarketHoursChecker(ctx.config)
        logger.info("Holiday added: %s (early_close=%s)", date_str, early_close)
        return {"success": True, "date": date_str, "early_close": early_close}

    @app.delete("/api/holidays/{date_str}")
    async def remove_holiday(
        date_str: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Remove a holiday or early close day."""
        import json as _json

        removed = False
        # Remove from holidays list
        holidays = list(ctx.config.market_hours.holidays)
        if date_str in holidays:
            holidays.remove(date_str)
            ctx.config.market_hours.holidays = holidays
            await ctx.db.set_config("market_hours.holidays", _json.dumps(holidays))
            removed = True

        # Remove from early close days
        ec = dict(ctx.config.market_hours.early_close_days)
        if date_str in ec:
            del ec[date_str]
            ctx.config.market_hours.early_close_days = ec
            await ctx.db.set_config("market_hours.early_close_days", _json.dumps(ec))
            removed = True

        if not removed:
            raise HTTPException(404, f"Date {date_str} not found in holidays or early close days")

        ctx.market_hours = MarketHoursChecker(ctx.config)
        logger.info("Holiday removed: %s", date_str)
        return {"success": True, "date": date_str}

    # ------------------------------------------------------------------
    # News Feed & Sentiment
    # ------------------------------------------------------------------

    @app.get("/api/news")
    async def get_news_feed(
        symbol: str | None = Query(None),
        source: str | None = Query(None),
        date_from: str | None = Query(None, description="YYYY-MM-DD"),
        date_to: str | None = Query(None, description="YYYY-MM-DD (exclusive upper bound)"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Recent news articles with source attribution."""
        articles = await ctx.db.get_news_articles(
            symbol=symbol, source=source, date_from=date_from, date_to=date_to,
            limit=limit, offset=offset,
        )
        if symbol:
            # Defensive post-filter: legacy rows scraped by the old
            # substring matcher mis-tagged short symbols (e.g. ITC
            # inside BITCOIN, BPL inside REPUBLIC). Drop rows whose
            # headline doesn't contain the symbol as a standalone
            # word. New rows will already pass; old rows get hidden
            # without a destructive backfill.
            import re as _re
            pattern = _re.compile(
                rf"(?<![A-Z0-9]){_re.escape(symbol.upper())}(?![A-Z0-9])"
            )
            articles = [
                a for a in articles
                if pattern.search((a.get("headline") or "").upper())
            ]
        return articles

    @app.get("/api/sentiment/{symbol}")
    async def get_symbol_sentiment(
        symbol: str, user: str = Depends(verify_credentials)
    ) -> dict[str, Any]:
        """Latest sentiment analysis for a symbol."""
        result = await ctx.db.get_sentiment(symbol)
        if not result:
            return {"symbol": symbol, "sentiment": "neutral", "confidence": 0, "key_drivers": []}
        # SentimentResult is a Pydantic model or dict
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if hasattr(result, "dict"):
            return result.dict()
        return result if isinstance(result, dict) else {"symbol": symbol, "sentiment": "neutral", "confidence": 0, "key_drivers": []}

