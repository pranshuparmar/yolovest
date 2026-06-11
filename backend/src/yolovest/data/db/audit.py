"""Skill-execution audit log.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import json
import logging
from typing import Any

from yolovest.timezone import now_utc

logger = logging.getLogger(__name__)


class AuditMixin:
    # Audit Log
    # ------------------------------------------------------------------

    async def log_audit(
        self,
        action_type: str,
        skill_name: str | None = None,
        input_summary: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        *,
        auto_commit: bool = True,
    ) -> None:
        """Log an audit entry for decision traceability.

        Set auto_commit=False when batching multiple audit entries,
        then call flush_audit() to commit them all at once.
        """
        ts_now = now_utc().isoformat()
        await self.conn.execute(
            "INSERT INTO audit_log (timestamp_ist, action_type, skill_name, "
            "input_summary, output_summary, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ts_now,
                action_type,
                skill_name,
                json.dumps(input_summary) if input_summary else None,
                json.dumps(output_summary) if output_summary else None,
                duration_ms,
            ),
        )
        if auto_commit:
            await self.conn.commit()

    async def flush_audit(self) -> None:
        """Commit any pending audit log entries."""
        await self.conn.commit()

    # ------------------------------------------------------------------
