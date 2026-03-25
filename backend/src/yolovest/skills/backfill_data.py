"""Skill: backfill-data — Bulk historical OHLCV backfill.

Trigger: MANUAL — run via dashboard or Telegram to seed historical data.

Fetches N days of daily OHLCV for all watchlist symbols using the existing
market data provider chain (jugaad → yfinance fallback). Upserts into DB
with deduplication, so it's safe to run repeatedly.

Typical use: bootstrapping a fresh install so model-retrain has enough bars.
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class BackfillDataSkill(SkillBase):
    name = "backfill-data"
    description = "Bulk-fetch historical OHLCV data for model training"
    trigger = SkillTrigger.MANUAL
    schedule = None

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        days = int(kwargs.get("days", 365))
        symbols = kwargs.get("symbols", self.ctx.config.scanning.seed_symbols)

        results: dict[str, Any] = {
            "days_requested": days,
            "symbols_processed": 0,
            "total_bars_stored": 0,
            "errors": [],
        }

        for symbol in symbols:
            try:
                bars = await self.ctx.market_data.get_ohlcv(symbol, "daily", days=days)
                if bars:
                    count = await self.ctx.db.upsert_ohlcv(symbol, "daily", bars, "backfill")
                    results["total_bars_stored"] += count
                    logger.info("Backfilled %s: %d bars stored", symbol, count)
                else:
                    logger.warning("No data returned for %s", symbol)
                results["symbols_processed"] += 1
            except Exception as e:
                results["errors"].append(f"{symbol}: {e}")
                logger.warning("Backfill failed for %s: %s", symbol, e)

        all_failed = results["symbols_processed"] == 0 and len(results["errors"]) > 0
        return SkillResult(
            success=not all_failed,
            skill_name=self.name,
            data=results,
        )
