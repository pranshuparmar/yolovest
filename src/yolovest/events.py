"""Simple async in-process event bus.

Publish/subscribe pattern for inter-skill communication.
No external dependencies — pure asyncio.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """An event published on the bus."""

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


# Type alias for async event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """In-process async event bus.

    Usage:
        bus = EventBus()
        bus.subscribe("signal.generated", my_handler)
        await bus.publish(Event(event_type="signal.generated", data={"symbol": "RELIANCE"}))
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for an event type."""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler for an event type."""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def subscriber_count(self, event_type: str) -> int:
        """Return the number of subscribers for an event type."""
        return len(self._handlers.get(event_type, []))

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribed handlers.

        Handlers are called sequentially. Errors in handlers are logged
        but do not prevent other handlers from running.
        """
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "Error in handler for event '%s'", event.event_type
                )
