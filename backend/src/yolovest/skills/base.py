"""Base skill class for OpenClaw agent skills.

All YoloVest skills extend SkillBase and implement execute().
Skills are the discrete, independently invocable capabilities of the trading agent.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

from yolovest.timezone import now_ist


class SkillTrigger(Enum):
    """How a skill gets invoked."""

    HEARTBEAT = "heartbeat"  # called every heartbeat during relevant hours
    CRON = "cron"  # called on a cron schedule
    EVENT = "event"  # called in response to a specific event (e.g. signal generated)
    MANUAL = "manual"  # called via Telegram command or dashboard


@dataclass
class SkillResult:
    """Standard result returned by every skill execution."""

    success: bool
    skill_name: str
    timestamp: datetime = field(default_factory=now_ist)
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0


class SkillBase(ABC):
    """Abstract base for all OpenClaw agent skills.

    Each skill:
    - Has a unique name and description
    - Declares its trigger type and schedule
    - Implements execute() as the main entry point
    - Returns a SkillResult for audit logging
    - Has access to shared context (config, db, broker, llm)
    """

    name: ClassVar[str]
    description: ClassVar[str]
    trigger: ClassVar[SkillTrigger]
    # Plain (not ClassVar): square-off mutates self.schedule at runtime
    # when the early-close calendar shifts the square-off time.
    schedule: str | None = None  # cron expression if trigger is CRON

    def __init__(self, context: Any) -> None:
        """Initialize with shared application context (config, db, broker, llm, etc.)."""
        self.ctx = context

    @abstractmethod
    async def execute(self, **kwargs: Any) -> SkillResult:
        """Run the skill. Override in subclasses."""
        ...

    @abstractmethod
    def should_run(self) -> bool:
        """Check preconditions — is it the right time/state to run this skill?"""
        ...

    def compute_schedule(self) -> str | None:
        """Return this skill's CRON schedule, resolved LIVE from config.

        The CRON scheduler calls this every tick instead of reading the
        cached ``self.schedule`` attribute, so a schedule changed via the
        Settings UI (which hot-replaces ``ctx.config``) takes effect
        without a restart. The default returns the cached attribute;
        skills whose schedule derives from a config key override this to
        re-read it (and keep ``__init__`` setting ``self.schedule`` from
        here so the cached value and the live value never diverge).
        """
        return self.schedule

    def _ingest_source(self, symbol: str, default: str) -> str:
        """Resolve the actual data provider behind this symbol's last
        OHLCV fetch (kite / jugaad / yfinance / tvdatafeed) so it can be
        stamped into `ohlcv.source` for provenance. Reads the ingester's
        per-symbol fetch metadata; falls back to `default` when the
        market-data layer doesn't expose it (tests, a bare provider) or
        the source is unknown.
        """
        get_meta = getattr(self.ctx.market_data, "get_fetch_meta", None)
        if not callable(get_meta):
            return default
        try:
            meta = get_meta(symbol)
        except Exception:
            return default
        if isinstance(meta, dict):
            src = meta.get("source")
            if isinstance(src, str) and src:
                return src
        return default

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event to the event bus (bridged to WebSocket clients)."""
        try:
            from yolovest.events import Event
            await self.ctx.event_bus.publish(Event(event_type=event_type, data=data))
        except Exception:
            logger.debug("Failed to broadcast event %s", event_type, exc_info=True)

    async def safe_execute(self, **kwargs: Any) -> SkillResult:
        """Wrapper that catches exceptions and returns error SkillResult."""
        start = time.monotonic()
        try:
            result = await self.execute(**kwargs)
            result.duration_ms = (time.monotonic() - start) * 1000
            return result
        except Exception as e:
            logger.exception("Skill '%s' failed with unhandled exception", self.name)
            return SkillResult(
                success=False,
                skill_name=self.name,
                error=str(e),
                duration_ms=(time.monotonic() - start) * 1000,
            )
