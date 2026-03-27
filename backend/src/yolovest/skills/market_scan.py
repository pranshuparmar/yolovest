"""Skill: market-scan — Dynamic stock scanning and ranking.

Trigger: HEARTBEAT during market hours (refreshes each heartbeat)
Pipeline position: After ingest-data/ingest-premarket, before generate-signals.

Flow:
1. Load latest OHLCV, volume, news sentiment, and fundamental data from DB
2. Scan NSE universe — apply volume filter (scanning.min_avg_daily_volume)
3. Filter out: illiquid stocks, F&O ban list, pending corporate actions
4. Score each stock using configurable weighted algorithm (scanning.weights):
   - Technical score (default 40%): trend strength, breakout patterns
   - Volume/momentum (default 25%): relative volume, delivery %, momentum indicators
   - News sentiment (default 20%): Gemini sentiment from ingest-data
   - Fundamental quality (default 15%): PE, debt ratio, promoter holding
5. Rank and shortlist top N stocks (scanning.shortlist_size)
6. Track sector rotation — flag sectors showing strength/weakness
7. Use Gemini to cross-validate shortlist against market narrative
8. Update dynamic watchlist in DB
"""

import logging
from typing import Any

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
        universe = await self.ctx.db.get_nse_universe()
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

        # Step 4: Compute sub-scores and weighted composite
        scored = []
        for stock in filtered:
            sub = self._compute_sub_scores(stock)
            composite = (
                sub["technical_score"] * cfg.weights.technical
                + sub["volume_momentum_score"] * cfg.weights.volume_momentum
                + sub["news_sentiment_score"] * cfg.weights.news_sentiment
                + sub["fundamental_score"] * cfg.weights.fundamental
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
        if self.ctx.config.risk.llm_review_enabled and shortlist:
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
            "market-scan: universe=%d, liquid=%d, shortlisted=%d — top: %s | sectors strong=%s weak=%s",
            len(universe), len(filtered), len(shortlist),
            [s["symbol"] for s in shortlist[:5]],
            sector_analysis.get("strong", []),
            sector_analysis.get("weak", []),
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "universe_size": len(universe),
                "after_filters": len(filtered),
                "shortlist_size": len(shortlist),
                "top_stocks": [s["symbol"] for s in shortlist[:5]],
                "strong_sectors": sector_analysis.get("strong", []),
                "weak_sectors": sector_analysis.get("weak", []),
            },
        )

    def _apply_exclusion_filters(self, stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove F&O banned stocks, pending corporate actions, etc."""
        # F&O ban list and corp actions would be fetched from NSE in production.
        # For now, pass through — the volume filter already removes illiquid stocks.
        return stocks

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

        return {
            "technical_score": tech,
            "volume_momentum_score": round(vol_score, 4),
            "news_sentiment_score": round(sent_score, 4),
            "fundamental_score": round(min(fund_score, 1.0), 4),
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
