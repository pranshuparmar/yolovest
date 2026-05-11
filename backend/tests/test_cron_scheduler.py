"""Tests for CronScheduler in cron_scheduler.py.

Tests CRON skill discovery, schedule evaluation, double-fire prevention,
should_run() gating, holiday skipping, and audit logging.
"""

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.cron_scheduler import CronScheduler
from yolovest.events import EventBus
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

# ---------------------------------------------------------------------------
# Stub skills
# ---------------------------------------------------------------------------


class CronStubSkill(SkillBase):
    """A configurable stub CRON skill for testing."""

    name = "cron-stub"
    description = "Test CRON stub"
    trigger = SkillTrigger.CRON
    schedule = "*/5 * * * *"  # every 5 minutes

    def __init__(
        self,
        context: Any,
        *,
        succeed: bool = True,
        should_run_val: bool = True,
    ) -> None:
        super().__init__(context)
        self._succeed = succeed
        self._should_run_val = should_run_val

    def should_run(self) -> bool:
        return self._should_run_val

    async def execute(self, **kwargs: Any) -> SkillResult:
        return SkillResult(
            success=self._succeed,
            skill_name=self.name,
            data={"executed": True},
        )


class HeartbeatStubSkill(SkillBase):
    """A stub with HEARTBEAT trigger -- should NOT be picked up by CronScheduler."""

    name = "heartbeat-stub"
    description = "Heartbeat stub"
    trigger = SkillTrigger.HEARTBEAT

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        return SkillResult(success=True, skill_name=self.name, data={})


def _make_cron_stub(
    ctx: Any,
    name: str,
    schedule: str | None = "*/5 * * * *",
    succeed: bool = True,
    should_run_val: bool = True,
) -> CronStubSkill:
    skill = CronStubSkill(ctx, succeed=succeed, should_run_val=should_run_val)
    skill.name = name
    skill.schedule = schedule
    return skill


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cron_context(
    sample_config, mock_broker, mock_llm, mock_db, mock_market_data, mock_notify,
):
    """AppContext for CronScheduler tests."""
    market_hours = MarketHoursChecker(sample_config)
    return AppContext(
        config=sample_config,
        db=mock_db,
        broker=mock_broker,
        llm=mock_llm,
        market_data=mock_market_data,
        notify=mock_notify,
        market_hours=market_hours,
        event_bus=EventBus(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCronDiscovery:
    """Test that CronScheduler discovers only CRON-triggered skills."""

    def test_discovers_cron_skills(self, cron_context):
        ctx = cron_context
        skills = {
            "cron-a": _make_cron_stub(ctx, "cron-a"),
            "cron-b": _make_cron_stub(ctx, "cron-b"),
            "heartbeat-x": HeartbeatStubSkill(ctx),
        }
        scheduler = CronScheduler(ctx, skills)

        assert "cron-a" in scheduler.cron_skills
        assert "cron-b" in scheduler.cron_skills
        assert "heartbeat-x" not in scheduler.cron_skills

    def test_discovers_skill_with_none_schedule(self, cron_context):
        """Skills with schedule=None are discovered but skipped at fire time."""
        ctx = cron_context
        skills = {
            "cron-none": _make_cron_stub(ctx, "cron-none", schedule=None),
        }
        scheduler = CronScheduler(ctx, skills)

        # Discovered (so it shows in cron_skills)
        assert "cron-none" in scheduler.cron_skills

    def test_no_cron_skills(self, cron_context):
        ctx = cron_context
        skills = {
            "heartbeat-only": HeartbeatStubSkill(ctx),
        }
        scheduler = CronScheduler(ctx, skills)
        assert len(scheduler.cron_skills) == 0


class TestScheduleEvaluation:
    """Test _is_due logic using croniter."""

    def test_skill_is_due_when_fire_time_in_window(self, cron_context):
        ctx = cron_context
        skills = {"cron-a": _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")}
        scheduler = CronScheduler(ctx, skills)

        # At exactly 10:05:10, the previous fire time for "*/5 * * * *" is 10:05:00
        # which is 10 seconds ago -- within the 30-second check interval
        now = datetime(2026, 3, 23, 10, 5, 10)  # Monday
        assert scheduler._is_due("cron-a", "*/5 * * * *", now) is True

    def test_skill_not_due_when_fire_time_outside_window(self, cron_context):
        ctx = cron_context
        skills = {"cron-a": _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")}
        scheduler = CronScheduler(ctx, skills)

        # At 10:03:00, the previous fire for "*/5" is 10:00:00 => 180s ago
        # That's outside the 30-second window
        now = datetime(2026, 3, 23, 10, 3, 0)
        assert scheduler._is_due("cron-a", "*/5 * * * *", now) is False

    def test_skill_is_due_after_last_run(self, cron_context):
        ctx = cron_context
        skills = {"cron-a": _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")}
        scheduler = CronScheduler(ctx, skills)

        # Set last run to 10:00:05
        scheduler._last_run["cron-a"] = datetime(2026, 3, 23, 10, 0, 5)

        # Now at 10:05:10: prev fire = 10:05:00 > last_run 10:00:05 => due
        now = datetime(2026, 3, 23, 10, 5, 10)
        assert scheduler._is_due("cron-a", "*/5 * * * *", now) is True

    def test_skill_not_due_when_already_fired(self, cron_context):
        """Double-firing prevention: if last_run >= prev fire time, not due."""
        ctx = cron_context
        skills = {"cron-a": _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")}
        scheduler = CronScheduler(ctx, skills)

        # Last run was at 10:05:05, prev fire for 10:05:10 is 10:05:00
        # 10:05:00 is NOT > 10:05:05 => not due (already fired)
        scheduler._last_run["cron-a"] = datetime(2026, 3, 23, 10, 5, 5)
        now = datetime(2026, 3, 23, 10, 5, 10)
        assert scheduler._is_due("cron-a", "*/5 * * * *", now) is False


class TestCheckAndFire:
    """Test _check_and_fire -- the main per-tick method."""

    async def test_due_skill_is_executed(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        # Patch _now to return a time right after a fire time
        now = datetime(2026, 3, 23, 10, 5, 10)  # Monday, not holiday
        scheduler._now = lambda: now
        await scheduler._check_and_fire()

        # Skill should have been fired; last_run updated
        assert "cron-a" in scheduler._last_run
        # Audit log should have been called
        ctx.db.log_audit.assert_called_once()

    async def test_skill_with_none_schedule_is_skipped(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-none", schedule=None)
        skills = {"cron-none": skill}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now
        await scheduler._check_and_fire()

        # Should NOT have run
        assert "cron-none" not in scheduler._last_run
        ctx.db.log_audit.assert_not_called()

    async def test_holiday_skips_all_skills(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        # 2026-01-26 is in the sample_config holidays list
        now = datetime(2026, 1, 26, 10, 5, 10)
        scheduler._now = lambda: now
        await scheduler._check_and_fire()

        assert "cron-a" not in scheduler._last_run

    async def test_should_run_false_prevents_execution(self, cron_context):
        """If skill.should_run() returns False, skill is still marked as fired
        but execute() is not called."""
        ctx = cron_context
        skill = _make_cron_stub(
            ctx, "cron-a", schedule="*/5 * * * *", should_run_val=False,
        )
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now
        await scheduler._check_and_fire()

        # last_run is NOT set when should_run() returns False (skill can retry next tick)
        assert "cron-a" not in scheduler._last_run

    async def test_double_fire_prevented(self, cron_context):
        """Running _check_and_fire twice at the same time should not fire twice."""
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now

        await scheduler._check_and_fire()
        first_last_run = scheduler._last_run["cron-a"]

        # Reset the mock to track calls after first fire
        ctx.db.log_audit.reset_mock()

        # Second check at same time -- should NOT fire again
        await scheduler._check_and_fire()

        # last_run should still be the same (not updated to a later time)
        assert scheduler._last_run["cron-a"] == first_last_run
        # No second audit log
        ctx.db.log_audit.assert_not_called()

    async def test_invalid_cron_expression_logged_and_skipped(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "bad-cron", schedule="not a cron")
        skills = {"bad-cron": skill}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now
        # Should not raise
        await scheduler._check_and_fire()

        assert "bad-cron" not in scheduler._last_run

    async def test_dynamic_schedule_change(self, cron_context):
        """Skills can change their schedule attribute dynamically."""
        ctx = cron_context
        skill = _make_cron_stub(ctx, "dynamic", schedule=None)
        skills = {"dynamic": skill}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now

        # First tick: schedule is None, should skip
        await scheduler._check_and_fire()
        assert "dynamic" not in scheduler._last_run

        # Now set the schedule dynamically
        skill.schedule = "*/5 * * * *"

        # Second tick: should fire
        await scheduler._check_and_fire()
        assert "dynamic" in scheduler._last_run

    async def test_multiple_skills_fired_independently(self, cron_context):
        """Multiple CRON skills can fire in the same tick."""
        ctx = cron_context
        skill_a = _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")
        skill_b = _make_cron_stub(ctx, "cron-b", schedule="*/5 * * * *")
        skills = {"cron-a": skill_a, "cron-b": skill_b}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now
        await scheduler._check_and_fire()

        assert "cron-a" in scheduler._last_run
        assert "cron-b" in scheduler._last_run
        assert ctx.db.log_audit.call_count == 2


class TestRunSkill:
    """Test _run_skill wrapper."""

    async def test_successful_skill_execution(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-a")
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        result = await scheduler._run_skill("cron-a", skill)
        assert result.success is True
        assert result.skill_name == "cron-a"

    async def test_should_run_false_returns_skipped_result(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-a", should_run_val=False)
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        result = await scheduler._run_skill("cron-a", skill)
        assert result.success is True
        assert result.data.get("skipped") is True

    async def test_failing_skill_returns_error_result(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-fail", succeed=False)
        skills = {"cron-fail": skill}
        scheduler = CronScheduler(ctx, skills)

        result = await scheduler._run_skill("cron-fail", skill)
        assert result.success is False


class TestLifecycle:
    """Test start/stop lifecycle."""

    async def test_stop_terminates_loop(self, cron_context):
        ctx = cron_context
        skills = {"cron-a": _make_cron_stub(ctx, "cron-a")}
        scheduler = CronScheduler(ctx, skills)

        # Stop immediately so the loop exits after one iteration
        scheduler.stop()
        assert scheduler._running is False

    async def test_start_sets_running_flag(self, cron_context):
        ctx = cron_context
        skills = {}
        scheduler = CronScheduler(ctx, skills)

        # We'll stop it after a very short time
        async def stop_soon():
            await asyncio.sleep(0.05)
            scheduler.stop()

        await asyncio.gather(
            scheduler.start(),
            stop_soon(),
        )
        # After stop, running should be False
        assert scheduler._running is False


class TestAuditLogging:
    """Test that CRON invocations are logged to audit."""

    async def test_audit_entry_logged_on_fire(self, cron_context):
        ctx = cron_context
        skill = _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now
        await scheduler._check_and_fire()

        ctx.db.log_audit.assert_called_once()
        # Verify the audit entry contents
        _, kwargs = ctx.db.log_audit.call_args
        assert kwargs["action_type"] == "cron_invocation"
        assert kwargs["skill_name"] == "cron-a"
        assert kwargs["input_summary"]["schedule"] == "*/5 * * * *"
        assert "success" in kwargs["output_summary"]

    async def test_audit_failure_does_not_crash(self, cron_context):
        """If log_audit raises, the scheduler should not crash."""
        ctx = cron_context
        ctx.db.log_audit = AsyncMock(side_effect=Exception("DB error"))

        skill = _make_cron_stub(ctx, "cron-a", schedule="*/5 * * * *")
        skills = {"cron-a": skill}
        scheduler = CronScheduler(ctx, skills)

        now = datetime(2026, 3, 23, 10, 5, 10)
        scheduler._now = lambda: now
        # Should not raise despite log_audit failure
        await scheduler._check_and_fire()

        # Skill was still fired
        assert "cron-a" in scheduler._last_run
