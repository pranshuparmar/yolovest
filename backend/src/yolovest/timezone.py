"""Timezone utilities for YoloVest.

All timestamps stored in the database use UTC. IST is used only for:
- Market hours logic (is_market_hours, square-off timing)
- Date boundary calculations (today's trades, weekly reports)
- Display formatting (handled by frontend)

Usage:
    from yolovest.timezone import IST, UTC, now_utc, now_ist
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = UTC


def now_utc() -> datetime:
    """Return the current time in UTC, timezone-aware."""
    return datetime.now(UTC)


def now_ist() -> datetime:
    """Return the current time in IST (Asia/Kolkata), timezone-aware.

    Use for market-hours logic and date boundary calculations only.
    For DB storage, use now_utc() instead.
    """
    return datetime.now(IST)
