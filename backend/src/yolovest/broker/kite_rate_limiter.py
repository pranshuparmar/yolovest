"""Shared rate limiter for Kite Connect API calls.

Combines two constraints in one async context manager:

1. Concurrency cap — at most ``concurrency`` calls in flight at once.
2. Time-based interval — at least ``1.0 / calls_per_second`` between
   successive call entries. Enforced FIFO so a busy waiter can't
   monopolize the wait.

A single instance can be shared between ``ZerodhaBroker`` and
``KiteDataProvider`` so all Kite calls draw from one combined budget.
Endpoint-specific tighter throttles stack on top of this limiter
rather than replacing it.
"""

import asyncio
import time


class KiteRateLimiter:
    """Async context manager combining concurrency + rate limit."""

    def __init__(
        self,
        calls_per_second: float = 10.0,
        concurrency: int = 8,
    ) -> None:
        if calls_per_second <= 0:
            raise ValueError("calls_per_second must be > 0")
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._semaphore = asyncio.Semaphore(concurrency)
        # Gates the timestamp update so two waiters don't read the same
        # _last_call and then both fire immediately.
        self._interval_lock = asyncio.Lock()
        self._min_interval = 1.0 / calls_per_second
        self._last_call: float = 0.0

    async def __aenter__(self) -> "KiteRateLimiter":
        # Time gate first, then concurrency. Order matters: if we took
        # the semaphore first, slow handlers would hold concurrency
        # slots while the next caller waits its time slice — defeating
        # the rate cap when many callers queue up.
        async with self._interval_lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._semaphore.release()
