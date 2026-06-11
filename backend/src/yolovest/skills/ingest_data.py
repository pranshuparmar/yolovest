"""Skill: ingest-data — Market data ingestion from all sources.

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
10. Deduplicate news across sources
11. Persist everything to SQLite with timestamps
12. Respect rate limits for all sources

Backpressure: expensive fetches (news, scrapers, etc.) run concurrently
with per-source timeouts and an overall budget. If the budget is exhausted,
remaining sources are skipped rather than blocking the heartbeat pipeline.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)

# Overall time budget for the expensive fetch phase (news, scrapers, etc.)
# If this is exceeded, remaining sources are skipped.
_EXPENSIVE_BUDGET_SEC = 90

# Per-source timeout for individual fetches within the expensive phase.
_PER_SOURCE_TIMEOUT_SEC = 30

# Max symbols to refresh per heartbeat for the two slow per-symbol scrapers.
# Screener.in self-throttles at ~2s/symbol; Trendlyne at ~2.5s/symbol (API +
# HTML fallback). With a 30s per-source budget that means ~12-14 symbols max
# before the timeout fires. Fundamentals/technicals also only change at
# quarterly result announcements, so a 24h cache + small per-cycle slice is
# fine — the watchlist is filled in over a handful of heartbeats and then
# stays warm.
_FUNDAMENTALS_PER_CYCLE = 10
_TECHNICALS_PER_CYCLE = 10
_FUNDAMENTALS_CACHE_HOURS = 24


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

        Quarantine replacements are applied before returning.
        """
        try:
            watchlist = await self.ctx.db.get_combined_watchlist()
            if watchlist:
                base = [s["symbol"] for s in watchlist]
                return await self.ctx.db.resolve_symbols_with_replacements(base)
        except Exception:
            pass
        return await self.ctx.db.resolve_symbols_with_replacements(
            list(self.ctx.config.scanning.seed_symbols),
        )

    async def _select_priority_symbols(self, limit: int = 15) -> list[str]:
        """Symbols worth spending NSE's per-symbol rate budget on.

        Composition (in order, deduped):
          1. Open positions — we need to know about corporate actions on
             stocks we currently hold (splits, bonuses, dividends).
          2. Top N from the algorithmic watchlist by composite_score —
             these are the most likely signal candidates.

        Capped at `limit` to keep total NSE calls bounded. Falls back to
        seed_symbols only if both sources are empty (truly fresh install).
        """
        ordered: list[str] = []
        seen: set[str] = set()

        try:
            for pos in await self.ctx.db.get_open_positions(mode=self.ctx.config.mode):
                sym = pos.get("symbol") if isinstance(pos, dict) else getattr(pos, "symbol", None)
                if sym and sym not in seen:
                    seen.add(sym)
                    ordered.append(sym)
        except Exception:
            logger.debug("ingest-data: could not read open positions", exc_info=True)

        try:
            for row in await self.ctx.db.get_watchlist():
                sym = row.get("symbol")
                if sym and sym not in seen:
                    seen.add(sym)
                    ordered.append(sym)
                if len(ordered) >= limit:
                    break
        except Exception:
            logger.debug("ingest-data: could not read watchlist", exc_info=True)

        if not ordered:
            return await self.ctx.db.resolve_symbols_with_replacements(
                list(self.ctx.config.scanning.seed_symbols),
            )

        return await self.ctx.db.resolve_symbols_with_replacements(ordered[:limit])

    async def execute(self, **kwargs: Any) -> SkillResult:
        symbols = kwargs.get("symbols") or await self._get_active_symbols()
        results: dict[str, Any] = {
            "symbols_ingested": 0, "news_articles": 0, "errors": [],
            "cache_hits": 0, "quarantined": 0,
        }

        # Include index symbol for market regime detection (if enabled)
        regime_cfg = self.ctx.config.strategy.market_regime
        index_symbol = regime_cfg.index_symbol if regime_cfg.enabled else None
        if index_symbol and index_symbol not in symbols:
            symbols.append(index_symbol)

        # Load quarantined symbols for fast skip (but never quarantine index symbols)
        quarantined = await self.ctx.db.get_all_quarantined_symbol_set()
        if index_symbol:
            quarantined.discard(index_symbol)
        active_symbols = [s for s in symbols if s not in quarantined]
        results["quarantined"] = len(symbols) - len(active_symbols)
        if results["quarantined"] > 0:
            logger.info(
                "ingest-data: skipping %d quarantined symbols: %s",
                results["quarantined"],
                sorted(quarantined & set(symbols)),
            )

        # --- OHLCV Data (primary + fallback) — fetched concurrently ---
        # Use a semaphore to limit concurrent external API calls (avoid rate limits).
        _OHLCV_CONCURRENCY = 5
        sem = asyncio.Semaphore(_OHLCV_CONCURRENCY)
        is_market = self.ctx.market_hours.is_market_hours()

        async def _ingest_symbol(symbol: str) -> dict[str, Any]:
            """Ingest OHLCV for a single symbol. Returns per-symbol result."""
            async with sem:
                result: dict[str, Any] = {"symbol": symbol, "ok": False, "cache_hit": False}
                try:
                    if await self._is_cached_fresh(symbol, "daily"):
                        result["ok"] = True
                        result["cache_hit"] = True
                    else:
                        daily = await self.ctx.market_data.get_ohlcv(symbol, "daily", days=30)
                        await self.ctx.db.upsert_ohlcv(
                            symbol, "daily", daily,
                            self._ingest_source(symbol, "ingester"),
                        )
                        result["ok"] = True

                        # Check provider-level health (errors, empties) for quarantine.
                        # This catches delisted symbols where one provider returns
                        # empty data and another returns stale-but-recent bars.
                        fetch_meta = {}
                        if hasattr(self.ctx.market_data, "get_fetch_meta"):
                            fetch_meta = self.ctx.market_data.get_fetch_meta(symbol)

                        provider_had_issues = (
                            fetch_meta.get("providers_empty", 0) > 0
                            or fetch_meta.get("provider_errors", 0) > 0
                        )

                        if daily:
                            latest = daily[-1].timestamp
                            if latest.tzinfo is None:
                                latest = latest.replace(tzinfo=IST)
                            days_old = (now_ist() - latest).days

                            if days_old > 5:
                                # Clearly stale — count as failure
                                logger.warning("Stale data for %s: latest bar is %dd old", symbol, days_old)
                                now_quarantined = await self.ctx.db.record_fetch_failure(
                                    symbol, f"data {days_old}d stale",
                                )
                                if now_quarantined:
                                    result["quarantined"] = True
                            elif provider_had_issues and fetch_meta.get("all_providers_tried"):
                                # Data returned but providers errored/returned empty
                                # (e.g. yfinance says delisted, jugaad returns last
                                # known bars). Count as failure toward quarantine.
                                errors = fetch_meta.get("provider_errors", 0)
                                empties = fetch_meta.get("providers_empty", 0)
                                logger.warning(
                                    "Provider issues for %s: %d errors, %d empty "
                                    "(data %dd old, possibly delisted)",
                                    symbol, errors, empties, days_old,
                                )
                                now_quarantined = await self.ctx.db.record_fetch_failure(
                                    symbol,
                                    f"provider issues: {errors} errors, {empties} empty, "
                                    f"data {days_old}d old",
                                )
                                if now_quarantined:
                                    result["quarantined"] = True
                            else:
                                await self.ctx.db.record_fetch_success(symbol)
                        else:
                            # No data at all from any provider
                            now_quarantined = await self.ctx.db.record_fetch_failure(
                                symbol, "all providers returned empty",
                            )
                            if now_quarantined:
                                result["quarantined"] = True

                    # Intraday candles if market is open
                    if is_market and not await self._is_cached_fresh(symbol, "5minute"):
                        try:
                            intraday = await self.ctx.market_data.get_ohlcv(symbol, "5minute", days=1)
                            await self.ctx.db.upsert_ohlcv(
                                symbol, "5minute", intraday,
                                self._ingest_source(symbol, "ingester"),
                            )
                        except Exception as e:
                            logger.debug("Intraday fetch skipped for %s: %s", symbol, e)

                except Exception as e:
                    result["error"] = str(e)
                    logger.warning("OHLCV fetch failed for %s: %s", symbol, e)
                    now_quarantined = await self.ctx.db.record_fetch_failure(symbol, str(e))
                    if now_quarantined:
                        result["quarantined"] = True

                return result

        # Fire all symbol fetches concurrently (bounded by semaphore)
        symbol_results = await asyncio.gather(
            *[_ingest_symbol(s) for s in active_symbols],
            return_exceptions=True,
        )

        for sr in symbol_results:
            if isinstance(sr, BaseException):
                # gather(return_exceptions=True) can also hand back
                # CancelledError (BaseException) during shutdown — treat
                # anything non-dict as a failed symbol.
                results["errors"].append(str(sr))
                continue
            if sr.get("ok"):
                results["symbols_ingested"] += 1
            if sr.get("cache_hit"):
                results["cache_hits"] += 1
            if sr.get("error"):
                results["errors"].append(f"{sr['symbol']}: {sr['error']}")
            if sr.get("quarantined"):
                results["quarantined"] += 1

        # --- Check if expensive fetches should be skipped ---
        # News, fundamentals, Google Finance etc. don't change minute-to-minute.
        # Skip if we fetched them within the cache TTL.
        skip_expensive = False
        try:
            last_full = await self.ctx.db.get_system_state("last_full_ingest")
            if last_full:
                from datetime import datetime
                last_ts = datetime.fromisoformat(last_full)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=IST)
                from datetime import timedelta
                ttl = self.ctx.config.market_data.cache_ttl_minutes
                if (now_ist() - last_ts) < timedelta(minutes=ttl):
                    skip_expensive = True
                    results["cache_hits"] += 1
                    logger.debug("Skipping expensive fetches (last full ingest %.0fs ago)",
                                 (now_ist() - last_ts).total_seconds())
        except Exception:
            pass

        # --- Expensive fetches: news, scrapers, sentiment ---
        # Run concurrently with per-source timeouts and an overall budget
        # to prevent slow external APIs from blocking the heartbeat pipeline.
        news_enabled = self.ctx.config.market_data.news_enabled
        scrapers_enabled = self.ctx.config.market_data.scrapers_enabled

        if not skip_expensive:
            budget_results = await self._run_expensive_fetches(
                symbols, active_symbols, news_enabled, scrapers_enabled, results,
            )
            results["news_articles"] = budget_results.get("news_count", 0)
            results["budget_elapsed_sec"] = budget_results.get("elapsed_sec", 0)
            sources_completed = budget_results.get("sources_completed", 0)
            sources_timed_out = budget_results.get("sources_timed_out", 0)
            if sources_timed_out:
                logger.warning(
                    "ingest-data: %d/%d expensive sources timed out (budget=%.0fs)",
                    sources_timed_out, sources_completed + sources_timed_out,
                    _EXPENSIVE_BUDGET_SEC,
                )
        else:
            results["news_articles"] = 0

        # Partial success: only fail if ALL symbols failed OHLCV
        all_failed = results["symbols_ingested"] == 0 and len(results["errors"]) > 0

        logger.info(
            "ingest-data: %d/%d symbols ingested (cache_hits=%d), "
            "news=%d articles, errors=%d%s",
            results["symbols_ingested"], len(symbols), results["cache_hits"],
            results["news_articles"], len(results["errors"]),
            " [SKIPPED expensive fetches]" if skip_expensive else "",
        )

        return SkillResult(
            success=not all_failed,
            skill_name=self.name,
            data=results,
        )

    async def _fetch_nse_data(self) -> dict[str, Any]:
        """Fetch corp announcements, bulk/block deals, FII/DII, delivery data.

        Uses NSEOfficialSource for all NSE API interactions.
        Failures are caught per-category so partial data is still returned.
        """
        from yolovest.news.nse_official import NSEOfficialSource

        nse = NSEOfficialSource()
        result: dict[str, Any] = {}

        try:
            # Bulk/block deals — persist so risk-check + ML features can
            # see institutional accumulation/distribution signals.
            try:
                deals = await nse.fetch_bulk_deals()
                if deals:
                    result["bulk_deals"] = deals
                    inserted = await self.ctx.db.upsert_bulk_deals(deals)
                    logger.info(
                        "NSE: fetched %d bulk/block deals (%d new)",
                        len(deals), inserted,
                    )
            except Exception as e:
                logger.warning("NSE bulk deals fetch failed: %s", e)

            # FII/DII activity — persist so risk-check can gate on
            # foreign net selling days.
            try:
                fii_dii = await nse.fetch_fii_dii()
                if fii_dii:
                    result["fii_dii"] = fii_dii
                    wrote = await self.ctx.db.upsert_fii_dii(fii_dii)
                    if wrote:
                        logger.info(
                            "NSE: persisted FII/DII for %s (fii_net=%.1f, dii_net=%.1f)",
                            fii_dii.get("date"),
                            (fii_dii.get("fii") or {}).get("net_value", 0.0),
                            (fii_dii.get("dii") or {}).get("net_value", 0.0),
                        )
            except Exception as e:
                logger.warning("NSE FII/DII fetch failed: %s", e)

            # Corporate actions + delivery data for the symbols that matter:
            # open positions (corp actions directly affect what we hold) plus
            # the top watchlist names by composite score. Capped at 15 to
            # respect NSE's aggressive per-symbol rate limits.
            symbols = await self._select_priority_symbols(limit=15)
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
                        # Persist on today's daily bar so risk-check and
                        # ML features can read recent delivery quality.
                        await self.ctx.db.update_delivery_pct(symbol, delivery)
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
        """Merge duplicate news across sources."""
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
        """Fetch market data from Google Finance."""
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        try:
            return await scraper.fetch_all(symbols)
        finally:
            await scraper.close()

    async def _fetch_economic_calendar(self) -> list[dict[str, Any]]:
        """Fetch economic calendar events: RBI, Fed, earnings."""
        from yolovest.data.economic_calendar import EconomicCalendarSource

        source = EconomicCalendarSource()
        try:
            return await source.fetch_all_events(lookback_days=7, lookahead_days=30)
        finally:
            await source.close()

    async def _fetch_fundamentals(self, symbols: list[str]) -> int:
        """Fetch fundamental data from Screener.in.

        Refreshes only stale-or-missing rows, capped to a small slice per
        heartbeat so we never blow the per-source ingest budget. The
        cooperative refresh fills the watchlist over a handful of cycles.
        """
        from yolovest.data.screener import ScreenerScraper

        try:
            stale = await self.ctx.db.get_stale_fundamentals_symbols(
                symbols, max_age_hours=_FUNDAMENTALS_CACHE_HOURS,
            )
        except Exception:
            logger.debug("ingest-data: stale-fundamentals lookup failed", exc_info=True)
            stale = list(symbols)
        targets = stale[:_FUNDAMENTALS_PER_CYCLE]
        if not targets:
            logger.debug("ingest-data: all watched fundamentals fresh, skipping Screener.in")
            return 0

        scraper = ScreenerScraper()
        count = 0
        try:
            batch = await scraper.fetch_batch(targets)
            for symbol, data in batch.items():
                await self.ctx.db.upsert_fundamentals(symbol, data)
                count += 1
            if count:
                logger.info(
                    "Fundamentals updated for %d/%d symbols via Screener.in (queue=%d)",
                    count, len(targets), len(stale),
                )
        finally:
            await scraper.close()
        return count

    async def _fetch_technicals(self, symbols: list[str]) -> int:
        """Fetch technical screener data from Trendlyne.

        Uses the same stale-row + per-cycle-cap strategy as fundamentals;
        Trendlyne data is upserted into the same `fundamentals` table so
        a single freshness window covers both scrapers.
        """
        from yolovest.data.trendlyne import TrendlyneScraper

        try:
            stale = await self.ctx.db.get_stale_fundamentals_symbols(
                symbols, max_age_hours=_FUNDAMENTALS_CACHE_HOURS,
            )
        except Exception:
            logger.debug("ingest-data: stale-fundamentals lookup failed", exc_info=True)
            stale = list(symbols)
        targets = stale[:_TECHNICALS_PER_CYCLE]
        if not targets:
            logger.debug("ingest-data: all watched technicals fresh, skipping Trendlyne")
            return 0

        scraper = TrendlyneScraper()
        count = 0
        try:
            batch = await scraper.fetch_batch(targets)
            for symbol, data in batch.items():
                # Store technical signals alongside fundamentals
                # Trendlyne data enriches the fundamentals table
                await self.ctx.db.upsert_fundamentals(symbol, data)
                count += 1
            if count:
                logger.info(
                    "Technicals updated for %d/%d symbols via Trendlyne (queue=%d)",
                    count, len(targets), len(stale),
                )
        finally:
            await scraper.close()
        return count

    async def _run_expensive_fetches(
        self,
        symbols: list[str],
        active_symbols: list[str],
        news_enabled: bool,
        scrapers_enabled: bool,
        results: dict[str, Any],
    ) -> dict[str, Any]:
        """Run all expensive fetches concurrently under an overall time budget.

        Each source gets _PER_SOURCE_TIMEOUT_SEC individually. The entire
        batch is wrapped in _EXPENSIVE_BUDGET_SEC so one hung source can't
        starve the heartbeat pipeline.

        Returns dict with deduped news, counts, and timing info.
        """
        import time as _time

        budget_start = _time.monotonic()
        deduped: list[Any] = []
        news_count = 0
        sources_completed = 0
        sources_timed_out = 0

        # --- Phase 1: News + scrapers in parallel ---
        async def _timed(name: str, coro: Any) -> tuple[str, Any]:
            """Run a coroutine with per-source timeout."""
            try:
                result = await asyncio.wait_for(coro, timeout=_PER_SOURCE_TIMEOUT_SEC)
                return (name, result)
            except TimeoutError:
                logger.warning("ingest-data: %s timed out after %ds", name, _PER_SOURCE_TIMEOUT_SEC)
                return (name, None)
            except Exception as e:
                logger.warning("ingest-data: %s failed: %s", name, e)
                return (name, None)

        tasks: list[Any] = []

        if news_enabled:
            tasks.append(_timed("news", self._fetch_all_news(symbols)))

        if scrapers_enabled:
            tasks.append(_timed("nse", self._fetch_nse_data()))
            tasks.append(_timed("economic_calendar", self._fetch_economic_calendar()))
            tasks.append(_timed("fundamentals", self._fetch_fundamentals(symbols)))
            tasks.append(_timed("technicals", self._fetch_technicals(symbols)))
            tasks.append(_timed("google_finance", self._fetch_google_finance(symbols)))

        if tasks:
            # Run all under the overall budget
            try:
                completed = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_EXPENSIVE_BUDGET_SEC,
                )
            except TimeoutError:
                logger.warning(
                    "ingest-data: overall budget of %ds exceeded, skipping remaining sources",
                    _EXPENSIVE_BUDGET_SEC,
                )
                completed = []

            # Process results
            source_results: dict[str, Any] = {}
            for item in completed:
                if isinstance(item, tuple):
                    name, value = item
                    source_results[name] = value
                    if value is not None:
                        sources_completed += 1
                    else:
                        sources_timed_out += 1
                else:
                    sources_timed_out += 1

            # --- Process news ---
            raw_news = source_results.get("news")
            if raw_news:
                deduped = self._deduplicate_news(raw_news)
                news_count = len(deduped)
                if deduped:
                    await self.ctx.db.upsert_news_articles(deduped)

            # --- Process NSE data ---
            nse_data = source_results.get("nse")
            if nse_data:
                logger.info("NSE data fetched: %d items", len(nse_data))

            # --- Process economic calendar ---
            econ_events = source_results.get("economic_calendar")
            if econ_events:
                count = await self.ctx.db.upsert_economic_events(econ_events)
                results["economic_events"] = count

            # --- Process fundamentals ---
            fund_count = source_results.get("fundamentals")
            if fund_count:
                results["fundamentals_updated"] = fund_count

            # --- Process technicals ---
            tech_count = source_results.get("technicals")
            if tech_count:
                results["technicals_updated"] = tech_count

            # --- Process Google Finance ---
            gf_data = source_results.get("google_finance")
            if gf_data:
                results["google_finance"] = {
                    "indices": len(gf_data.get("indices", {})),
                    "trending": len(gf_data.get("trending_tickers", [])),
                    "news": len(gf_data.get("news", [])),
                }
                gf_news = gf_data.get("news", [])
                if gf_news:
                    gf_deduped = self._deduplicate_news(list(deduped) + gf_news)
                    new_articles = [a for a in gf_deduped if a not in deduped]
                    if new_articles:
                        await self.ctx.db.upsert_news_articles(new_articles)
                        news_count += len(new_articles)

            # Mark last full ingest time
            try:
                from yolovest.timezone import now_utc
                await self.ctx.db.set_system_state("last_full_ingest", now_utc().isoformat())
            except Exception:
                pass

        # --- Phase 2: Sentiment (depends on news, runs after) ---
        if self.ctx.config.llm.enabled and deduped:
            for symbol in active_symbols:
                # Check budget before each sentiment call
                if _time.monotonic() - budget_start > _EXPENSIVE_BUDGET_SEC:
                    logger.warning("ingest-data: budget exhausted, skipping remaining sentiment")
                    break
                symbol_headlines = [
                    n.headline for n in deduped
                    if symbol.lower() in " ".join(n.symbols).lower()
                    or symbol.lower() in n.headline.lower()
                ]
                if symbol_headlines:
                    try:
                        sentiment = await asyncio.wait_for(
                            self.ctx.llm.analyze_sentiment(symbol, symbol_headlines),
                            timeout=_PER_SOURCE_TIMEOUT_SEC,
                        )
                        await self.ctx.db.upsert_sentiment(symbol, sentiment)
                    except TimeoutError:
                        logger.warning("Sentiment analysis timed out for %s", symbol)
                    except Exception as e:
                        logger.warning("Sentiment analysis failed for %s: %s", symbol, e)

        elapsed = _time.monotonic() - budget_start
        return {
            "deduped": deduped,
            "news_count": news_count,
            "elapsed_sec": round(elapsed, 1),
            "sources_completed": sources_completed,
            "sources_timed_out": sources_timed_out,
        }
