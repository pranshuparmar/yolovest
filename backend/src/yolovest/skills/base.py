"""Base skill class for OpenClaw agent skills.

All YoloVest skills extend SkillBase and implement execute().
Skills are the discrete, independently invocable capabilities of the trading agent.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


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
    timestamp: datetime = field(default_factory=datetime.now)
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

    name: str
    description: str
    trigger: SkillTrigger
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

    async def safe_execute(self, **kwargs: Any) -> SkillResult:
        """Wrapper that catches exceptions and returns error SkillResult."""
        start = datetime.now()
        try:
            result = await self.execute(**kwargs)
            result.duration_ms = (datetime.now() - start).total_seconds() * 1000
            return result
        except Exception as e:
            return SkillResult(
                success=False,
                skill_name=self.name,
                error=str(e),
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
