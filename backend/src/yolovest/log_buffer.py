"""In-memory ring buffer for recent log lines.

Keeps the last N formatted log lines in memory, accessible via the
dashboard API for live log viewing without file I/O.
"""

import logging
from collections import deque


class LogBuffer(logging.Handler):
    """Logging handler that stores formatted lines in a ring buffer."""

    def __init__(self, maxlen: int = 500) -> None:
        super().__init__()
        self._buffer: deque[str] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
        except Exception:
            pass

    def get_lines(self, last_n: int | None = None) -> list[str]:
        """Return buffered log lines (newest last)."""
        lines = list(self._buffer)
        if last_n is not None:
            return lines[-last_n:]
        return lines

    def clear(self) -> None:
        self._buffer.clear()


def get_log_buffer() -> LogBuffer | None:
    """Find the LogBuffer handler attached to the root logger."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, LogBuffer):
            return handler
    return None
