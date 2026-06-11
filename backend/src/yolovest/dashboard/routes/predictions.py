"""Prediction listings, weekly summaries, risk exposure.

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
    # Predictions Detail & Failures
    # ------------------------------------------------------------------

    @app.get("/api/predictions/today")
    async def get_todays_predictions(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = Query(default=None),
        direction: str | None = Query(default=None, pattern=r"^(BUY|SELL)$"),
        model: str | None = Query(default=None),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Today's predictions with linked symbols and confidence."""
        return await ctx.db.get_todays_predictions(
            limit=limit, offset=offset, symbol=symbol,
            direction=direction, model=model, mode=ctx.config.mode,
        )

    @app.get("/api/predictions/unscored")
    async def get_unscored_predictions(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = Query(default=None),
        direction: str | None = Query(default=None, pattern=r"^(BUY|SELL)$"),
        model: str | None = Query(default=None),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """All predictions awaiting scoring."""
        return await ctx.db.get_all_awaiting_predictions(
            limit=limit, offset=offset, symbol=symbol,
            direction=direction, model=model,
        )

    @app.get("/api/predictions/outcomes")
    async def get_prediction_outcomes(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = Query(default=None),
        direction: str | None = Query(default=None, pattern=r"^(BUY|SELL)$"),
        direction_correct: int | None = Query(default=None, ge=0, le=1),
        target_hit: int | None = Query(default=None, ge=0, le=1),
        model: str | None = Query(default=None),
        min_confidence: float | None = Query(default=None, ge=0, le=1),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Scored prediction outcomes with filters."""
        return await ctx.db.get_prediction_outcomes_paginated(
            limit=limit, offset=offset, symbol=symbol,
            direction=direction, direction_correct=direction_correct,
            target_hit=target_hit, model=model,
            min_confidence=min_confidence,
        )

    # ------------------------------------------------------------------
    # Weekly Summary
    # ------------------------------------------------------------------

    @app.get("/api/weekly/trades")
    async def get_weekly_trades(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's trades."""
        return await ctx.db.get_weekly_trades(mode=ctx.config.mode)

    @app.get("/api/weekly/predictions")
    async def get_weekly_predictions(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's predictions."""
        return await ctx.db.get_weekly_predictions(mode=ctx.config.mode)

    @app.get("/api/weekly/llm-reviews")
    async def get_weekly_llm_reviews(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's LLM reviews with linked trade outcomes."""
        return await ctx.db.get_weekly_llm_reviews()

    # ------------------------------------------------------------------
    # Risk Exposure
    # ------------------------------------------------------------------

    @app.get("/api/risk-exposure")
    async def get_risk_exposure(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Portfolio risk breakdown by stock and sector."""
        portfolio = await ctx.db.get_portfolio_state(mode=ctx.config.mode)
        positions = await ctx.db.get_open_positions(mode=ctx.config.mode)
        stock_exposures = portfolio.get("stock_exposures", {})
        sector_counts = portfolio.get("sector_counts", {})

        # Build sector exposure from positions
        sector_exposure: dict[str, float] = {}
        for pos in positions:
            sector = await ctx.db.get_stock_sector(pos.get("symbol", ""))
            sector_name = sector or "Unknown"
            value = pos.get("fill_price", 0) * pos.get("quantity", 0)
            sector_exposure[sector_name] = sector_exposure.get(sector_name, 0) + value

        total_capital = portfolio.get("total_capital", 1)
        return {
            "total_capital": total_capital,
            "exposure_pct": portfolio.get("exposure_pct", 0),
            "stock_exposures": stock_exposures,
            "sector_counts": sector_counts,
            "sector_exposure_value": sector_exposure,
            "sector_exposure_pct": {
                k: round(v / total_capital * 100, 2) if total_capital > 0 else 0
                for k, v in sector_exposure.items()
            },
            "positions_count": len(positions),
        }

