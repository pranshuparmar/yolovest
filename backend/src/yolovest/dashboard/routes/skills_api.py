"""Skill registry listing and manual skill runs.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
)

from yolovest.dashboard.ws import broadcast_ws

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials


    # ------------------------------------------------------------------
    # Manual Skill Trigger
    # ------------------------------------------------------------------

    @app.get("/api/skills")
    async def list_skills(
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, str | None]]:
        """List all registered skills with metadata and runtime schedules."""
        from yolovest.skills import SKILL_REGISTRY

        out = []
        for name, cls in sorted(SKILL_REGISTRY.items()):
            # Instantiate to get runtime schedule (set from config in __init__)
            try:
                instance = cls(ctx)
                schedule = instance.schedule
            except Exception:
                logger.debug("Failed to instantiate skill %s for schedule", name, exc_info=True)
                schedule = cls.schedule
            out.append({
                "name": name,
                "description": cls.description,
                "trigger": cls.trigger.value,
                "schedule": schedule,
            })
        return out

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

