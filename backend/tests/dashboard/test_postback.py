"""Tests for the Zerodha order postback endpoint — checksum verification
and business-logic routing to trade rows."""

import hashlib
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus


@pytest.fixture
def dashboard_ctx(sample_config, mock_db, mock_broker, mock_llm, mock_market_data, mock_notify):
    # api_secret is required for checksum verification
    sample_config.broker.api_secret = sample_config.broker.api_secret  # already set in conftest
    return AppContext(
        config=sample_config,
        db=mock_db,
        broker=mock_broker,
        llm=mock_llm,
        market_data=mock_market_data,
        notify=mock_notify,
        market_hours=MarketHoursChecker(sample_config),
        event_bus=EventBus(),
    )


@pytest.fixture
def client(dashboard_ctx):
    return TestClient(create_app(dashboard_ctx))


def make_payload(
    order_id: str, order_timestamp: str, api_secret: str,
    **overrides,
) -> dict:
    """Build a postback body with a valid checksum."""
    checksum = hashlib.sha256(
        f"{order_id}{order_timestamp}{api_secret}".encode(),
    ).hexdigest()
    body = {
        "order_id": order_id,
        "order_timestamp": order_timestamp,
        "checksum": checksum,
        "status": "COMPLETE",
        "tradingsymbol": "RELIANCE",
        "transaction_type": "BUY",
        "average_price": 2500.0,
    }
    body.update(overrides)
    return body


class TestChecksumVerification:
    def test_valid_checksum_accepted(self, client, dashboard_ctx):
        secret = dashboard_ctx.config.broker.api_secret.get_secret_value()
        # No matching trade — should still return 200, just no-op
        dashboard_ctx.db.find_trade_by_order_id = AsyncMock(return_value=(None, None))
        body = make_payload("ORD-1", "2026-05-13 10:00:00", secret)
        resp = client.post("/api/auth/zerodha/postback", json=body)
        assert resp.status_code == 200

    def test_invalid_checksum_rejected_with_401(self, client, dashboard_ctx):
        body = make_payload("ORD-1", "2026-05-13 10:00:00", "wrong_secret")
        resp = client.post("/api/auth/zerodha/postback", json=body)
        assert resp.status_code == 401

    def test_missing_checksum_rejected(self, client):
        resp = client.post("/api/auth/zerodha/postback", json={
            "order_id": "ORD-1",
            "order_timestamp": "2026-05-13 10:00:00",
            "status": "COMPLETE",
        })
        assert resp.status_code == 401

    def test_invalid_json_rejected(self, client):
        resp = client.post(
            "/api/auth/zerodha/postback",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


class TestBusinessLogic:
    def test_sl_complete_cancels_target_leg(self, client, dashboard_ctx):
        secret = dashboard_ctx.config.broker.api_secret.get_secret_value()
        # Match found on sl leg, trade has a resting target_order_id
        trade = {
            "trade_id": "T-001", "symbol": "INDUSTOWER",
            "signal_type": "BUY", "quantity": 48,
            "sl_order_id": "SL-XYZ", "target_order_id": "TGT-XYZ",
        }
        dashboard_ctx.db.find_trade_by_order_id = AsyncMock(return_value=(trade, "sl"))

        body = make_payload("SL-XYZ", "2026-05-13 10:53:00", secret, status="COMPLETE")
        resp = client.post("/api/auth/zerodha/postback", json=body)
        assert resp.status_code == 200

        dashboard_ctx.broker.cancel_order.assert_awaited_with("TGT-XYZ")
        dashboard_ctx.db.set_trade_target_order_id.assert_awaited_with("T-001", None)

    def test_target_complete_cancels_sl_leg(self, client, dashboard_ctx):
        secret = dashboard_ctx.config.broker.api_secret.get_secret_value()
        trade = {
            "trade_id": "T-002", "symbol": "INDUSTOWER",
            "signal_type": "BUY", "quantity": 48,
            "sl_order_id": "SL-XYZ", "target_order_id": "TGT-XYZ",
        }
        dashboard_ctx.db.find_trade_by_order_id = AsyncMock(return_value=(trade, "target"))

        body = make_payload("TGT-XYZ", "2026-05-13 10:53:00", secret, status="COMPLETE")
        resp = client.post("/api/auth/zerodha/postback", json=body)
        assert resp.status_code == 200

        dashboard_ctx.broker.cancel_order.assert_awaited_with("SL-XYZ")
        dashboard_ctx.db.set_trade_sl_order_id.assert_awaited_with("T-002", None)

    def test_entry_rejected_alerts_user(self, client, dashboard_ctx):
        secret = dashboard_ctx.config.broker.api_secret.get_secret_value()
        trade = {"trade_id": "T-003", "symbol": "RELIANCE", "signal_type": "BUY"}
        dashboard_ctx.db.find_trade_by_order_id = AsyncMock(return_value=(trade, "entry"))

        body = make_payload(
            "ORD-3", "2026-05-13 09:30:00", secret,
            status="REJECTED",
            status_message="Insufficient funds",
        )
        resp = client.post("/api/auth/zerodha/postback", json=body)
        assert resp.status_code == 200

        # send_trade_alert isn't called for this path — generic notify.send is
        dashboard_ctx.notify.send.assert_awaited()
        args = dashboard_ctx.notify.send.await_args.args[0]
        assert "REJECTED" in args
        assert "RELIANCE" in args

    def test_sl_rejected_alerts_user_loud(self, client, dashboard_ctx):
        """SL rejection is critical — the position is unprotected."""
        secret = dashboard_ctx.config.broker.api_secret.get_secret_value()
        trade = {
            "trade_id": "T-004", "symbol": "PNB",
            "signal_type": "BUY", "sl_order_id": "SL-PNB",
        }
        dashboard_ctx.db.find_trade_by_order_id = AsyncMock(return_value=(trade, "sl"))

        body = make_payload(
            "SL-PNB", "2026-05-13 09:31:00", secret, status="REJECTED",
        )
        resp = client.post("/api/auth/zerodha/postback", json=body)
        assert resp.status_code == 200

        dashboard_ctx.notify.send.assert_awaited()
        msg = dashboard_ctx.notify.send.await_args.args[0]
        assert "UNPROTECTED" in msg.upper() or "REJECTED" in msg.upper()

    def test_unknown_order_id_is_noop(self, client, dashboard_ctx):
        secret = dashboard_ctx.config.broker.api_secret.get_secret_value()
        dashboard_ctx.db.find_trade_by_order_id = AsyncMock(return_value=(None, None))

        body = make_payload("UNKNOWN-1", "2026-05-13 10:00:00", secret)
        resp = client.post("/api/auth/zerodha/postback", json=body)
        assert resp.status_code == 200
        # No state changes
        dashboard_ctx.broker.cancel_order.assert_not_awaited()


class TestPostbackHardening:
    def test_disabled_without_api_secret(self, sample_config, dashboard_ctx):
        """No broker secret configured → endpoint is disabled (403), not
        open to forged order updates."""
        from fastapi.testclient import TestClient
        from pydantic import SecretStr

        from yolovest.dashboard.app import create_app

        dashboard_ctx.config.broker.api_secret = SecretStr("")
        client = TestClient(create_app(dashboard_ctx))
        resp = client.post("/api/auth/zerodha/postback", json={
            "order_id": "ORD-1",
            "order_timestamp": "2026-05-13 10:00:00",
            "status": "COMPLETE",
        })
        assert resp.status_code == 403

    def test_missing_order_id_no_longer_bypasses_checksum(self, client):
        """A body without order_id used to skip verification entirely."""
        resp = client.post("/api/auth/zerodha/postback", json={
            "status": "COMPLETE",
            "checksum": "whatever",
        })
        assert resp.status_code == 400
