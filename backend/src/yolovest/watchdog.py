"""Heartbeat watchdog for YoloVest.

Monitors the orchestrator's heartbeat loop and alerts if heartbeats
stop arriving. Detects hung event loops, deadlocks, and unresponsive
API calls that would otherwise silently stop all trading and position
monitoring.

Limitation: since this runs in the same event loop as the orchestrator,
a fully frozen event loop will also freeze the watchdog. For that case,
use an external watchdog (systemd WatchdogSec, Docker HEALTHCHECK).
"""

import asyncio
import logging
import time

from yolovest.context import AppContext

logger = logging.getLogger(__name__)

# How often the watchdog checks (seconds)
_CHECK_INTERVAL_SEC = 60


class HeartbeatWatchdog:
    """Watches for heartbeat completions and alerts when overdue.

    States:
    - Normal: heartbeat completed within expected interval
    - Warning (2x overdue): sends alert, logs warning
    - Critical (3x overdue): sends CRITICAL alert every check cycle

    Handles interval transitions (off-hours → market hours) by tracking
    which interval was active at the last heartbeat.
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._last_heartbeat: float = time.monotonic()
        self._last_interval_sec: float = ctx.config.heartbeat.off_hours_interval_min * 60
        self._running = False
        self._alerted_warning = False
        self._alerted_critical = False

    def record_heartbeat(self) -> None:
        """Called by the orchestrator after each heartbeat completes."""
        self._last_heartbeat = time.monotonic()
        # Record the interval that will be used for the NEXT sleep
        if self._ctx.market_hours.is_market_hours():
            self._last_interval_sec = self._ctx.config.heartbeat.market_hours_interval_min * 60
        else:
            self._last_interval_sec = self._ctx.config.heartbeat.off_hours_interval_min * 60
        self._alerted_warning = False
        self._alerted_critical = False

    def _expected_interval_sec(self) -> float:
        """Get the expected interval based on what was active at last heartbeat.

        This prevents false alarms during off-hours → market-hours transition:
        the last heartbeat may have been 60min ago (off-hours interval), but
        the current time is now market hours (15min interval). Using the
        interval from last heartbeat avoids a spurious overdue warning.
        """
        return self._last_interval_sec

    async def start(self) -> None:
        """Run the watchdog loop. Runs until stopped."""
        self._running = True
        logger.info("Heartbeat watchdog started (check every %ds)", _CHECK_INTERVAL_SEC)

        while self._running:
            await asyncio.sleep(_CHECK_INTERVAL_SEC)
            if not self._running:
                break

            elapsed = time.monotonic() - self._last_heartbeat
            expected = self._expected_interval_sec()

            # Add buffer: heartbeat takes time to execute, so allow
            # expected + 2 minutes before considering it overdue
            buffer_sec = 120
            overdue_ratio = elapsed / (expected + buffer_sec)

            if overdue_ratio >= 3.0 and not self._alerted_critical:
                self._alerted_critical = True
                logger.critical(
                    "WATCHDOG: No heartbeat for %.0fs (expected every %.0fs). "
                    "Orchestrator may be hung.",
                    elapsed, expected,
                )
                await self._ctx.notify.send(
                    f"CRITICAL: No heartbeat for {elapsed / 60:.0f} minutes "
                    f"(expected every {expected / 60:.0f}min). "
                    f"Orchestrator may be hung — open positions could be unmonitored.\n"
                    f"Check server logs and consider restarting.",
                    alert_type="errors",
                )
            elif overdue_ratio >= 2.0 and not self._alerted_warning:
                self._alerted_warning = True
                logger.warning(
                    "WATCHDOG: Heartbeat overdue — %.0fs since last "
                    "(expected every %.0fs)",
                    elapsed, expected,
                )
                await self._ctx.notify.send(
                    f"WARNING: Heartbeat overdue — {elapsed / 60:.0f}min since last "
                    f"(expected every {expected / 60:.0f}min). "
                    f"May be a slow API call or temporary issue.",
                    alert_type="errors",
                )

    def stop(self) -> None:
        """Signal the watchdog loop to stop."""
        self._running = False
        logger.info("Heartbeat watchdog stop requested")
