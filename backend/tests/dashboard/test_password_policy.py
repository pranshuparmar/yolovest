"""Password-policy + live-mode guard tests.

The dashboard password is the only gate on real-money execution, so:
- a new password must be at least MIN_PASSWORD_LENGTH chars and never the
  shipped default
- the app refuses to switch to live trading while the password is still the
  default (anyone who reached the dashboard could otherwise arm live trading)
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus

# Default dashboard password in sample_config. TestClient is NOT used as a
# context manager, so the startup hook that would overwrite the password from
# the DB never runs — the password stays at this default.
DEFAULT_AUTH = ("admin", "yolovest")


@pytest.fixture
def client(sample_config, mock_db, mock_broker, mock_llm, mock_market_data, mock_notify):
    # update_config reads/writes config through these — give real return shapes.
    mock_db.get_all_config = AsyncMock(return_value={})
    mock_db.get_config = AsyncMock(return_value=None)
    mock_db.set_config_bulk = AsyncMock(return_value=None)
    mock_db.set_system_state = AsyncMock(return_value=None)
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
    return TestClient(create_app(ctx))


class TestChangePassword:
    def test_rejects_too_short(self, client):
        r = client.post(
            "/api/change-password", json={"new_password": "short"}, auth=DEFAULT_AUTH,
        )
        assert r.status_code == 400

    def test_rejects_the_shipped_default(self, client):
        r = client.post(
            "/api/change-password", json={"new_password": "yolovest"}, auth=DEFAULT_AUTH,
        )
        assert r.status_code == 400

    def test_accepts_valid_password(self, client):
        r = client.post(
            "/api/change-password", json={"new_password": "s3cretpass"}, auth=DEFAULT_AUTH,
        )
        assert r.status_code == 200
        assert r.json()["success"] is True


class TestLiveModeGuard:
    def test_refuses_live_while_password_is_default(self, client):
        r = client.put(
            "/api/config", json={"updates": {"mode": "live"}}, auth=DEFAULT_AUTH,
        )
        assert r.status_code == 400
        assert "default" in r.json()["detail"].lower()

    def test_paper_mode_is_not_gated(self, client):
        r = client.put(
            "/api/config", json={"updates": {"mode": "paper"}}, auth=DEFAULT_AUTH,
        )
        assert r.status_code == 200

    def test_live_allowed_after_password_change(self, client):
        new_pw = "s3cretpass"
        r1 = client.post(
            "/api/change-password", json={"new_password": new_pw}, auth=DEFAULT_AUTH,
        )
        assert r1.status_code == 200
        # Auth with the NEW password — and live is now permitted.
        r2 = client.put(
            "/api/config", json={"updates": {"mode": "live"}}, auth=("admin", new_pw),
        )
        assert r2.status_code == 200
