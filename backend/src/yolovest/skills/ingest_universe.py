"""Skill: ingest-universe — Populate OHLCV for the full NSE scanning universe.

Trigger: CRON (daily before market open) + MANUAL
Pipeline position: Runs before heartbeat cycle so market-scan has a broad pool.

Fetches daily OHLCV (lightweight, no news/sentiment/fundamentals) for all
symbols in the configured universe (nifty50 or nifty500). This gives
market-scan a real pool to rank from, instead of being limited to seed_symbols.

The deep pass (news, sentiment, fundamentals) is handled by ingest-data,
which runs on the watchlist symbols shortlisted by market-scan.
"""

import logging
from typing import Any

from yolovest.data.nse_symbols import get_universe_symbols
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class IngestUniverseSkill(SkillBase):
    name = "ingest-universe"
    description = "Fetch daily OHLCV for the full NSE scanning universe"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.ctx.config.scanning.universe_cron

    def should_run(self) -> bool:
        # Don't run during market hours — avoid competing with heartbeat ingestion
        return not self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        universe = kwargs.get("universe", self.ctx.config.scanning.universe)
        days = int(kwargs.get("days", 365))

        symbols = get_universe_symbols(universe)
        logger.info(
            "Ingesting universe '%s': %d symbols, %d days",
            universe, len(symbols), days,
        )

        results: dict[str, Any] = {
            "universe": universe,
            "total_symbols": len(symbols),
            "symbols_ingested": 0,
            "total_bars_stored": 0,
            "errors": [],
            "cache_hits": 0,
        }

        for idx, symbol in enumerate(symbols):
            try:
                bars = await self.ctx.market_data.get_ohlcv(
                    symbol, "daily", days=days, skip_stale_check=True,
                )
                if bars:
                    count = await self.ctx.db.upsert_ohlcv(
                        symbol, "daily", bars, "universe"
                    )
                    results["total_bars_stored"] += count
                    results["symbols_ingested"] += 1
                else:
                    logger.debug("No data returned for %s", symbol)

                # Broadcast progress every 10 symbols
                if (idx + 1) % 10 == 0 or idx + 1 == len(symbols):
                    await self.broadcast("ingest_progress", {
                        "skill": "ingest-universe",
                        "current": idx + 1,
                        "total": len(symbols),
                        "symbol": symbol,
                    })
            except Exception as e:
                results["errors"].append(f"{symbol}: {e}")
                logger.debug("Universe fetch failed for %s: %s", symbol, e)

        error_count = len(results["errors"])
        if error_count:
            logger.warning(
                "Universe ingestion: %d/%d symbols failed",
                error_count, len(symbols),
            )

        return SkillResult(
            success=results["symbols_ingested"] > 0,
            skill_name=self.name,
            data=results,
        )
