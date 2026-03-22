"""FastAPI dashboard for YoloVest.

Covers: FR-8.1 (portfolio overview), FR-8.2 (WebSocket), FR-8.3 (trade detail),
FR-8.7 (historical reports), FR-8.9 (basic auth).

All endpoints read from the shared database via AppContext.
"""

import json
import logging
import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from yolovest.context import AppContext

logger = logging.getLogger(__name__)

security = HTTPBasic()

# WebSocket connection manager
_ws_clients: set[WebSocket] = set()


def create_app(ctx: AppContext) -> FastAPI:
    """Create and configure the FastAPI dashboard application."""
    app = FastAPI(
        title="YoloVest Dashboard",
        description="Autonomous AI-driven Indian stock trading platform",
        version="0.1.0",
    )

    # Store context for dependency injection
    app.state.ctx = ctx

    # Auth config
    dash_password = (
        ctx.config.dashboard.password
        if hasattr(ctx.config.dashboard, "password")
        else "yolovest"
    )

    def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:  # noqa: B008
        """FR-8.9: Basic password protection."""
        correct = secrets.compare_digest(credentials.password, dash_password)
        if not correct:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    # ------------------------------------------------------------------
    # FR-8.1: Portfolio Overview
    # ------------------------------------------------------------------

    @app.get("/api/portfolio")
    async def get_portfolio(user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Portfolio overview: capital, exposure, open positions, PnL."""
        portfolio = await ctx.db.get_portfolio_state()
        return portfolio

    @app.get("/api/positions")
    async def get_positions(user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """Current open positions."""
        return await ctx.db.get_open_positions()

    @app.get("/api/trades/today")
    async def get_todays_trades(user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """Today's trades."""
        return await ctx.db.get_todays_trades()

    @app.get("/api/trades")
    async def get_trades(
        start: str | None = Query(None, description="Start date YYYY-MM-DD"),
        end: str | None = Query(None, description="End date YYYY-MM-DD"),
        symbol: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Trade history with optional date range and symbol filter."""
        return await ctx.db.get_trades_history(
            start_date=start, end_date=end, symbol=symbol, limit=limit
        )

    @app.get("/api/equity-curve")
    async def get_equity_curve(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Daily equity curve data for charting."""
        return await ctx.db.get_equity_curve(days=days)

    # ------------------------------------------------------------------
    # FR-8.3: Trade Detail View
    # ------------------------------------------------------------------

    @app.get("/api/trades/{trade_id}")
    async def get_trade_detail(
        trade_id: str, user: str = Depends(verify_credentials)
    ) -> dict[str, Any]:
        """Full reasoning chain for a trade: signal → risk → LLM → execution → outcome."""
        detail = await ctx.db.get_trade_detail(trade_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Trade not found")
        return detail

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

    # ------------------------------------------------------------------
    # FR-8.7: Historical Reports
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

    # ------------------------------------------------------------------
    # Watchlist & Market Data
    # ------------------------------------------------------------------

    @app.get("/api/watchlist")
    async def get_watchlist(user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """Current watchlist with scores."""
        return await ctx.db.get_watchlist()

    @app.get("/api/sectors")
    async def get_sector_rotation(user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Sector rotation analysis."""
        return await ctx.db.get_sector_rotation()

    # ------------------------------------------------------------------
    # System
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health_check() -> dict[str, Any]:
        """System health (no auth required)."""
        db_ok = await ctx.db.health_check()
        return {
            "status": "ok" if db_ok else "degraded",
            "database": db_ok,
            "mode": ctx.config.mode,
        }

    @app.get("/api/audit")
    async def get_audit_log(
        limit: int = Query(50, ge=1, le=500),
        action_type: str | None = Query(None),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Recent audit log entries (FR-8.8)."""
        return await ctx.db.get_audit_log(limit=limit, action_type=action_type)

    # ------------------------------------------------------------------
    # FR-8.2: WebSocket Live Updates
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WebSocket for real-time trade and position updates."""
        await websocket.accept()
        _ws_clients.add(websocket)
        try:
            while True:
                # Keep connection alive, listen for client messages
                await websocket.receive_text()
        except WebSocketDisconnect:
            _ws_clients.discard(websocket)

    return app


async def broadcast_ws(event_type: str, data: dict[str, Any]) -> None:
    """Broadcast an event to all connected WebSocket clients."""
    global _ws_clients
    message = json.dumps({"type": event_type, "data": data})
    disconnected = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    _ws_clients -= disconnected
