"""Skill: drift-watch — push model decay alerts to Telegram.

Trigger: CRON — daily at 16:30 IST (after report-generate at 16:00).
Pipeline position: Post-market, after EOD reporting.

With LLM review out of the loop, silent model decay is the largest
unattended risk for autonomous live trading. The drift dashboard
(`/api/model-drift`) already computes a `warning` field when a
model's realised win-rate drops more than 15 percentage points
over the trailing 7 days vs the prior 7 — but that warning is only
visible if the user opens the page. This skill makes the same
check, pushes the warning to Telegram when present, and writes an
audit-log entry either way so a future post-mortem can see when
drift was last evaluated.

Mode-scoped via ctx.config.mode (paper and live are evaluated
separately by the user — switching mode at runtime will evaluate
the active mode at the next 16:30 fire).
"""

from __future__ import annotations

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class DriftWatchSkill(SkillBase):
    name = "drift-watch"
    description = "Daily model-drift check; alert on calibration decay"
    trigger = SkillTrigger.CRON
    # 30 minutes after the 16:00 daily report so all of today's
    # closed-trade outcomes have been scored.
    schedule = "30 16 * * 1-5"

    def should_run(self) -> bool:
        # Heartbeat / report dependencies all need market hours
        # to have completed; the cron itself only fires on weekdays
        # and only after market close, so should_run defaults True.
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        mode = self.ctx.config.mode
        try:
            stats = await self.ctx.db.get_model_drift_stats(days=14, mode=mode)
        except Exception as e:
            logger.exception("drift-watch: get_model_drift_stats failed")
            return SkillResult(
                success=False, skill_name=self.name, error=str(e),
            )

        warning = stats.get("warning")
        versions = stats.get("model_versions", [])

        # Build a short per-model digest for the alert + audit.
        digest_lines: list[str] = []
        for v in versions:
            mt = v.get("model_type")
            ver = v.get("version")
            by_day = v.get("by_day", [])
            recent = by_day[-7:] if len(by_day) >= 7 else by_day
            if not recent:
                continue
            samples = sum(r.get("sample_size", 0) for r in recent)
            if samples == 0:
                continue
            realised_avg = sum(
                r.get("realised_win_rate", 0.0) * r.get("sample_size", 0)
                for r in recent
            ) / samples
            digest_lines.append(
                f"  {mt} ({ver}): realised win-rate {realised_avg:.0%} "
                f"over {samples} scored predictions in last 7d",
            )

        if warning:
            msg = (
                f"WARNING: Model drift detected ({mode} mode)\n"
                f"{warning}\n"
                + ("\n".join(digest_lines) if digest_lines else "")
                + "\n\nReview the Model Drift page; consider /run model-retrain "
                "if the decay is recent and persistent."
            )
            try:
                await self.ctx.notify.send(msg, alert_type="errors")
            except Exception:
                logger.warning("drift-watch: notify.send failed", exc_info=True)
            logger.warning("drift-watch: %s", warning)
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={
                    "alerted": True,
                    "warning": warning,
                    "digest": digest_lines,
                    "mode": mode,
                },
            )

        # No drift — silent success, but emit a digest log line so the
        # daily check is visible in the audit trail / heartbeat log.
        logger.info(
            "drift-watch: no drift detected (%s mode)%s",
            mode,
            " | " + " | ".join(digest_lines) if digest_lines else "",
        )
        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "alerted": False,
                "warning": None,
                "digest": digest_lines,
                "mode": mode,
            },
        )
