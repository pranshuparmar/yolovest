"""Skill: ingest-fno — daily F&O option-chain aggregates for the ML model.

Trigger: CRON at 18:30 IST.
Pipeline position: Offline. Independent of the heartbeat pipeline.

NSE settles official EOD OI ~18:00 IST; running at 18:30 gives us the
authoritative day-close values. PCR / OI / futures aggregates feed five
derived features in `data/fno_features.py`:
  pcr_oi, pcr_volume, oi_change_pct_1d, oi_buildup_signal, is_fno_stock

These are forward-only: Kite doesn't expose historical option-chain
snapshots, so backfill is impossible. The model sees neutral values for
every historical training row until enough days have accumulated.

Gates on `market_data.kite_data_enabled` AND broker authentication —
the F&O data is Kite-only (jugaad / yfinance / tvDatafeed don't expose
a clean option-chain endpoint we can aggregate).
"""
from __future__ import annotations

import logging
from typing import Any

from yolovest.data.fno_provider import fetch_fno_aggregates
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)


class IngestFnoSkill(SkillBase):
    name = "ingest-fno"
    description = "Daily F&O option-chain aggregates for ML derivatives features"
    trigger = SkillTrigger.CRON
    # 18:30 IST Mon-Fri — after NSE EOD settlement (~18:00) so OI values
    # are the authoritative close-of-day numbers, not a mid-session
    # snapshot. Independent of other 16:00 / 16:30 crons.
    schedule = "30 18 * * 1-5"

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        if not self.ctx.config.market_data.kite_data_enabled:
            logger.info("ingest-fno: kite_data_enabled is false, skipping")
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"reason": "kite_data_disabled"},
            )

        # Reach the active kite client through the broker — it's the only
        # subsystem guaranteed to hold a live access_token (the data
        # provider's token is synced from the broker after /auth, but the
        # broker is the source of truth).
        kite = getattr(self.ctx.broker, "_kite", None)
        access_token = getattr(self.ctx.broker, "_access_token", None)
        if kite is None or not access_token or access_token == "paper_token":
            logger.warning("ingest-fno: broker not authenticated, skipping")
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"reason": "broker_not_authenticated"},
            )

        try:
            aggregates = await fetch_fno_aggregates(kite)
        except Exception as e:
            logger.exception("ingest-fno: fetch_fno_aggregates raised")
            return SkillResult(
                success=False, skill_name=self.name, error=str(e),
            )

        if not aggregates:
            return SkillResult(
                success=False,
                skill_name=self.name,
                error="no_aggregates",
                data={"reason": "empty_chain"},
            )

        date_str = now_ist().date().isoformat()
        inserted = await self.ctx.db.upsert_fno_daily(date_str, aggregates)
        logger.info(
            "ingest-fno: upserted %d F&O underlyings for %s",
            inserted, date_str,
        )
        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"date": date_str, "underlyings": inserted},
        )
