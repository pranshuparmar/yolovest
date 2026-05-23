"""Skill: ingest-vix — daily India VIX ingest for the ML regime feature.

Trigger: CRON at 16:00 IST (30 min after market close).
Pipeline position: Offline. Independent of the heartbeat pipeline.

VIX is a single broadcast series — every per-(symbol, date) training
sample on a given date sees the same VIX context. We store it in the
shared `ohlcv` table under symbol `INDIA VIX` so existing readers (DB
backups, dashboard storage stats) work unchanged; `data/vix_features.py`
reads the timeline via `db.get_vix_timeline` to derive level / 5d-change
/ 20d-zscore features.

On first run, backfills 365d of history so `model_retrain` has enough
context for the trailing-20d z-score on every sample in its window.
Subsequent runs only need the latest day — but we still pull 30d as a
cheap reconciliation guard against missed weekend ingests.
"""
from __future__ import annotations

import logging
from typing import Any

from yolovest.data.vix_provider import VIX_SYMBOL, fetch_vix_history
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class IngestVixSkill(SkillBase):
    name = "ingest-vix"
    description = "Daily India VIX ingest for the ML volatility-regime feature"
    trigger = SkillTrigger.CRON
    # 16:00 IST Mon-Fri — after NSE close (15:30) but before report-generate
    # (16:00) so dashboards already see today's regime value. The seed
    # parameter on the orchestrator's scheduler lets this share a 16:00
    # slot with report-generate without serialization concerns; both are
    # read-mostly + idempotent.
    schedule = "0 16 * * 1-5"

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        # On a cold table, backfill enough history to cover the model's
        # training window so VIX features aren't zero for ~80% of
        # samples on a multi-year retrain (the vix_* triad is a
        # broadcast feature — every (symbol, date) sample on a date
        # without a VIX bar gets neutral 0.0). Match the daily-OHLCV
        # training window (+ a 60-day pad for the trailing-20d z-score).
        # Once the table has data, the daily 30-day pull is enough as a
        # missed-day reconciliation guard.
        try:
            existing = await self.ctx.db.get_vix_timeline()
        except Exception:
            logger.exception("ingest-vix: get_vix_timeline failed")
            existing = []

        cold_start_days = max(
            365,
            int(self.ctx.config.retraining.max_training_days) + 60,
        )
        days = 30 if len(existing) > 30 else cold_start_days
        bars = await fetch_vix_history(days=days)
        if not bars:
            logger.warning("ingest-vix: no bars returned from yfinance")
            return SkillResult(
                success=False,
                skill_name=self.name,
                error="no_bars",
                data={"days_requested": days},
            )

        inserted = await self.ctx.db.upsert_ohlcv(
            symbol=VIX_SYMBOL,
            interval="daily",
            bars=bars,
            source="yfinance_vix",
        )
        logger.info(
            "ingest-vix: upserted %d VIX bars (latest close=%.2f)",
            inserted, bars[-1].close,
        )
        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "bars_upserted": inserted,
                "latest_close": bars[-1].close,
                "latest_date": bars[-1].timestamp.date().isoformat(),
            },
        )
