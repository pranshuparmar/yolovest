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

        # Signal-class collapse check — catches "model produces zero
        # BUYs for a week" or "every signal is HOLD." Independent of
        # the win-rate drift check above; either can fire on its own.
        class_warnings: list[str] = []
        class_counts: dict[str, Any] = {}
        try:
            class_counts = await self.ctx.db.get_signal_class_counts(
                days=7, mode=mode,
            )
            total = int(class_counts.get("total", 0) or 0)
            if total >= 30:  # below this it's too noisy to call collapse
                for key in ("BUY", "SELL", "HOLD"):
                    n = int(class_counts.get(key, 0) or 0)
                    pct = (n / total) * 100 if total else 0.0
                    if n == 0:
                        class_warnings.append(
                            f"{key}: 0 of {total} signals in last 7d "
                            "(model never picks this class).",
                        )
                    elif pct > 95:
                        class_warnings.append(
                            f"{key}: {n}/{total} signals ({pct:.0f}%) "
                            "in last 7d — class imbalance suggests a "
                            "single dominant disposition.",
                        )
        except Exception:
            logger.debug("drift-watch: class-count lookup failed", exc_info=True)

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

        if warning or class_warnings:
            sections: list[str] = []
            if warning:
                sections.append(f"Win-rate drift:\n{warning}")
            if class_warnings:
                sections.append(
                    "Signal-class imbalance:\n  " + "\n  ".join(class_warnings),
                )
            if digest_lines:
                sections.append("\n".join(digest_lines))

            # Hard suspension of signal-gen when opt-in is enabled and
            # the model is materially decayed (win-rate drop OR class
            # collapse). Cleared by the next successful model retrain,
            # or manually via the dashboard.
            suspended = False
            if self.ctx.config.risk.drift_auto_suspend_enabled:
                reason_parts = []
                if warning:
                    reason_parts.append("win_rate_decay")
                if class_warnings:
                    reason_parts.append("class_collapse")
                reason = "+".join(reason_parts) or "drift_detected"
                try:
                    await self.ctx.db.set_system_state(
                        "signal_gen_suspended_by_drift", reason,
                    )
                    suspended = True
                    sections.append(
                        "Signal generation has been AUTO-SUSPENDED until "
                        "the next successful model-retrain (or manual "
                        "clear from the dashboard).",
                    )
                except Exception:
                    logger.warning(
                        "drift-watch: failed to set suspension flag",
                        exc_info=True,
                    )

            sections.append(
                "Review the Model Drift page; consider /run model-retrain "
                "if the decay is recent and persistent.",
            )
            msg = (
                f"WARNING: Model health alert ({mode} mode)\n\n"
                + "\n\n".join(sections)
            )
            try:
                await self.ctx.notify.send(msg, alert_type="errors")
            except Exception:
                logger.warning("drift-watch: notify.send failed", exc_info=True)
            if warning:
                logger.warning("drift-watch: %s", warning)
            for w in class_warnings:
                logger.warning("drift-watch class collapse: %s", w)
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={
                    "alerted": True,
                    "warning": warning,
                    "class_warnings": class_warnings,
                    "class_counts": class_counts,
                    "digest": digest_lines,
                    "mode": mode,
                    "signal_gen_suspended": suspended,
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
                "class_warnings": class_warnings,
                "class_counts": class_counts,
                "digest": digest_lines,
                "mode": mode,
            },
        )
