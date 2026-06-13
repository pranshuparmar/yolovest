"""Prediction scoreboard, recommendations, reports.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    Query,
)

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

    # ------------------------------------------------------------------
    # Predictions & Scoreboard
    # ------------------------------------------------------------------

    @app.get("/api/predictions/scoreboard")
    async def get_scoreboard(
        group_type: str | None = Query(None),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Prediction accuracy scoreboard."""
        return await ctx.db.get_prediction_scoreboard(group_type)

    @app.get("/api/recommendations")
    async def get_recommendations(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Today's signals with disposition (executed/pending/rejected).

        Each row is enriched with the derived target (predicted-exit) date
        and the cost-adjusted net gain / loss so the UI can show MIS/CNC,
        the target date, and approximate P&L after deductions.
        """
        from yolovest.dashboard.helpers import compute_signal_economics

        rows = await ctx.db.get_todays_recommendations()
        for r in rows:
            r.update(compute_signal_economics(
                ctx,
                signal_type=r.get("signal_type"),
                entry_price=r.get("entry_price"),
                target_price=r.get("target_price"),
                stop_loss_price=r.get("stop_loss_price"),
                position_size=r.get("position_size"),
                product=r.get("product"),
                base_date=r.get("created_at"),
                expected_holding_days=r.get("expected_holding_days"),
            ))
        return rows

    # ------------------------------------------------------------------
    # Historical Reports
    # ------------------------------------------------------------------

    @app.get("/api/reports")
    async def get_reports(
        report_type: str | None = Query(None, description="'daily' or 'weekly'"),
        start: str | None = Query(None),
        end: str | None = Query(None),
        limit: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Historical reports archive."""
        return await ctx.db.get_reports_history(
            report_type=report_type, start_date=start, end_date=end, limit=limit
        )

