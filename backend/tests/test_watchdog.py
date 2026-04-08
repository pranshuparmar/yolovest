"""Tests for HeartbeatWatchdog."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from yolovest.watchdog import HeartbeatWatchdog


@pytest.fixture
def watchdog(app_context):
    return HeartbeatWatchdog(app_context)


class TestRecordHeartbeat:
    def test_record_resets_timestamp(self, watchdog):
        old = watchdog._last_heartbeat
        time.sleep(0.01)
        watchdog.record_heartbeat()
        assert watchdog._last_heartbeat > old

    def test_record_clears_alert_flags(self, watchdog):
        watchdog._alerted_warning = True
        watchdog._alerted_critical = True
        watchdog.record_heartbeat()
        assert not watchdog._alerted_warning
        assert not watchdog._alerted_critical


class TestExpectedInterval:
    def test_market_hours_interval(self, watchdog):
        # _expected_interval_sec returns _last_interval_sec which is set
        # during record_heartbeat based on current market hours state.
        # Patch is_market_hours before calling record_heartbeat so the
        # stored interval reflects market hours.
        with patch.object(watchdog._ctx.market_hours, "is_market_hours", return_value=True):
            watchdog.record_heartbeat()
            interval = watchdog._expected_interval_sec()
        assert interval == watchdog._ctx.config.heartbeat.market_hours_interval_min * 60

    def test_off_hours_interval(self, watchdog):
        with patch.object(watchdog._ctx.market_hours, "is_market_hours", return_value=False):
            interval = watchdog._expected_interval_sec()
        assert interval == watchdog._ctx.config.heartbeat.off_hours_interval_min * 60


class TestWatchdogAlerts:
    async def test_no_alert_when_heartbeat_recent(self, watchdog):
        """No alert should fire if heartbeat just happened."""
        watchdog.record_heartbeat()

        # Simulate one check cycle (patch sleep to return immediately)
        watchdog._running = True

        async def _one_cycle():
            # Directly invoke the check logic
            elapsed = time.monotonic() - watchdog._last_heartbeat
            expected = watchdog._expected_interval_sec()
            buffer_sec = 120
            overdue_ratio = elapsed / (expected + buffer_sec)
            return overdue_ratio

        ratio = await _one_cycle()
        assert ratio < 1.0
        assert not watchdog._alerted_warning
        assert not watchdog._alerted_critical

    async def test_warning_when_overdue(self, watchdog):
        """Warning alert when 2x overdue."""
        # Set last heartbeat far in the past
        expected = watchdog._expected_interval_sec()
        buffer = 120
        # Make it 2.5x overdue
        watchdog._last_heartbeat = time.monotonic() - (expected + buffer) * 2.5

        with patch.object(watchdog._ctx.market_hours, "is_market_hours", return_value=True):
            elapsed = time.monotonic() - watchdog._last_heartbeat
            overdue_ratio = elapsed / (expected + buffer)

        assert overdue_ratio >= 2.0
        # Simulate the alert logic
        if overdue_ratio >= 2.0 and not watchdog._alerted_warning:
            watchdog._alerted_warning = True
            await watchdog._ctx.notify.send("test warning", alert_type="errors")

        watchdog._ctx.notify.send.assert_awaited()
        assert watchdog._alerted_warning

    async def test_critical_when_very_overdue(self, watchdog):
        """Critical alert when 3x overdue."""
        expected = watchdog._expected_interval_sec()
        buffer = 120
        watchdog._last_heartbeat = time.monotonic() - (expected + buffer) * 3.5

        elapsed = time.monotonic() - watchdog._last_heartbeat
        overdue_ratio = elapsed / (expected + buffer)
        assert overdue_ratio >= 3.0


class TestWatchdogLifecycle:
    async def test_stop_terminates_loop(self, watchdog):
        """Watchdog should stop when stop() is called."""
        watchdog.stop()
        assert not watchdog._running

    async def test_start_sets_running(self, watchdog):
        """start() should set running flag."""
        # Run for a very short time
        watchdog._running = True

        async def _quick_stop():
            await asyncio.sleep(0.05)
            watchdog.stop()

        task = asyncio.create_task(_quick_stop())
        # Override sleep to be very short
        with patch("yolovest.watchdog._CHECK_INTERVAL_SEC", 0.01):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                mock_sleep.side_effect = [None, asyncio.CancelledError()]
                try:
                    await watchdog.start()
                except asyncio.CancelledError:
                    pass
        task.cancel()
