"""Skill: auto-score — daily post-close scoring of dry-runs + predictions.

Trigger: CRON — config ``scoring.auto_score_cron`` (default 16:45 IST,
weekdays), after the day's daily bars are ingested.

Each pass scores every dry-run that still has unscored signals and every
elapsed prediction against the actuals on its OWN target date (path-aware
over the holding window), not against today. Idempotent and partial:
already-scored rows are skipped and signals whose horizon hasn't fully
elapsed are left pending for a later run.
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class AutoScoreSkill(SkillBase):
    name = "auto-score"
    description = "Score dry-runs and predictions against their target-date actuals"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        return self.ctx.config.scoring.auto_score_cron

    def should_run(self) -> bool:
        return bool(self.ctx.config.scoring.auto_score_enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        # --- Dry-runs ---------------------------------------------------
        run_ids = await self.ctx.db.get_dry_run_ids_needing_scoring()
        dry_scored = dry_pending = runs_touched = 0
        for run_id in run_ids:
            try:
                res = await self.ctx.db.score_dry_run(run_id)
            except Exception:
                logger.exception("auto-score: scoring dry-run %s failed", run_id)
                continue
            dry_scored += int(res.get("scored", 0) or 0)
            dry_pending += int(res.get("pending", 0) or 0)
            if res.get("scored"):
                runs_touched += 1

        # --- Predictions ------------------------------------------------
        # Reuse predict-track's scorer (now target-date / end-date based)
        # so heartbeat and post-close scoring stay identical.
        pred_scored = 0
        try:
            from yolovest.skills.predict_track import PredictTrackSkill

            pres = await PredictTrackSkill(self.ctx).execute(mode="score")
            pred_scored = int((pres.data or {}).get("predictions_scored", 0) or 0)
        except Exception:
            logger.exception("auto-score: prediction scoring failed")

        logger.info(
            "auto-score: dry-runs scored=%d pending=%d across %d/%d runs; "
            "predictions scored=%d",
            dry_scored, dry_pending, runs_touched, len(run_ids), pred_scored,
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "dry_run_signals_scored": dry_scored,
                "dry_run_signals_pending": dry_pending,
                "dry_runs_touched": runs_touched,
                "dry_runs_with_pending": len(run_ids),
                "predictions_scored": pred_scored,
            },
        )
