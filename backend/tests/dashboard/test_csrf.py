"""CSRF middleware tests.

State-changing requests must carry a valid X-CSRF-Token when they're
browser-issued (a Bearer session, or anything with an Origin/Referer header),
which now covers Basic-auth-from-a-browser too. Tokenless curl/CLI (no
Origin/Referer) is still allowed so the Basic-auth CLI path keeps working.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus

AUTH = ("admin", "yolovest")


@pytest.fixture
def client(sample_config, mock_db, mock_broker, mock_llm, mock_market_data, mock_notify):
    mock_db.set_system_state = AsyncMock(return_value=None)
    ctx = AppContext(
        config=sample_config, db=mock_db, broker=mock_broker, llm=mock_llm,
        market_data=mock_market_data, notify=mock_notify,
        market_hours=MarketHoursChecker(sample_config), event_bus=EventBus(),
    )
    return TestClient(create_app(ctx))


def _login(client):
    r = client.post("/api/auth/login", json={"password": "yolovest"})
    assert r.status_code == 200
    return r.json()  # {token, csrf_token, ...}


class TestCsrf:
    def test_bearer_without_token_blocked(self, client):
        tok = _login(client)["token"]
        r = client.post(
            "/api/change-password",
            json={"new_password": "s3cretpass"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 403
        assert "csrf" in r.json()["detail"].lower()

    def test_bearer_with_valid_token_passes(self, client):
        data = _login(client)
        r = client.post(
            "/api/change-password",
            json={"new_password": "s3cretpass"},
            headers={
                "Authorization": f"Bearer {data['token']}",
                "X-CSRF-Token": data["csrf_token"],
            },
        )
        assert r.status_code == 200

    def test_browser_basic_auth_without_token_blocked(self, client):
        # A browser-issued request (Origin present) using Basic auth must now
        # also carry the CSRF token — closes the Basic-auth CSRF gap.
        r = client.post(
            "/api/change-password",
            json={"new_password": "s3cretpass"},
            headers={"Origin": "https://evil.example.com"},
            auth=AUTH,
        )
        assert r.status_code == 403

    def test_cli_basic_auth_without_origin_allowed(self, client):
        # curl/CLI: Basic auth, no Origin/Referer, no token — still works.
        r = client.post(
            "/api/change-password",
            json={"new_password": "s3cretpass"},
            auth=AUTH,
        )
        assert r.status_code == 200

    def test_invalid_token_blocked(self, client):
        data = _login(client)
        r = client.post(
            "/api/change-password",
            json={"new_password": "s3cretpass"},
            headers={
                "Authorization": f"Bearer {data['token']}",
                "X-CSRF-Token": "not-the-real-token",
            },
        )
        assert r.status_code == 403
        assert "invalid" in r.json()["detail"].lower()
