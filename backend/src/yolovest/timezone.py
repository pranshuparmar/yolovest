"""Timezone utilities for YoloVest.

All datetime operations in YoloVest should use IST (Asia/Kolkata) since
this is an India-first trading platform and all market hours, OHLCV
timestamps, and scheduling are in IST.

Usage:
    from yolovest.timezone import IST, now_ist
"""

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Return the current time in IST (Asia/Kolkata), timezone-aware."""
    return datetime.now(IST)
