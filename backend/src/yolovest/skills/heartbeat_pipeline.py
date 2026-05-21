"""Skill: heartbeat-pipeline — Run the full heartbeat pipeline on demand.

Wraps HeartbeatOrchestrator.run_heartbeat() so the user can trigger a
full heartbeat (health-check → ingest-data → market-scan →
generate-signals → position-monitor) from Telegram `/run` or the
dashboard Skills page without waiting for the next scheduled cycle.

The orchestrator's existing per-cycle mutex (`_lock.locked()` check
in `run_heartbeat`) handles the "scheduled cycle is already running"
case — a manual trigger that races with the auto-loop returns
`{"skipped": True}` quickly instead of double-running. The
auto-loop's interval timer is not reset; the next scheduled cycle
fires when its current sleep expires, which the orchestrator's
mutex serialises behind any manual run still in flight.
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class HeartbeatPipelineSkill(SkillBase):
    name = "heartbeat-pipeline"
    description = (
        "Run the full heartbeat pipeline on demand — runs in order: "
        "expire-pending-trades, reprice-pending-trades, health-check, "
        "ingest-data, market-scan, generate-signals (+ per-signal risk-check, "
        "llm-review, trade-execute or manual-approval queue), position-monitor."
    )
    trigger = SkillTrigger.MANUAL
    schedule = None

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        orchestrator = getattr(self.ctx, "orchestrator", None)
        if orchestrator is None:
            return SkillResult(
                success=False, skill_name=self.name,
                error="Orchestrator not wired to ctx — boot order issue",
            )
        results = await orchestrator.run_heartbeat(source="manual")
        if results.get("skipped"):
            logger.info(
                "heartbeat-pipeline: scheduled cycle already in progress — "
                "manual run skipped (consecutive_skips=%d)",
                results.get("consecutive_skips", 0),
            )
            return SkillResult(
                success=True, skill_name=self.name,
                data={"skipped": True, **results},
            )
        # Summarise pass/fail counts for the audit-log entry.
        succeeded = sum(
            1 for v in results.values()
            if hasattr(v, "success") and v.success
        )
        total = sum(1 for v in results.values() if hasattr(v, "success"))
        return SkillResult(
            success=True, skill_name=self.name,
            data={"skills_succeeded": succeeded, "skills_total": total},
        )
