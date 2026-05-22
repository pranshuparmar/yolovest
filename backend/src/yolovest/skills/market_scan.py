"""Skill: market-scan — Dynamic stock scanning and ranking.

Trigger: HEARTBEAT during market hours (refreshes each heartbeat)
Pipeline position: After ingest-data/ingest-premarket, before generate-signals.

Flow:
1. Load latest OHLCV, volume, news sentiment, and fundamental data from DB
2. Scan NSE universe — apply volume filter (scanning.min_avg_daily_volume)
3. Filter out: illiquid stocks, F&O ban list, pending corporate actions
4. Compute technical features (RSI, MACD, ATR, SuperTrend) from OHLCV bars
5. Score each stock using configurable weighted algorithm (scanning.weights):
   - Technical score (default 35%): trend strength, breakout patterns
   - Volume/momentum (default 25%): relative volume, delivery %, momentum indicators
   - News sentiment (default 15%): Gemini sentiment from ingest-data
   - Fundamental quality (default 15%): PE, debt ratio, promoter holding
   - Volatility (default 10%): ATR% preference bell curve
6. Rank and shortlist top N stocks (scanning.shortlist_size)
7. Track sector rotation — flag sectors showing strength/weakness
8. Use Gemini to cross-validate shortlist against market narrative
9. Update dynamic watchlist in DB
"""

import asyncio
import logging
from typing import Any

from yolovest.data.features import IndicatorConfig, compute_features
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class MarketScanSkill(SkillBase):
    name = "market-scan"
    description = "Scan NSE universe, rank stocks, produce watchlist"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_market_hours())

    async def execute(self, **kwargs: Any) -> SkillResult:
        cfg = self.ctx.config.scanning

        # Step 1-2: Load universe, apply volume filter
        sentiment_ttl = self.ctx.config.market_data.sentiment_ttl_hours
        universe = await self.ctx.db.get_nse_universe(
            sentiment_ttl_hours=sentiment_ttl,
        )
        if not universe:
            # Fallback to seed symbols if universe is empty
            universe = [
                {"symbol": s, "avg_daily_volume": cfg.min_avg_daily_volume + 1}
                for s in cfg.seed_symbols
            ]
        liquid = [
            s for s in universe
            if (s.get("avg_daily_volume") or 0) >= cfg.min_avg_daily_volume
        ]

        # Step 3: Filter out banned / corporate action stocks
        filtered = self._apply_exclusion_filters(liquid)
        count_after_exclusion = len(filtered)

        # Exclude symbols in rotation cooldown — they failed to produce signals
        # for consecutive heartbeats, so give them a break and free slots.
        if cfg.rotation_enabled:
            cooldown = await self.ctx.db.get_rotation_cooldown_symbols()
            if cooldown:
                before = len(filtered)
                filtered = [s for s in filtered if s.get("symbol", "").upper() not in cooldown]
                logger.info(
                    "market-scan: rotation cooldown excluded %d/%d symbols: %s",
                    before - len(filtered), before, sorted(cooldown),
                )
        count_after_rotation = len(filtered)

        # Step 4: Enrich with technical indicators from OHLCV bars
        filtered = await self._enrich_with_features(filtered)

        # Detect market regime (if enabled) before scoring
        regime_cfg = self.ctx.config.strategy.market_regime
        regime = "unknown"
        if regime_cfg.enabled:
            regime = await self._detect_market_regime()
            logger.info("market-scan: detected market regime = %s", regime)

        # Step 5: Compute sub-scores and weighted composite
        # Adjust weights based on market regime
        w_technical = cfg.weights.technical
        w_volume = cfg.weights.volume_momentum
        w_sentiment = cfg.weights.news_sentiment
        w_fundamental = cfg.weights.fundamental
        w_volatility = cfg.weights.volatility

        if regime_cfg.enabled and regime != "unknown":
            if regime == "bull":
                # Boost technical, reduce fundamental
                w_technical *= 1.20
                w_fundamental *= 0.80
            elif regime == "bear":
                # Boost fundamental, reduce technical
                w_fundamental *= 1.20
                w_technical *= 0.80
            elif regime == "range":
                # Boost volatility weight
                w_volatility *= 1.50

            # Re-normalize weights to sum to 1.0
            w_total = w_technical + w_volume + w_sentiment + w_fundamental + w_volatility
            if w_total > 0:
                w_technical /= w_total
                w_volume /= w_total
                w_sentiment /= w_total
                w_fundamental /= w_total
                w_volatility /= w_total

        scored = []
        for stock in filtered:
            sub = self._compute_sub_scores(stock)
            composite = (
                sub["technical_score"] * w_technical
                + sub["volume_momentum_score"] * w_volume
                + sub["news_sentiment_score"] * w_sentiment
                + sub["fundamental_score"] * w_fundamental
                + sub["volatility_score"] * w_volatility
            )
            scored.append({**stock, **sub, "composite_score": composite})

        # Step 5: Rank and shortlist
        # Sort by composite score, break ties by volume (avoids alphabetical bias)
        scored.sort(
            key=lambda s: (s["composite_score"], s.get("avg_daily_volume") or 0),
            reverse=True,
        )
        shortlist = scored[: cfg.shortlist_size]

        # Step 6: Sector rotation analysis
        sector_analysis = self._analyze_sector_rotation(scored)

        # Step 7: Gemini cross-validation
        if self.ctx.config.llm.enabled and self.ctx.config.risk.llm_review_enabled and shortlist:
            try:
                llm_validation = await self.ctx.llm.validate_watchlist(
                    shortlist=shortlist,
                    sector_analysis=sector_analysis,
                    premarket_context=await self.ctx.db.get_latest_premarket(),
                )
                # LLM can reorder or filter stocks
                if hasattr(llm_validation, "approved_symbols") and llm_validation.approved_symbols:
                    approved = set(llm_validation.approved_symbols)
                    shortlist = [s for s in shortlist if s["symbol"] in approved]
            except Exception as e:
                logger.warning("LLM watchlist validation failed, using rules-only: %s", e)

        # Step 8: Persist watchlist (updates each heartbeat)
        await self.ctx.db.upsert_watchlist(shortlist)

        logger.info(
            "market-scan: universe=%d, liquid=%d, after_exclusion=%d, "
            "after_rotation=%d, shortlisted=%d — top: %s | "
            "sectors strong=%s weak=%s",
            len(universe), len(liquid), count_after_exclusion,
            count_after_rotation, len(shortlist),
            [s["symbol"] for s in shortlist[:5]],
            sector_analysis.get("strong", []),
            sector_analysis.get("weak", []),
        )

        result_data: dict[str, Any] = {
            "universe_size": len(universe),
            "liquid": len(liquid),
            "after_exclusion": count_after_exclusion,
            "after_rotation": count_after_rotation,
            # Kept for backwards-compat with any downstream consumer
            # reading the old field name; equals after_rotation.
            "after_filters": count_after_rotation,
            "shortlist_size": len(shortlist),
            "top_stocks": [s["symbol"] for s in shortlist[:5]],
            "strong_sectors": sector_analysis.get("strong", []),
            "weak_sectors": sector_analysis.get("weak", []),
        }
        if regime_cfg.enabled:
            result_data["market_regime"] = regime
            # Persist for downstream skills (generate-signals reads this)
            await self.ctx.db.set_system_state("market_regime", regime)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data=result_data,
        )

    def _apply_exclusion_filters(self, stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove F&O banned stocks, pending corporate actions, etc."""
        # F&O ban list and corp actions would be fetched from NSE in production.
        # For now, pass through — the volume filter already removes illiquid stocks.
        return stocks

    async def _enrich_with_features(
        self, stocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compute technical indicators for each stock from OHLCV bars.

        Fetches daily bars and runs compute_features() to add RSI, MACD,
        ATR, SuperTrend, etc. to each stock dict for scoring. Stocks with
        insufficient data are kept but will score neutral on technical.
        """
        indicator_cfg = IndicatorConfig(
            rsi=self.ctx.config.strategy.indicators.rsi,
            macd=self.ctx.config.strategy.indicators.macd,
            bollinger_bands=self.ctx.config.strategy.indicators.bollinger_bands,
            vwap=self.ctx.config.strategy.indicators.vwap,
            atr=self.ctx.config.strategy.indicators.atr,
            volume_profile=self.ctx.config.strategy.indicators.volume_profile,
            obv=self.ctx.config.strategy.indicators.obv,
            supertrend=self.ctx.config.strategy.indicators.supertrend,
        )

        async def _enrich_one(stock: dict[str, Any]) -> dict[str, Any]:
            symbol = stock.get("symbol", "")
            try:
                bars = await self.ctx.db.get_ohlcv(symbol, "daily", days=60)
                if len(bars) < 20:
                    return stock  # not enough data, keep with defaults
                features = compute_features(bars, indicator_cfg)
                if features:
                    stock["rsi"] = features.get("rsi_14")
                    stock["macd_histogram"] = features.get("macd_histogram")
                    stock["supertrend_direction"] = features.get("supertrend_direction")
                    stock["atr_pct"] = features.get("atr_pct", 0.0)
                    # Relative volume: today's volume vs 20-day avg
                    if bars and stock.get("avg_daily_volume"):
                        today_vol = bars[-1].volume if bars[-1].volume else 0
                        avg_vol = stock["avg_daily_volume"]
                        if avg_vol > 0:
                            stock["relative_volume"] = today_vol / avg_vol
            except Exception as e:
                logger.debug("Feature enrichment failed for %s: %s", symbol, e)
            return stock

        # Run enrichment concurrently (bounded to avoid overwhelming DB)
        semaphore = asyncio.Semaphore(20)

        async def _bounded(stock: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await _enrich_one(stock)

        enriched = await asyncio.gather(*[_bounded(s) for s in stocks])
        enriched_count = sum(1 for s in enriched if s.get("rsi") is not None)
        logger.info("Enriched %d/%d stocks with technical indicators", enriched_count, len(stocks))
        return list(enriched)

    def _compute_sub_scores(self, stock: dict[str, Any]) -> dict[str, Any]:
        """Compute normalized [0, 1] sub-scores from raw data."""
        # Technical score: derive from available indicator data (RSI, MACD, SuperTrend)
        tech = self._compute_technical_score(stock)

        # Volume/momentum score: normalize relative to volume threshold
        avg_vol = stock.get("avg_daily_volume") or 0
        min_vol = self.ctx.config.scanning.min_avg_daily_volume
        vol_score = min(avg_vol / (min_vol * 5), 1.0) if min_vol > 0 else 0.5

        # Sentiment score: map sentiment to [0, 1]
        sentiment = stock.get("sentiment")
        sent_conf = stock.get("sentiment_confidence") or 0.5
        if sentiment == "bullish":
            sent_score = 0.5 + sent_conf * 0.5  # 0.5 to 1.0
        elif sentiment == "bearish":
            sent_score = 0.5 - sent_conf * 0.5  # 0.0 to 0.5
        else:
            sent_score = 0.5

        # Fundamental score: inverse PE (lower = better), promoter holding
        pe = stock.get("pe_ratio")
        promoter = stock.get("promoter_holding_pct") or 50.0
        if pe and pe > 0:
            # Normalize PE: PE of 10 → 1.0, PE of 50 → 0.2, PE of 100 → 0.1
            fund_score = min(10.0 / pe, 1.0) * 0.6 + (promoter / 100.0) * 0.4
        else:
            fund_score = (promoter / 100.0) * 0.4 + 0.3  # unknown PE gets neutral

        # Volatility score: favor stocks with sufficient but not extreme daily range
        atr_pct = stock.get("atr_pct") or 0.0
        vol_cfg = self.ctx.config.strategy.volatility
        volatility_score = self._compute_volatility_score(atr_pct, vol_cfg)

        return {
            "technical_score": tech,
            "volume_momentum_score": round(vol_score, 4),
            "news_sentiment_score": round(sent_score, 4),
            "fundamental_score": round(min(fund_score, 1.0), 4),
            "volatility_score": round(volatility_score, 4),
        }

    @staticmethod
    def _compute_technical_score(stock: dict[str, Any]) -> float:
        """Compute a [0, 1] technical score from indicator data if available.

        Uses RSI, MACD signal, and SuperTrend direction when present.
        Falls back to 0.5 (neutral) if no indicator data exists.
        """
        signals: list[float] = []

        # RSI: 30-70 band → map to score (oversold=bullish, overbought=bearish)
        rsi = stock.get("rsi")
        if rsi is not None:
            if rsi < 30:
                signals.append(0.8)   # oversold → bullish
            elif rsi < 45:
                signals.append(0.65)
            elif rsi <= 55:
                signals.append(0.5)   # neutral
            elif rsi <= 70:
                signals.append(0.35)
            else:
                signals.append(0.2)   # overbought → bearish

        # MACD: positive histogram → bullish
        macd_hist = stock.get("macd_histogram")
        if macd_hist is not None:
            signals.append(0.7 if macd_hist > 0 else 0.3)

        # SuperTrend: direction flag
        supertrend_dir = stock.get("supertrend_direction")
        if supertrend_dir is not None:
            signals.append(0.7 if supertrend_dir > 0 else 0.3)

        # Trendlyne momentum score (0-100 from scraper)
        momentum = stock.get("momentum_score")
        if momentum is not None:
            signals.append(min(momentum / 100.0, 1.0))

        if not signals:
            return 0.5  # no data → neutral

        return round(sum(signals) / len(signals), 4)

    @staticmethod
    def _compute_volatility_score(atr_pct: float, vol_cfg: Any) -> float:
        """Compute a [0, 1] volatility score using a bell-curve preference.

        Stocks in the ideal ATR% range (default 1.5%-3%) score highest.
        Below minimum → 0 (won't move enough). Above maximum → penalized.
        """
        if atr_pct <= 0 or atr_pct < vol_cfg.min_atr_pct:
            return 0.0
        if atr_pct > vol_cfg.max_atr_pct:
            return 0.3  # too volatile but still tradeable
        if vol_cfg.ideal_min_atr_pct <= atr_pct <= vol_cfg.ideal_max_atr_pct:
            return 1.0  # sweet spot
        if atr_pct < vol_cfg.ideal_min_atr_pct:
            # Linear ramp from min to ideal_min
            rng = vol_cfg.ideal_min_atr_pct - vol_cfg.min_atr_pct
            return 0.5 + 0.5 * ((atr_pct - vol_cfg.min_atr_pct) / rng) if rng > 0 else 0.5
        # Between ideal_max and max — gradual decline
        rng = vol_cfg.max_atr_pct - vol_cfg.ideal_max_atr_pct
        return 0.3 + 0.7 * ((vol_cfg.max_atr_pct - atr_pct) / rng) if rng > 0 else 0.5

    def _analyze_sector_rotation(self, scored_stocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Group by sector, compute avg scores, identify rotation."""
        sectors: dict[str, list[float]] = {}
        for stock in scored_stocks:
            sector = stock.get("sector") or "Unknown"
            sectors.setdefault(sector, []).append(stock.get("composite_score", 0))

        if not sectors:
            return {"strong": [], "weak": [], "rotation": {}}

        # Compute averages
        sector_avgs = {s: sum(scores) / len(scores) for s, scores in sectors.items()}

        # Classify using percentiles
        all_avgs = sorted(sector_avgs.values())
        if len(all_avgs) >= 4:
            p75 = all_avgs[int(len(all_avgs) * 0.75)]
            p25 = all_avgs[int(len(all_avgs) * 0.25)]
        else:
            p75 = max(all_avgs) if all_avgs else 0
            p25 = min(all_avgs) if all_avgs else 0

        strong = [s for s, avg in sector_avgs.items() if avg >= p75]
        weak = [s for s, avg in sector_avgs.items() if avg <= p25]

        return {
            "strong": strong,
            "weak": weak,
            "rotation": sector_avgs,
        }

    async def _detect_market_regime(self) -> str:
        """Detect the current market regime (bull / bear / range) from index data.

        Fetches recent OHLCV bars for the configured index symbol and classifies
        the regime based on average daily returns and the current price position
        within the recent trading range.

        Returns:
            "bull", "bear", "range", or "unknown" if insufficient data.
        """
        regime_cfg = self.ctx.config.strategy.market_regime
        try:
            bars = await self.ctx.db.get_ohlcv(
                regime_cfg.index_symbol, "daily", days=regime_cfg.lookback_days,
            )
        except Exception as e:
            logger.warning("Market regime detection failed (data fetch): %s", e)
            return "unknown"

        if len(bars) < 10:
            logger.info(
                "Market regime: insufficient data (%d bars < 10) for %s",
                len(bars), regime_cfg.index_symbol,
            )
            return "unknown"

        # Compute daily returns
        returns = []
        for i in range(1, len(bars)):
            prev_close = bars[i - 1].close
            if prev_close > 0:
                returns.append(bars[i].close / prev_close - 1)

        if not returns:
            return "unknown"

        avg_return = sum(returns) / len(returns)

        # Price position within recent range
        closes = [bar.close for bar in bars]
        recent_high = max(closes)
        recent_low = min(closes)
        current = closes[-1]

        if recent_high == recent_low:
            position_in_range = 0.5
        else:
            position_in_range = (current - recent_low) / (recent_high - recent_low)

        # Classify regime
        if avg_return > 0.001 and position_in_range > 0.6:
            regime = "bull"
        elif avg_return < -0.001 and position_in_range < 0.4:
            regime = "bear"
        else:
            regime = "range"

        logger.debug(
            "Market regime: %s (avg_return=%.4f, position_in_range=%.2f, index=%s)",
            regime, avg_return, position_in_range, regime_cfg.index_symbol,
        )
        return regime
