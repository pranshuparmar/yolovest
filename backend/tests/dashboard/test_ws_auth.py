"""WebSocket /ws must require a valid session token.

The endpoint streams live trade / order / position events; without
auth, anyone who can reach the server receives them. The browser
WebSocket API can't set headers, so the token rides as ?token=.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus


@pytest.fixture
def ws_client(sample_config, mock_db, mock_broker, mock_llm, mock_market_data, mock_notify):
    ctx = AppContext(
        config=sample_config,
        db=mock_db,
        broker=mock_broker,
        llm=mock_llm,
        market_data=mock_market_data,
        notify=mock_notify,
        market_hours=MarketHoursChecker(sample_config),
        event_bus=EventBus(),
    )
    app = create_app(ctx)
    return TestClient(app)


def _login_token(client: TestClient) -> str:
    resp = client.post("/api/auth/login", json={"password": "yolovest"})
    assert resp.status_code == 200
    return resp.json()["token"]


class TestWebSocketAuth:
    def test_missing_token_rejected(self, ws_client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with ws_client.websocket_connect("/ws"):
                pass
        assert exc.value.code == 1008

    def test_garbage_token_rejected(self, ws_client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with ws_client.websocket_connect("/ws?token=not-a-real-token"):
                pass
        assert exc.value.code == 1008

    def test_valid_token_accepted(self, ws_client):
        token = _login_token(ws_client)
        with ws_client.websocket_connect(f"/ws?token={token}") as ws:
            # Handshake succeeded; the socket is connected and can send.
            ws.send_text("ping")
