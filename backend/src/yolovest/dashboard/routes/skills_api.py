"""Skill registry listing and manual skill runs.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)

from yolovest.dashboard.ws import broadcast_ws

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def _next_cron_run(schedule: str) -> str | None:
    """Next fire time (IST ISO string) for a cron expression, or None
    if the expression can't be parsed."""
    from croniter import croniter

    from yolovest.timezone import now_ist
    try:
        return croniter(schedule, now_ist()).get_next(type(now_ist())).isoformat()
    except (ValueError, KeyError):
        return None


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials


    # ------------------------------------------------------------------
    # Manual Skill Trigger
    # ------------------------------------------------------------------

    @app.get("/api/skills")
    async def list_skills(
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """List all registered skills with metadata and runtime schedules.

        For CRON skills, also reports whether the schedule is currently
        enabled (vs paused via the dashboard) and the next fire time, so
        the Skills page can offer Start/Stop controls.
        """
        from yolovest.cron_scheduler import load_disabled_schedules
        from yolovest.skills import SKILL_REGISTRY
        from yolovest.skills.base import SkillTrigger

        disabled = await load_disabled_schedules(ctx.db)

        out: list[dict[str, Any]] = []
        for name, cls in sorted(SKILL_REGISTRY.items()):
            # Instantiate to get runtime schedule (set from config in __init__)
            try:
                instance = cls(ctx)
                schedule = instance.compute_schedule()
            except Exception:
                logger.debug("Failed to instantiate skill %s for schedule", name, exc_info=True)
                schedule = cls.schedule
            is_cron = cls.trigger == SkillTrigger.CRON
            enabled: bool | None = (name not in disabled) if is_cron else None
            next_run: str | None = None
            if is_cron and schedule and enabled:
                next_run = _next_cron_run(schedule)
            out.append({
                "name": name,
                "description": cls.description,
                "trigger": cls.trigger.value,
                "schedule": schedule,
                "enabled": enabled,
                "next_run": next_run,
            })
        return out

    @app.post("/api/skills/{skill_name}/schedule")
    async def set_schedule_enabled(
        skill_name: str,
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Start or stop a CRON skill's automatic schedule.

        Body: {"enabled": true|false}. Paused schedules are persisted in
        system_state and skipped by the running scheduler within one tick;
        manual "Run Now" is unaffected. Does not touch the cron expression
        itself (edit that in Settings).
        """
        from yolovest.cron_scheduler import (
            DISABLED_SCHEDULES_KEY,
            load_disabled_schedules,
        )
        from yolovest.skills import SKILL_REGISTRY
        from yolovest.skills.base import SkillTrigger

        cls = SKILL_REGISTRY.get(skill_name)
        if cls is None:
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
        if cls.trigger != SkillTrigger.CRON:
            raise HTTPException(
                status_code=400,
                detail=f"Skill '{skill_name}' is not a scheduled (CRON) skill",
            )

        body = await request.json()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="Body must include boolean 'enabled'")

        disabled = await load_disabled_schedules(ctx.db)
        if enabled:
            disabled.discard(skill_name)
        else:
            disabled.add(skill_name)
        await ctx.db.set_system_state(DISABLED_SCHEDULES_KEY, json.dumps(sorted(disabled)))
        logger.info("Schedule for '%s' %s", skill_name, "enabled" if enabled else "paused")
        return {"success": True, "skill": skill_name, "enabled": enabled}

    # Track background skill tasks
    _running_skills: dict[str, asyncio.Task[Any]] = {}

    @app.post("/api/skills/{skill_name}/run")
    async def run_skill(
        skill_name: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Manually trigger a registered skill by name.

        Long-running skills run in the background and return immediately.
        Results are broadcast via WebSocket when complete.
        """
        from yolovest.skills import SKILL_REGISTRY

        if skill_name not in SKILL_REGISTRY:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown skill: {skill_name}. "
                f"Available: {sorted(SKILL_REGISTRY.keys())}",
            )

        # Check if already running
        existing = _running_skills.get(skill_name)
        if existing and not existing.done():
            return {"success": True, "skill": skill_name, "status": "already_running"}

        skill_cls = SKILL_REGISTRY[skill_name]
        skill = skill_cls(ctx)

        async def _run_in_background() -> None:
            logger.info("Background skill started: %s", skill_name)
            try:
                result = await skill.safe_execute()
                logger.info(
                    "Background skill %s completed: success=%s, duration=%.1fms",
                    skill_name, result.success, result.duration_ms,
                )
                # Audit log
                try:
                    await ctx.db.log_audit(
                        action_type="manual_skill_execution",
                        skill_name=skill_name,
                        output_summary={
                            "success": result.success,
                            "duration_ms": round(result.duration_ms, 1),
                            "error": result.error,
                        },
                        duration_ms=result.duration_ms,
                    )
                except Exception:
                    logger.debug("Failed to log audit for manual skill %s", skill_name, exc_info=True)
                await broadcast_ws("skill_completed", {
                    "skill": skill_name,
                    "success": result.success,
                    "duration_ms": round(result.duration_ms, 1),
                    "error": result.error,
                    "data": {k: v for k, v in result.data.items()
                             if isinstance(v, (str, int, float, bool, type(None)))}
                    if result.data else {},
                })
            except Exception as e:
                logger.exception("Background skill run failed: %s", skill_name)
                await broadcast_ws("skill_completed", {
                    "skill": skill_name,
                    "success": False,
                    "error": str(e),
                })
            finally:
                _running_skills.pop(skill_name, None)

        task = asyncio.create_task(_run_in_background())
        _running_skills[skill_name] = task

        return {"success": True, "skill": skill_name, "status": "started"}

