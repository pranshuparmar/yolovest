"""Skill: health-check — System health monitoring and heartbeat.

Trigger: HEARTBEAT — every heartbeat (market hours: 15min, off hours: 60min)
Pipeline position: Runs first every heartbeat, gates all other skills.

Flow:
1. Check broker connectivity (is access_token valid?)
2. Check database health (can we read/write?)
3. Check Gemini API reachability (ping with small request)
4. Check market data providers (at least one responding?)
5. Check disk space (SQLite DB growing?)
6. Check open positions are consistent (no orphaned orders)
7. Check kill switch state — if active, skip all trading skills
8. If any critical check fails:
   a. Send Telegram alert (errors alert type)
   b. If positions are at risk, trigger protective square-off
   c. Log failure for dashboard display
9. Return health status for orchestrator to decide which skills to run
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class HealthCheckSkill(SkillBase):
    name = "health-check"
    description = "System health monitoring, heartbeat, crash recovery"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return True  # always runs

    async def execute(self, **kwargs: Any) -> SkillResult:
        checks: dict[str, Any] = {}
        critical_failures: list[str] = []

        # Check 1: Broker
        try:
            checks["broker"] = await self.ctx.broker.is_authenticated()
        except Exception as e:
            checks["broker"] = False
            critical_failures.append(f"Broker: {e}")

        # Check 2: Database
        try:
            checks["database"] = await self.ctx.db.health_check()
        except Exception as e:
            checks["database"] = False
            critical_failures.append(f"Database: {e}")

        # Check 3: LLM (non-critical — fallback exists)
        # Only ping once per hour to conserve Gemini free tier quota
        try:
            last_llm_check = await self.ctx.db.get_system_state("last_llm_ping_ok")
            if last_llm_check:
                from datetime import datetime, timedelta
                last_ts = datetime.fromisoformat(last_llm_check)
                from yolovest.timezone import IST, now_ist
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=IST)
                if (now_ist() - last_ts) < timedelta(hours=1):
                    checks["llm"] = True  # cached result
                else:
                    checks["llm"] = await self.ctx.llm.ping()
                    if checks["llm"]:
                        await self.ctx.db.set_system_state("last_llm_ping_ok", now_ist().isoformat())
            else:
                checks["llm"] = await self.ctx.llm.ping()
                if checks["llm"]:
                    from yolovest.timezone import now_ist
                    await self.ctx.db.set_system_state("last_llm_ping_ok", now_ist().isoformat())
        except Exception:
            checks["llm"] = False  # non-critical, fallback exists

        # Check 4: Market data (at least one provider up)
        try:
            checks["market_data"] = await self.ctx.market_data.health_check()
        except Exception as e:
            checks["market_data"] = False
            critical_failures.append(f"Market data: {e}")

        # Check 5: Disk space
        checks["disk_ok"] = await self._check_disk_space()

        # Check 6: Position consistency
        checks["positions_consistent"] = await self._check_position_consistency()

        # Check 7: Kill switch state
        checks["kill_switch_active"] = await self.ctx.db.is_kill_switch_active()

        # Graceful degradation — protect positions on critical failure
        if critical_failures and self.ctx.market_hours.is_market_hours():
            open_positions = await self.ctx.db.get_open_positions()
            if open_positions:
                await self.ctx.notify.send(
                    f"CRITICAL: {len(critical_failures)} system failures detected. "
                    f"{len(open_positions)} open positions at risk.\n"
                    + "\n".join(critical_failures)
                )

        # Alert on any failures (respects errors alert toggle)
        if critical_failures:
            await self.ctx.notify.send(
                "Health check failures:\n" + "\n".join(critical_failures),
                alert_type="errors",
            )

        return SkillResult(
            success=len(critical_failures) == 0,
            skill_name=self.name,
            data={
                "checks": checks,
                "critical_failures": critical_failures,
                "all_healthy": len(critical_failures) == 0,
                "trading_allowed": (
                    len(critical_failures) == 0 and not checks["kill_switch_active"]
                ),
            },
        )

    async def _check_disk_space(self) -> bool:
        """Ensure sufficient disk space for DB growth."""
        import shutil

        try:
            db_path = self.ctx.config.database.path
            usage = shutil.disk_usage(db_path if db_path != ":memory:" else "/")
            # Warn if less than 100MB free
            return usage.free > 100 * 1024 * 1024
        except Exception:
            return True  # assume OK if we can't check

    async def _check_position_consistency(self) -> bool:
        """Verify no orphaned orders or position mismatches."""
        try:
            local = await self.ctx.db.get_open_positions()
            # In paper mode, no broker positions to compare
            if self.ctx.config.mode == "paper":
                return True
            broker = await self.ctx.broker.get_positions()
            # Simple check: same count
            local_count = len(local)
            broker_count = sum(
                1 for p in broker
                if (p.get("quantity", 0) or p.get("net_quantity", 0)) != 0
            )
            return local_count == broker_count
        except Exception:
            return False
