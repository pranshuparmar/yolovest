"""CRON scheduler for YoloVest.

Discovers CRON-triggered skills from the registry and fires them
on their defined schedules. Runs as a background async loop alongside
the heartbeat orchestrator.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from croniter import croniter

from yolovest.context import AppContext
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)

# How often (seconds) the scheduler checks for due skills.
_CHECK_INTERVAL_SEC = 30

# Hard ceiling on how long a single CRON skill may run before the scheduler
# abandons it. The scheduler fires skills inline on one loop, so a hung skill
# (e.g. stuck on a network call) would otherwise block the loop and starve
# every other scheduled skill indefinitely. Set generously so genuinely
# long-running skills (model-retrain, ingest-universe) finish normally —
# only a true hang trips it.
_SKILL_TIMEOUT_SEC = 3600.0

# system_state key holding the JSON list of skill names whose CRON
# schedule is currently paused via the dashboard. Read fresh every tick
# so a Start/Stop toggle takes effect within one check interval.
DISABLED_SCHEDULES_KEY = "disabled_schedules"


async def load_disabled_schedules(db: Any) -> set[str]:
    """Return the set of skill names whose schedule is paused.

    Stored as a JSON list under ``system_state.disabled_schedules``.
    Tolerant of a missing / malformed value (returns an empty set).
    """
    try:
        raw = await db.get_system_state(DISABLED_SCHEDULES_KEY)
        if not raw:
            return set()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {str(x) for x in parsed}
    except Exception:
        logger.debug("Failed to load disabled schedules", exc_info=True)
    return set()


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
    def _now() -> Any:
        """Return the current time in IST. Extracted for easy patching in tests."""
        return now_ist()

    async def _check_and_fire(self) -> None:
        """One iteration: check every CRON skill and fire those that are due."""
        now = self._now()

        # Skip holidays entirely
        if self._ctx.market_hours.is_holiday(now.date()):
            return

        # Schedules the user has paused via the dashboard (read fresh each
        # tick so Start/Stop takes effect without a restart).
        disabled = await load_disabled_schedules(self._ctx.db)

        for name, skill in self._cron_skills.items():
            if name in disabled:
                logger.debug("CRON skill '%s' schedule is paused — skipping", name)
                continue

            # Dynamic schedules: re-read the LIVE schedule each tick via
            # compute_schedule() so a schedule changed in the Settings UI
            # (which hot-replaces ctx.config) is honoured without restart.
            schedule = skill.compute_schedule()
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

            # Fire the skill — bounded so one hung skill can't freeze the
            # whole scheduler loop and starve every other CRON skill.
            try:
                result = await asyncio.wait_for(
                    self._run_skill(name, skill), timeout=_SKILL_TIMEOUT_SEC,
                )
            except TimeoutError:
                logger.error(
                    "CRON skill '%s' exceeded %.0fs timeout — abandoned this "
                    "run so other scheduled skills aren't starved",
                    name, _SKILL_TIMEOUT_SEC,
                )
                # Record the attempt so it retries at its next scheduled time
                # rather than re-firing into the same hang every tick.
                self._last_run[name] = now
                continue
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
