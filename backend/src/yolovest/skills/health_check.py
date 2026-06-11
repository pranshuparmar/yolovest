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
8. If broker not authenticated during market hours, send periodic
   Telegram reminder (throttled to once per 30 minutes)
9. If any critical check fails:
   a. Send Telegram alert (errors alert type)
   b. If positions are at risk, trigger protective square-off
   c. Log failure for dashboard display
10. Return health status for orchestrator to decide which skills to run
"""

import logging
import time
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)

# Throttle broker auth reminders: minimum seconds between alerts
_BROKER_AUTH_REMINDER_INTERVAL_SEC = 30 * 60  # 30 minutes


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

        # Broker auth reminder: if not authenticated during market hours,
        # send periodic Telegram reminder (throttled to every 30 minutes)
        if not checks.get("broker") and self.ctx.market_hours.is_market_hours():
            await self._send_broker_auth_reminder()
            # Push to dashboard WS so any open tab gets a banner
            # immediately rather than waiting for the next REST poll.
            try:
                from yolovest.dashboard.ws import broadcast_ws
                await broadcast_ws("broker_auth_lost", {})
            except Exception:
                logger.debug("broker_auth_lost broadcast failed", exc_info=True)

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
                from datetime import UTC

                from yolovest.timezone import now_utc
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=UTC)
                if (now_utc() - last_ts) < timedelta(hours=1):
                    checks["llm"] = True  # cached result
                else:
                    checks["llm"] = await self.ctx.llm.ping()
                    if checks["llm"]:
                        await self.ctx.db.set_system_state("last_llm_ping_ok", now_utc().isoformat())
            else:
                checks["llm"] = await self.ctx.llm.ping()
                if checks["llm"]:
                    from yolovest.timezone import now_utc
                    await self.ctx.db.set_system_state("last_llm_ping_ok", now_utc().isoformat())
        except Exception:
            logger.debug("LLM health check failed", exc_info=True)
            checks["llm"] = False  # non-critical, fallback exists

        # Check 4: Market data (at least one provider up)
        try:
            checks["market_data"] = await self.ctx.market_data.health_check()
        except Exception as e:
            checks["market_data"] = False
            critical_failures.append(f"Market data: {e}")

        # Check 5: Disk space
        checks["disk_ok"] = await self._check_disk_space()

        # Check 6: Position consistency. Skip the broker round-trip
        # if we already know auth is dead — the comparison is
        # meaningless without a working session and re-calling
        # broker.get_positions() just emits a TokenException
        # traceback every heartbeat (most visibly on weekends, when
        # Kite tokens have naturally expired and the user hasn't
        # re-authed because the market is closed).
        checks["positions_consistent"] = await self._check_position_consistency(
            broker_ok=bool(checks.get("broker")),
        )

        # Check 7: Kill switch state
        checks["kill_switch_active"] = await self.ctx.db.is_kill_switch_active()

        # Graceful degradation — protect positions on critical failure
        if critical_failures and self.ctx.market_hours.is_market_hours():
            open_positions = await self.ctx.db.get_open_positions(mode=self.ctx.config.mode)
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
            logger.debug("Disk space check failed", exc_info=True)
            return True  # assume OK if we can't check

    async def _check_position_consistency(self, broker_ok: bool = True) -> bool:
        """Verify no orphaned orders or position mismatches.

        When `broker_ok` is False (auth already known to be invalid)
        we skip the broker round-trip. There's nothing to compare
        without a working session and the failure mode is already
        surfaced by check #1.
        """
        # In paper mode, no broker positions to compare
        if self.ctx.config.mode == "paper":
            return True
        if not broker_ok:
            # Expected on weekends / after Kite's daily 6:00 AM IST
            # token expiry until the user re-auths. The auth state
            # itself is reported by checks["broker"]; nothing else to
            # do here.
            logger.debug(
                "Skipping position consistency check — broker not authenticated"
            )
            return True
        try:
            local = await self.ctx.db.get_open_positions()
            broker = await self.ctx.broker.get_positions()
            # Simple check: same count
            local_count = len(local)
            broker_count = sum(
                1 for p in broker
                if (p.get("quantity", 0) or p.get("net_quantity", 0)) != 0
            )
            return local_count == broker_count
        except Exception:
            logger.warning("Position consistency check failed", exc_info=True)
            return False

    # Track last broker auth reminder time (monotonic, per-process)
    _last_broker_auth_reminder: float = 0.0

    async def _send_broker_auth_reminder(self) -> None:
        """Send a Telegram reminder to authenticate with Kite.

        Throttled to once per _BROKER_AUTH_REMINDER_INTERVAL_SEC (30 min).
        """
        now = time.monotonic()
        if now - self._last_broker_auth_reminder < _BROKER_AUTH_REMINDER_INTERVAL_SEC:
            return

        HealthCheckSkill._last_broker_auth_reminder = now
        login_url = self.ctx.broker.get_login_url()
        logger.warning("Broker not authenticated during market hours — sending reminder")

        try:
            await self.ctx.notify.send(
                "Kite session not authenticated — trading is disabled.\n"
                f"Re-authenticate: {login_url}\n"
                "Or use /auth (request_token) in Telegram.",
                alert_type="errors",
            )
        except Exception as e:
            logger.warning("Failed to send broker auth reminder: %s", e)
