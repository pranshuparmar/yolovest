"""Skill: market-scan — Dynamic stock scanning and ranking.

Covers: FR-3.1 to FR-3.6
Trigger: CRON at 8:45 AM (pre-market scan) + HEARTBEAT during market hours (watchlist refresh)
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
6. Track sector rotation — flag sectors showing strength/weakness (FR-3.5)
7. Use Gemini to cross-validate shortlist against market narrative (FR-3.6)
8. Update dynamic watchlist in DB (FR-3.4)
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class MarketScanSkill(SkillBase):
    name = "market-scan"
    description = "Scan NSE universe, rank stocks, produce watchlist"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        # Pre-market scan at 8:45 AM + periodic refresh during market hours
        return self.ctx.market_hours.is_premarket_window() or self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        cfg = self.ctx.config.scanning

        # Step 1-2: Load universe, apply volume filter
        universe = await self.ctx.db.get_nse_universe()
        liquid = [s for s in universe if s["avg_daily_volume"] >= cfg.min_avg_daily_volume]

        # Step 3: Filter out banned / corporate action stocks
        filtered = await self._apply_exclusion_filters(liquid)

        # Step 4: Score with configurable weights
        scored = []
        for stock in filtered:
            score = (
                stock["technical_score"] * cfg.weights.technical
                + stock["volume_momentum_score"] * cfg.weights.volume_momentum
                + stock["news_sentiment_score"] * cfg.weights.news_sentiment
                + stock["fundamental_score"] * cfg.weights.fundamental
            )
            scored.append({**stock, "composite_score": score})

        # Step 5: Rank and shortlist
        scored.sort(key=lambda s: s["composite_score"], reverse=True)
        shortlist = scored[: cfg.shortlist_size]

        # Step 6: Sector rotation analysis
        sector_analysis = self._analyze_sector_rotation(scored)

        # Step 7: Gemini cross-validation
        if self.ctx.config.risk.llm_review_enabled:
            llm_validation = await self.ctx.llm.validate_watchlist(
                shortlist=shortlist,
                sector_analysis=sector_analysis,
                premarket_context=await self.ctx.db.get_latest_premarket(),
            )
            # LLM can reorder or flag stocks
            shortlist = llm_validation.get("adjusted_shortlist", shortlist)

        # Step 8: Persist watchlist
        await self.ctx.db.upsert_watchlist(shortlist)

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

    async def _apply_exclusion_filters(self, stocks: list[dict]) -> list[dict]:
        """Remove F&O banned stocks, pending corporate actions, etc."""
        raise NotImplementedError

    def _analyze_sector_rotation(self, scored_stocks: list[dict]) -> dict:
        """Group by sector, compute avg scores, identify rotation. FR-3.5."""
        raise NotImplementedError
