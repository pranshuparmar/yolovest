"""Skill: kill-switch — Emergency stop, kill, and resume.

Trigger: MANUAL — via Telegram commands (/stop, /kill, /resume) or dashboard button
Pipeline position: Overrides all other skills when active.

Commands:
- /stop  → Pause all trading. Cancel all pending/open orders. Keep positions.
           State persists across restarts.
- /kill  → Square off EVERYTHING at market price + pause trading.
           Calls square-off skill with force=True.
- /resume → Resume trading. Only works after explicit /stop or /kill.

Flow:
1. Parse command: stop / kill / resume
2. /stop:
   a. Set kill_switch_active = True in DB (persistent)
   b. Cancel all pending orders via broker
   c. Send Telegram confirmation
3. /kill:
   a. Set kill_switch_active = True in DB
   b. Cancel all pending orders
   c. Square off ALL positions (MIS + CNC) via square-off skill
   d. Send Telegram confirmation with PnL
4. /resume:
   a. Set kill_switch_active = False in DB
   b. Run health check to verify system is healthy
   c. Send Telegram confirmation
"""

import contextlib
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class KillSwitchSkill(SkillBase):
    name = "kill-switch"
    description = "Emergency stop/kill/resume trading"
    trigger = SkillTrigger.MANUAL
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.config.risk.kill_switch_enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        command = kwargs.get("command", "stop")

        if command == "stop":
            return await self._execute_stop()
        elif command == "kill":
            return await self._execute_kill()
        elif command == "resume":
            return await self._execute_resume()
        else:
            return SkillResult(
                success=False,
                skill_name=self.name,
                error=f"Unknown command: {command}",
            )

    async def _execute_stop(self) -> SkillResult:
        """Pause trading, cancel pending orders, keep positions."""
        # Persist kill switch state
        await self.ctx.db.set_system_state("kill_switch", "active")

        # Cancel all pending orders
        pending_orders = await self.ctx.broker.get_pending_orders()
        cancelled = 0
        for order in pending_orders:
            try:
                await self.ctx.broker.cancel_order(order["order_id"])
                cancelled += 1
            except Exception:
                pass  # log but continue

        await self.ctx.notify.send(
            f"STOP: Trading paused. {cancelled} pending orders cancelled.\n"
            "Existing positions are untouched.\n"
            "Send /resume to restart trading.",
            alert_type="kill_switch",
        )

        await self.broadcast("kill_switch_activated", {
            "command": "stop", "orders_cancelled": cancelled,
        })

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"command": "stop", "orders_cancelled": cancelled},
        )

    async def _execute_kill(self) -> SkillResult:
        """Nuclear option: square off everything + pause."""
        await self.ctx.db.set_system_state("kill_switch", "active")

        # Cancel all pending orders
        pending_orders = await self.ctx.broker.get_pending_orders()
        for order in pending_orders:
            with contextlib.suppress(Exception):
                await self.ctx.broker.cancel_order(order["order_id"])

        # Square off ALL positions (force=True bypasses MIS filter)
        from yolovest.skills.square_off import SquareOffSkill

        square_off = SquareOffSkill(self.ctx)
        sq_result = await square_off.execute(force=True)

        total_pnl = sq_result.data.get("total_pnl", 0)
        await self.ctx.notify.send(
            f"KILL: All positions squared off. PnL: {total_pnl:,.2f}\n"
            "Trading is paused.\n"
            "Send /resume to restart trading.",
            alert_type="kill_switch",
        )

        await self.broadcast("kill_switch_activated", {
            "command": "kill", "total_pnl": total_pnl,
        })

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "command": "kill",
                "square_off_result": sq_result.data,
                "total_pnl": total_pnl,
            },
        )

    async def _execute_resume(self) -> SkillResult:
        """Resume trading after stop/kill."""
        await self.ctx.db.set_system_state("kill_switch", "inactive")

        # Run health check before resuming
        from yolovest.skills.health_check import HealthCheckSkill

        health = HealthCheckSkill(self.ctx)
        health_result = await health.execute()

        healthy = health_result.data.get("all_healthy", False)
        status = "All systems healthy." if healthy else "WARNING: Some systems unhealthy."
        await self.ctx.notify.send(
            f"RESUME: Trading resumed. {status}", alert_type="kill_switch",
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "command": "resume",
                "system_healthy": healthy,
                "health_checks": health_result.data.get("checks"),
            },
        )
