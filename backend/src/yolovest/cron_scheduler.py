"""CRON scheduler for YoloVest.

Discovers CRON-triggered skills from the registry and fires them
on their defined schedules. Runs as a background async loop alongside
the heartbeat orchestrator.
"""

import asyncio
import logging
from datetime import datetime

from croniter import croniter

from yolovest.timezone import now_ist

from yolovest.context import AppContext
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)

# How often (seconds) the scheduler checks for due skills.
_CHECK_INTERVAL_SEC = 30


class CronScheduler:
    """Evaluate cron expressions and run CRON-triggered skills on schedule.

    Design:
    - Loops every ``_CHECK_INTERVAL_SEC`` seconds.
    - For each CRON skill that has a valid ``schedule`` expression,
      uses ``croniter`` to determine whether the skill was due since the
      last check.
    - Tracks ``_last_run`` per skill to prevent double-firing within
      the same cron window.
    - Skips holidays via ``MarketHoursChecker.is_holiday()``.
    - Logs audit entries for every invocation via ``ctx.db.log_audit()``.
    """

    def __init__(
        self,
        ctx: AppContext,
        skills: dict[str, SkillBase],
    ) -> None:
        self._ctx = ctx
        self._skills = skills
        self._running = False
        # last successful fire time per skill name
        self._last_run: dict[str, datetime] = {}
        # Cache of discovered CRON skills (name -> skill)
        self._cron_skills: dict[str, SkillBase] = {}
        self._discover_cron_skills()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_cron_skills(self) -> None:
        """Find all skills with ``trigger == SkillTrigger.CRON``."""
        for name, skill in self._skills.items():
            if skill.trigger == SkillTrigger.CRON:
                if skill.schedule is None:
                    logger.warning(
                        "CRON skill '%s' has schedule=None — it will be skipped "
                        "until a schedule is set",
                        name,
                    )
                self._cron_skills[name] = skill

        logger.info(
            "CronScheduler discovered %d CRON skills: %s",
            len(self._cron_skills),
            list(self._cron_skills.keys()),
        )

    @property
    def cron_skills(self) -> dict[str, SkillBase]:
        """Discovered CRON skills (read-only view for testing)."""
        return dict(self._cron_skills)

    # ------------------------------------------------------------------
    # Schedule evaluation
    # ------------------------------------------------------------------

    def _is_due(self, skill_name: str, schedule: str, now: datetime) -> bool:
        """Return True if *schedule* has a fire time between the last run and *now*.

        Uses ``croniter`` to step backwards from *now* and check whether
        the most recent fire time is after the last recorded run.
        """
        cron = croniter(schedule, now)
        prev_fire: datetime = cron.get_prev(datetime)

        last = self._last_run.get(skill_name)
        if last is None:
            # Never run before — fire if the previous fire time is within
            # the current check window (i.e. within the last interval).
            return (now - prev_fire).total_seconds() < _CHECK_INTERVAL_SEC

        return prev_fire > last

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _run_skill(self, name: str, skill: SkillBase) -> SkillResult:
        """Execute a single CRON skill via ``safe_execute``."""
        if not skill.should_run():
            logger.debug(
                "CRON skill '%s' should_run() returned False — skipping", name,
            )
            return SkillResult(
                success=True,
                skill_name=name,
                data={"skipped": True, "reason": "should_run() returned False"},
            )

        logger.info("CRON firing skill: %s", name)
        result = await skill.safe_execute()
        logger.info(
            "CRON skill %s completed: success=%s, duration=%.1fms",
            name,
            result.success,
            result.duration_ms,
        )
        return result

    @staticmethod
    def _now():
        """Return the current time in IST. Extracted for easy patching in tests."""
        return now_ist()

    async def _check_and_fire(self) -> None:
        """One iteration: check every CRON skill and fire those that are due."""
        now = self._now()

        # Skip holidays entirely
        if self._ctx.market_hours.is_holiday(now.date()):
            return

        for name, skill in self._cron_skills.items():
            # Dynamic schedules: re-read schedule each tick
            schedule = skill.schedule
            if schedule is None:
                continue

            try:
                if not self._is_due(name, schedule, now):
                    continue
            except (ValueError, KeyError) as exc:
                logger.error(
                    "Invalid cron expression for skill '%s': %s — skipping",
                    name,
                    exc,
                )
                continue

            # Fire the skill
            result = await self._run_skill(name, skill)
            # Only mark as run if the skill actually executed (not skipped)
            skipped = result.data.get("skipped", False) if result.data else False
            if not skipped:
                self._last_run[name] = now

            # Audit log (best-effort)
            try:
                await self._ctx.db.log_audit(
                    action_type="cron_invocation",
                    skill_name=name,
                    input_summary={"schedule": schedule},
                    output_summary={
                        "success": result.success,
                        "duration_ms": result.duration_ms,
                        "error": result.error,
                    },
                    duration_ms=result.duration_ms,
                )
            except Exception:
                logger.debug("Failed to log audit for CRON skill %s", name, exc_info=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the CRON loop — check every ``_CHECK_INTERVAL_SEC`` for due skills."""
        self._running = True
        logger.info("CronScheduler started (interval=%ds)", _CHECK_INTERVAL_SEC)

        while self._running:
            try:
                await self._check_and_fire()
            except Exception:
                logger.exception("Unhandled error in CRON scheduler tick")

            if self._running:
                await asyncio.sleep(_CHECK_INTERVAL_SEC)

    def stop(self) -> None:
        """Signal the CRON loop to stop."""
        self._running = False
        logger.info("CronScheduler stop requested")
