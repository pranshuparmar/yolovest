"""Dashboard WebSocket: client registry, broadcast, /ws endpoint."""

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from yolovest.dashboard.security import _verify_token

if TYPE_CHECKING:
    from fastapi import FastAPI

    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)

_ws_clients: set[WebSocket] = set()


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


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    # ------------------------------------------------------------------
    # WebSocket Live Updates
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WebSocket for real-time trade and position updates.

        Requires the session token as a `?token=` query param — the
        browser WebSocket API can't set an Authorization header. An
        invalid/missing token closes the handshake with 1008 (policy
        violation); the client re-logs-in and reconnects with a fresh
        token (tokens are per-process, so a backend restart invalidates
        them by design).
        """
        token = websocket.query_params.get("token", "")
        try:
            _verify_token(token)
        except HTTPException:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        _ws_clients.add(websocket)
        try:
            while True:
                # Keep connection alive, listen for client messages
                await websocket.receive_text()
        except WebSocketDisconnect:
            _ws_clients.discard(websocket)
