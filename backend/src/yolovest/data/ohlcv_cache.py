"""Short-TTL in-memory cache for on-demand OHLCV fetches.

The Quick Review and symbol deep-dive fetch daily history live from the
provider chain for symbols outside the ingested universe — a few seconds each
time. This memoises that fetch briefly (per-process) so repeat looks at the
same symbol are instant. Transient and never persisted; bounded so it can't
grow without limit. Universe symbols never reach here (they're served from the
DB first), so this only ever holds ad-hoc, off-universe fetches.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yolovest.context import MarketDataProtocol

_TTL_SEC = 300.0      # 5 minutes — fresh enough for a manual review
_MAX_ENTRIES = 256    # bound memory under a tab cycling through many symbols

# key: (UPPER_SYMBOL, days) -> (expiry_monotonic, bars)
_cache: dict[tuple[str, int], tuple[float, list[Any]]] = {}


async def get_ohlcv_cached(
    market_data: MarketDataProtocol, symbol: str, days: int,
) -> list[Any]:
    """Fetch daily OHLCV from the provider chain, memoised for ~5 min.

    Empty results are not cached (so a transient provider miss is retried).
    """
    key = (symbol.upper(), int(days))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    bars = await market_data.get_ohlcv(symbol, "daily", days=days)
    if bars:
        _prune(now)
        _cache[key] = (now + _TTL_SEC, bars)
    return bars


def _prune(now: float) -> None:
    """Drop expired entries; if still at the cap, evict soonest-to-expire."""
    for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
        _cache.pop(k, None)
    if len(_cache) >= _MAX_ENTRIES:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[
            : len(_cache) - _MAX_ENTRIES + 1
        ]:
            _cache.pop(k, None)


def clear() -> None:
    """Drop all cached entries (used by tests)."""
    _cache.clear()
