"""Skill: ingest-data — Market data ingestion from all sources.

Covers: FR-2.1 to FR-2.9, FR-2.12, FR-2.13
Trigger: HEARTBEAT — every heartbeat during market hours
Pipeline position: Feeds into market-scan and generate-signals.

Flow:
1. Fetch OHLCV candles via MarketDataBase abstraction (jugaad → yfinance fallback)
2. Fetch intraday candles via tvDatafeed (if market hours)
3. Fetch NSE/BSE official data: corp announcements, bulk/block deals, FII/DII, delivery
4. Fetch news from MoneyControl, ET Markets, LiveMint
5. Fetch fundamentals from Screener.in
6. Fetch technicals from Trendlyne
7. Fetch economic calendar events
8. Fetch Google Finance data for global cues
9. Run Gemini sentiment analysis on aggregated news
10. Deduplicate news across sources (FR-2.13)
11. Persist everything to SQLite with timestamps
12. Respect rate limits for all sources (FR-2.9)
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class IngestDataSkill(SkillBase):
    name = "ingest-data"
    description = "Fetch OHLCV, news, fundamentals, and sentiment from all sources"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        # Always run during market hours; run less frequently outside
        return self.ctx.market_hours.is_market_hours() or self.ctx.scheduler.is_heartbeat_due()

    async def execute(self, **kwargs: Any) -> SkillResult:
        symbols = kwargs.get("symbols", self.ctx.config.scanning.seed_symbols)
        results: dict[str, Any] = {"symbols_ingested": 0, "news_articles": 0, "errors": []}

        # --- OHLCV Data (primary + fallback) ---
        for symbol in symbols:
            try:
                # Daily candles via jugaad-data (primary) → yfinance (fallback)
                daily = await self.ctx.market_data.fetch_daily(symbol)
                await self.ctx.db.upsert_ohlcv(symbol, "daily", daily)

                # Intraday candles via tvDatafeed (if market hours)
                if self.ctx.market_hours.is_market_hours():
                    intraday = await self.ctx.market_data.fetch_intraday(symbol)
                    await self.ctx.db.upsert_ohlcv(symbol, "intraday", intraday)

                results["symbols_ingested"] += 1
            except Exception as e:
                results["errors"].append(f"{symbol}: {e}")

        # --- NSE/BSE Official Data ---
        nse_data = await self._fetch_nse_data()
        await self.ctx.db.upsert_market_data("nse_official", nse_data)

        # --- News Aggregation + Dedup ---
        raw_news = await self._fetch_all_news(symbols)
        deduped = self._deduplicate_news(raw_news)
        results["news_articles"] = len(deduped)

        # --- Gemini Sentiment Analysis ---
        for symbol in symbols:
            symbol_news = [n for n in deduped if symbol in n.get("symbols", [])]
            if symbol_news:
                sentiment = await self.ctx.llm.analyze_sentiment(
                    symbol, [n["headline"] for n in symbol_news]
                )
                await self.ctx.db.upsert_sentiment(symbol, sentiment)

        # --- Fundamentals + Technicals ---
        await self._fetch_fundamentals(symbols)
        await self._fetch_technicals(symbols)

        return SkillResult(success=True, skill_name=self.name, data=results)

    async def _fetch_nse_data(self) -> dict:
        """Fetch corp announcements, bulk/block deals, FII/DII, delivery data."""
        # FR-2.2: NSE/BSE official data
        raise NotImplementedError

    async def _fetch_all_news(self, symbols: list[str]) -> list[dict]:
        """Aggregate news from MoneyControl, ET Markets, LiveMint, Google Finance."""
        # FR-2.3, FR-2.12
        raise NotImplementedError

    def _deduplicate_news(self, articles: list[dict]) -> list[dict]:
        """Merge duplicate news across sources. FR-2.13."""
        raise NotImplementedError

    async def _fetch_fundamentals(self, symbols: list[str]) -> None:
        """Fetch from Screener.in. FR-2.4."""
        raise NotImplementedError

    async def _fetch_technicals(self, symbols: list[str]) -> None:
        """Fetch from Trendlyne. FR-2.5."""
        raise NotImplementedError
