"""Skill: expire-pending-trades — Sweep abandoned manual-approval rows.

Pending trades that sit in the queue past `execution.pending_expiry_minutes`
are flipped to `status='expired'`. Risk-check counts pending notional and
pending count toward `max_portfolio_exposure_pct`, `max_open_positions`,
and `max_trades_per_day`, so a forgotten pending silently locks those
budgets and chokes off the rest of the day's signals — this skill
releases them.

Runs as the first step of the heartbeat pipeline (see orchestrator
`_execute_pipeline`) and is exposed as a standalone manual skill so
the user can also sweep on demand from Telegram `/run
expire-pending-trades` or the dashboard Skills page.
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class ExpirePendingSkill(SkillBase):
    name = "expire-pending-trades"
    description = (
        "Auto-expire pending manual-approval trades older than "
        "`execution.pending_expiry_minutes`. Frees the exposure / "
        "max-open-positions / max-trades-per-day budget those rows "
        "were holding. Also runs as the first step of every heartbeat."
    )
    trigger = SkillTrigger.MANUAL
    schedule = None

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        expiry_min = int(self.ctx.config.execution.pending_expiry_minutes)
        expired_count = await self.ctx.db.expire_pending_trades(
            max_age_minutes=expiry_min,
        )
        if expired_count:
            logger.info(
                "expire-pending-trades: expired %d row(s) older than %dmin",
                expired_count, expiry_min,
            )
        return SkillResult(
            success=True, skill_name=self.name,
            data={"expired_count": expired_count, "max_age_minutes": expiry_min},
        )
