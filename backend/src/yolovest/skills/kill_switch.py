"""Skill: kill-switch — Emergency pause, stop, kill, and resume.

Trigger: MANUAL — via Telegram commands (/pause, /stop, /kill, /resume) or dashboard button
Pipeline position: Overrides all other skills when active.

Commands:
- /pause → Block new signals from executing. Leave every existing broker
            order, GTT, SL leg and position untouched. Pure
            future-trade halt. Use when you want the system to "stop
            taking new bets" but keep existing protections alive.
- /stop  → Pause all trading. Cancel all pending/open orders. Keep positions.
           Warning: this also cancels SL / target legs of open MIS
           positions, leaving them unprotected. Prefer /pause unless
           you specifically want every order off the books.
- /kill  → Square off EVERYTHING at market price + pause trading.
           Calls square-off skill with force=True.
- /resume → Resume trading. Only works after explicit /pause, /stop or /kill.

State storage:
- system_state.kill_switch = "active" | "inactive" (existing boolean flag)
- system_state.kill_switch_mode = "pause" | "stop" | "kill" | null
  Records WHICH command activated the pause so the UI / logs can
  surface "Paused (soft)" vs "Stop (orders cancelled)". All three
  active modes block new trades identically via risk_check.
"""

import contextlib
import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class KillSwitchSkill(SkillBase):
    name = "kill-switch"
    description = "Emergency pause/stop/kill/resume trading"
    trigger = SkillTrigger.MANUAL
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.config.risk.kill_switch_enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        command = kwargs.get("command", "stop")

        if command == "pause":
            return await self._execute_pause()
        elif command == "stop":
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

    async def _execute_pause(self) -> SkillResult:
        """Soft pause: block new signals, leave broker state alone.

        Does not call broker.cancel_order, does not touch GTTs, does
        not square off anything. Active SL / target legs, GTTs, and
        positions continue to run normally. risk_check will refuse
        new signals because kill_switch is active.
        """
        await self.ctx.db.set_system_state("kill_switch", "active")
        await self.ctx.db.set_system_state("kill_switch_mode", "pause")

        await self.ctx.notify.send(
            "PAUSE: New trades blocked. "
            "All existing broker orders, GTTs and positions are untouched.\n"
            "Send /resume to restart.",
            alert_type="kill_switch",
        )

        await self.broadcast("kill_switch_activated", {"command": "pause"})

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"command": "pause"},
        )

    async def _execute_stop(self) -> SkillResult:
        """Pause trading, cancel pending orders, keep positions."""
        # Persist kill switch state
        await self.ctx.db.set_system_state("kill_switch", "active")
        await self.ctx.db.set_system_state("kill_switch_mode", "stop")

        # Cancel all pending orders
        pending_orders = await self.ctx.broker.get_pending_orders()
        cancelled = 0
        for order in pending_orders:
            try:
                await self.ctx.broker.cancel_order(order["order_id"])
                cancelled += 1
            except Exception:
                logger.warning("Failed to cancel order %s", order.get("order_id"), exc_info=True)

        await self.ctx.notify.send(
            f"STOP: Trading paused. {cancelled} pending orders cancelled.\n"
            "Existing positions are untouched (but their SL/target legs may now be gone — review).\n"
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
        await self.ctx.db.set_system_state("kill_switch_mode", "kill")

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
        """Resume trading after pause/stop/kill."""
        await self.ctx.db.set_system_state("kill_switch", "inactive")
        await self.ctx.db.set_system_state("kill_switch_mode", "")

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
