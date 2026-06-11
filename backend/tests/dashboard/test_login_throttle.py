"""Brute-force protection on password attempts.

Both /api/auth/login and the Basic-auth fallback must lock a source out
after repeated failures — Basic auth on any protected endpoint would
otherwise remain an unthrottled password oracle.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import _LoginThrottle, create_app
from yolovest.events import EventBus


@pytest.fixture
def client(sample_config, mock_db, mock_broker, mock_llm, mock_market_data, mock_notify):
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


def _basic(pw: str) -> dict[str, str]:
    creds = base64.b64encode(f"admin:{pw}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


class TestLoginEndpointThrottle:
    def test_lockout_after_repeated_failures(self, client):
        for _ in range(_LoginThrottle.PER_IP_THRESHOLD):
            resp = client.post("/api/auth/login", json={"password": "wrong"})
            assert resp.status_code == 401
        resp = client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_locked_source_cannot_login_even_with_correct_password(self, client):
        for _ in range(_LoginThrottle.PER_IP_THRESHOLD):
            client.post("/api/auth/login", json={"password": "wrong"})
        resp = client.post("/api/auth/login", json={"password": "yolovest"})
        assert resp.status_code == 429

    def test_success_resets_counter(self, client):
        for _ in range(_LoginThrottle.PER_IP_THRESHOLD - 1):
            client.post("/api/auth/login", json={"password": "wrong"})
        resp = client.post("/api/auth/login", json={"password": "yolovest"})
        assert resp.status_code == 200
        # Counter cleared — a fresh failure is a 401, not a 429.
        resp = client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401


class TestBasicAuthThrottle:
    def test_basic_auth_failures_count_toward_lockout(self, client):
        for _ in range(_LoginThrottle.PER_IP_THRESHOLD):
            resp = client.get("/api/portfolio", headers=_basic("wrong"))
            assert resp.status_code == 401
        resp = client.get("/api/portfolio", headers=_basic("wrong"))
        assert resp.status_code == 429

    def test_bearer_token_unaffected_by_lockout(self, client):
        token = client.post(
            "/api/auth/login", json={"password": "yolovest"},
        ).json()["token"]
        for _ in range(_LoginThrottle.PER_IP_THRESHOLD + 1):
            client.get("/api/portfolio", headers=_basic("wrong"))
        # An established session keeps working; the lockout only gates
        # password attempts.
        resp = client.get(
            "/api/portfolio", headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200


class TestThrottleUnit:
    def test_exponential_backoff_caps(self):
        t = _LoginThrottle()
        for _ in range(50):
            t.record_failure("1.2.3.4")
        assert t.locked_for("1.2.3.4") <= _LoginThrottle.MAX_LOCK_SEC + 1

    def test_global_damper_on_spoofed_sources(self):
        t = _LoginThrottle()
        for i in range(_LoginThrottle.GLOBAL_THRESHOLD):
            t.record_failure(f"10.0.0.{i}")
        # A brand-new source is also briefly locked.
        assert t.locked_for("99.99.99.99") > 0

    def test_tracked_ip_bound(self):
        t = _LoginThrottle()
        for i in range(_LoginThrottle.MAX_TRACKED_IPS + 100):
            t.record_failure(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
        assert len(t._by_ip) <= _LoginThrottle.MAX_TRACKED_IPS
