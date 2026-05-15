"""Tests for the shared KiteRateLimiter."""

import asyncio
import time

import pytest

from yolovest.broker.kite_rate_limiter import KiteRateLimiter


class TestKiteRateLimiterTime:
    """The time-based gate enforces minimum interval between entries."""

    async def test_first_acquire_is_immediate(self):
        rl = KiteRateLimiter(calls_per_second=10.0, concurrency=8)
        start = time.monotonic()
        async with rl:
            pass
        # Should complete in well under one interval
        assert time.monotonic() - start < 0.05

    async def test_second_acquire_waits_for_interval(self):
        rl = KiteRateLimiter(calls_per_second=5.0, concurrency=8)  # 200ms interval
        async with rl:
            pass
        start = time.monotonic()
        async with rl:
            pass
        elapsed = time.monotonic() - start
        assert elapsed >= 0.18  # tolerance for scheduling

    async def test_serial_acquires_sustain_rate(self):
        """Five back-to-back acquires at 10 req/s should take >= 400ms."""
        rl = KiteRateLimiter(calls_per_second=10.0, concurrency=8)
        start = time.monotonic()
        for _ in range(5):
            async with rl:
                pass
        elapsed = time.monotonic() - start
        # Four intervals of 100ms between five entries; first is free
        assert elapsed >= 0.36


class TestKiteRateLimiterConcurrency:
    """The semaphore caps concurrent holders independently of rate."""

    async def test_concurrency_cap_enforced(self):
        rl = KiteRateLimiter(calls_per_second=1000.0, concurrency=2)
        events: list[str] = []

        async def worker(idx: int):
            async with rl:
                events.append(f"enter:{idx}")
                await asyncio.sleep(0.1)
                events.append(f"exit:{idx}")

        await asyncio.gather(*[worker(i) for i in range(4)])

        # With concurrency=2, the 3rd and 4th workers must enter only
        # after at least one of the first two has exited
        enters = [e for e in events if e.startswith("enter:")]
        exits = [e for e in events if e.startswith("exit:")]
        # First two enters before any exits
        assert events.index(enters[0]) < events.index(exits[0])
        assert events.index(enters[1]) < events.index(exits[0])
        # Third enter happens after at least one exit
        assert events.index(enters[2]) > events.index(exits[0])


class TestKiteRateLimiterValidation:
    def test_rejects_zero_rate(self):
        with pytest.raises(ValueError):
            KiteRateLimiter(calls_per_second=0)

    def test_rejects_negative_rate(self):
        with pytest.raises(ValueError):
            KiteRateLimiter(calls_per_second=-1)

    def test_rejects_zero_concurrency(self):
        with pytest.raises(ValueError):
            KiteRateLimiter(concurrency=0)
