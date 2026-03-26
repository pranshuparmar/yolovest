"""Skill: ingest-data — Market data ingestion from all sources.

Covers: FR-2.1 to FR-2.9, FR-2.12, FR-2.13, FR-2.6 (economic calendar)
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

import logging
from datetime import timedelta
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)


class IngestDataSkill(SkillBase):
    name = "ingest-data"
    description = "Fetch OHLCV, news, fundamentals, and sentiment from all sources"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return True  # Always run; frequency controlled by orchestrator

    async def _is_cached_fresh(self, symbol: str, interval: str) -> bool:
        """Check if DB already has fresh enough data for this symbol/interval.

        Uses market_data.cache_ttl_minutes from config (default 15 min).
        For daily data during non-market hours, cached data from today is fresh.
        """
        ttl = self.ctx.config.market_data.cache_ttl_minutes
        try:
            bars = await self.ctx.db.get_ohlcv(symbol, interval, days=2)
            if not bars:
                return False
            latest = bars[-1].timestamp
            now = now_ist()
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=IST)

            if interval in ("daily", "1d"):
                # Daily data: fresh if latest bar is from today or yesterday
                return (now - latest) < timedelta(days=2)
            else:
                # Intraday: fresh if within cache_ttl_minutes
                return (now - latest) < timedelta(minutes=ttl)
        except Exception:
            return False

    async def _get_active_symbols(self) -> list[str]:
        """Get symbols for deep ingestion: watchlist first, seed_symbols as fallback.

        After ingest-universe populates the OHLCV table and market-scan builds
        a watchlist, ingest-data targets those shortlisted symbols for the
        expensive deep pass (news, sentiment, fundamentals). On a fresh install
        before the first scan, falls back to seed_symbols.
        """
        try:
            watchlist = await self.ctx.db.get_combined_watchlist()
            if watchlist:
                return [s["symbol"] for s in watchlist]
        except Exception:
            pass
        return self.ctx.config.scanning.seed_symbols

    async def execute(self, **kwargs: Any) -> SkillResult:
        symbols = kwargs.get("symbols") or await self._get_active_symbols()
        results: dict[str, Any] = {"symbols_ingested": 0, "news_articles": 0, "errors": [], "cache_hits": 0}

        # --- OHLCV Data (primary + fallback) ---
        for symbol in symbols:
            try:
                # Skip external fetch if DB cache is fresh
                if await self._is_cached_fresh(symbol, "daily"):
                    results["symbols_ingested"] += 1
                    results["cache_hits"] += 1
                    logger.debug("Cache hit for %s daily — skipping external fetch", symbol)
                else:
                    daily = await self.ctx.market_data.get_ohlcv(symbol, "daily", days=30)
                    await self.ctx.db.upsert_ohlcv(symbol, "daily", daily, "ingester")
                    results["symbols_ingested"] += 1

                # Intraday candles if market is open
                if self.ctx.market_hours.is_market_hours():
                    if not await self._is_cached_fresh(symbol, "5minute"):
                        try:
                            intraday = await self.ctx.market_data.get_ohlcv(
                                symbol, "5minute", days=1
                            )
                            await self.ctx.db.upsert_ohlcv(symbol, "5minute", intraday, "ingester")
                        except Exception as e:
                            logger.debug("Intraday fetch skipped for %s: %s", symbol, e)

            except Exception as e:
                results["errors"].append(f"{symbol}: {e}")
                logger.warning("OHLCV fetch failed for %s: %s", symbol, e)

        # --- News Aggregation + Dedup ---
        raw_news = await self._fetch_all_news(symbols)
        deduped = self._deduplicate_news(raw_news)
        results["news_articles"] = len(deduped)

        # Persist news articles
        if deduped:
            await self.ctx.db.upsert_news_articles(deduped)

        # --- Gemini Sentiment Analysis (FR-2.7) ---
        for symbol in symbols:
            symbol_headlines = [
                n.headline for n in deduped
                if symbol.lower() in " ".join(n.symbols).lower()
                or symbol.lower() in n.headline.lower()
            ]
            if symbol_headlines:
                try:
                    sentiment = await self.ctx.llm.analyze_sentiment(symbol, symbol_headlines)
                    await self.ctx.db.upsert_sentiment(symbol, sentiment)
                except Exception as e:
                    logger.warning("Sentiment analysis failed for %s: %s", symbol, e)

        # --- NSE Official Data (FR-2.2) ---
        try:
            nse_data = await self._fetch_nse_data()
            if nse_data:
                logger.info("NSE data fetched: %d items", len(nse_data))
        except Exception as e:
            logger.warning("NSE data fetch failed: %s", e)

        # --- Economic Calendar (FR-2.6) ---
        try:
            econ_events = await self._fetch_economic_calendar()
            if econ_events:
                count = await self.ctx.db.upsert_economic_events(econ_events)
                results["economic_events"] = count
                logger.info("Ingested %d economic calendar events", count)
        except Exception as e:
            logger.warning("Economic calendar fetch failed: %s", e)

        # --- Fundamentals from Screener.in (FR-2.4) ---
        try:
            fundamentals_count = await self._fetch_fundamentals(symbols)
            results["fundamentals_updated"] = fundamentals_count
        except Exception as e:
            logger.warning("Fundamentals fetch failed: %s", e)

        # --- Technicals from Trendlyne (FR-2.5) ---
        try:
            technicals_count = await self._fetch_technicals(symbols)
            results["technicals_updated"] = technicals_count
        except Exception as e:
            logger.warning("Technicals fetch failed: %s", e)

        # --- Google Finance (FR-2.12) ---
        try:
            gf_data = await self._fetch_google_finance(symbols)
            if gf_data:
                results["google_finance"] = {
                    "indices": len(gf_data.get("indices", {})),
                    "trending": len(gf_data.get("trending_tickers", [])),
                    "news": len(gf_data.get("news", [])),
                }
                # Merge Google Finance news into the deduped articles
                gf_news = gf_data.get("news", [])
                if gf_news:
                    gf_deduped = self._deduplicate_news(list(deduped) + gf_news)
                    new_articles = [a for a in gf_deduped if a not in deduped]
                    if new_articles:
                        await self.ctx.db.upsert_news_articles(new_articles)
                        results["news_articles"] += len(new_articles)
        except Exception as e:
            logger.warning("Google Finance fetch failed: %s", e)

        # Partial success: only fail if ALL symbols failed OHLCV
        all_failed = results["symbols_ingested"] == 0 and len(results["errors"]) > 0
        return SkillResult(
            success=not all_failed,
            skill_name=self.name,
            data=results,
        )

    async def _fetch_nse_data(self) -> dict[str, Any]:
        """Fetch corp announcements, bulk/block deals, FII/DII, delivery data.

        Uses NSEOfficialSource for all NSE API interactions (FR-2.2).
        Failures are caught per-category so partial data is still returned.
        """
        from yolovest.news.nse_official import NSEOfficialSource

        nse = NSEOfficialSource()
        result: dict[str, Any] = {}

        try:
            # Bulk/block deals
            try:
                deals = await nse.fetch_bulk_deals()
                if deals:
                    result["bulk_deals"] = deals
                    logger.info("NSE: fetched %d bulk/block deals", len(deals))
            except Exception as e:
                logger.warning("NSE bulk deals fetch failed: %s", e)

            # FII/DII activity
            try:
                fii_dii = await nse.fetch_fii_dii()
                if fii_dii:
                    result["fii_dii"] = fii_dii
                    logger.info("NSE: fetched FII/DII data")
            except Exception as e:
                logger.warning("NSE FII/DII fetch failed: %s", e)

            # Corporate actions + delivery data per symbol
            symbols = self.ctx.config.scanning.seed_symbols
            for symbol in symbols:
                try:
                    actions = await nse.fetch_corp_actions(symbol)
                    if actions:
                        result.setdefault("corp_actions", {})[symbol] = actions
                except Exception as e:
                    logger.debug("NSE corp actions for %s failed: %s", symbol, e)

                try:
                    delivery = await nse.fetch_delivery_data(symbol)
                    if delivery is not None:
                        result.setdefault("delivery_data", {})[symbol] = delivery
                except Exception as e:
                    logger.debug("NSE delivery data for %s failed: %s", symbol, e)
        finally:
            await nse.close()

        return result

    async def _fetch_all_news(self, symbols: list[str]) -> list[Any]:
        """Aggregate news from all configured sources."""
        from yolovest.models.schemas import NewsArticle

        all_articles: list[NewsArticle] = []

        if self.ctx.news_aggregator is not None:
            try:
                all_articles = await self.ctx.news_aggregator.fetch_all(symbols)
            except Exception as e:
                logger.warning("News aggregator failed: %s", e)
        else:
            logger.debug("No news aggregator configured, skipping news fetch")

        return all_articles

    def _deduplicate_news(self, articles: list[Any]) -> list[Any]:
        """Merge duplicate news across sources. FR-2.13."""
        if not articles:
            return []

        seen: dict[str, Any] = {}
        for article in articles:
            h = article.content_hash
            if h not in seen:
                seen[h] = article
            else:
                # Merge symbols from duplicate
                existing = seen[h]
                for sym in article.symbols:
                    if sym not in existing.symbols:
                        existing.symbols.append(sym)
        return list(seen.values())

    async def _fetch_google_finance(self, symbols: list[str]) -> dict[str, Any] | None:
        """Fetch market data from Google Finance (FR-2.12)."""
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        try:
            return await scraper.fetch_all(symbols)
        finally:
            await scraper.close()

    async def _fetch_economic_calendar(self) -> list[dict[str, Any]]:
        """Fetch economic calendar events (FR-2.6): RBI, Fed, earnings."""
        from yolovest.data.economic_calendar import EconomicCalendarSource

        source = EconomicCalendarSource()
        try:
            return await source.fetch_all_events(lookback_days=7, lookahead_days=30)
        finally:
            await source.close()

    async def _fetch_fundamentals(self, symbols: list[str]) -> int:
        """Fetch fundamental data from Screener.in (FR-2.4)."""
        from yolovest.data.screener import ScreenerScraper

        scraper = ScreenerScraper()
        count = 0
        try:
            batch = await scraper.fetch_batch(symbols)
            for symbol, data in batch.items():
                await self.ctx.db.upsert_fundamentals(symbol, data)
                count += 1
            if count:
                logger.info("Fundamentals updated for %d symbols via Screener.in", count)
        finally:
            await scraper.close()
        return count

    async def _fetch_technicals(self, symbols: list[str]) -> int:
        """Fetch technical screener data from Trendlyne (FR-2.5)."""
        from yolovest.data.trendlyne import TrendlyneScraper

        scraper = TrendlyneScraper()
        count = 0
        try:
            batch = await scraper.fetch_batch(symbols)
            for symbol, data in batch.items():
                # Store technical signals alongside fundamentals
                # Trendlyne data enriches the fundamentals table
                await self.ctx.db.upsert_fundamentals(symbol, data)
                count += 1
            if count:
                logger.info("Technicals updated for %d symbols via Trendlyne", count)
        finally:
            await scraper.close()
        return count
