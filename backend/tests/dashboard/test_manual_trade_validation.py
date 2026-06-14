"""Input-validation tests for the manual-trade endpoint.

A manual trade is placed LIVE immediately, so malformed input (inverted
stop-loss, negative price, bad side) must be rejected at the API boundary
before it reaches the broker — not stopped out a second later.
"""

import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus

AUTH = ("admin", "yolovest")  # default dashboard password in sample_config


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


def _trade(**overrides):
    body = {
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 100.0,
        "target_price": 110.0,
        "stop_loss_price": 95.0,
    }
    body.update(overrides)
    return body


class TestManualTradeValidation:
    def test_rejects_buy_with_stop_above_entry(self, client):
        # Inverted SL on a BUY would stop out instantly.
        r = client.post("/api/manual-trade", json=_trade(stop_loss_price=120.0), auth=AUTH)
        assert r.status_code == 400

    def test_rejects_buy_with_target_below_entry(self, client):
        r = client.post("/api/manual-trade", json=_trade(target_price=90.0), auth=AUTH)
        assert r.status_code == 400

    def test_rejects_sell_with_inverted_levels(self, client):
        # For SELL we need target < entry < stop_loss; this passes BUY-shaped
        # levels which are inverted for a short.
        r = client.post(
            "/api/manual-trade",
            json=_trade(signal_type="SELL", target_price=110.0, stop_loss_price=95.0),
            auth=AUTH,
        )
        assert r.status_code == 400

    def test_rejects_negative_price(self, client):
        r = client.post("/api/manual-trade", json=_trade(entry_price=-100.0), auth=AUTH)
        assert r.status_code == 400

    def test_rejects_non_numeric_price(self, client):
        r = client.post("/api/manual-trade", json=_trade(entry_price="abc"), auth=AUTH)
        assert r.status_code == 400

    def test_rejects_bad_side(self, client):
        r = client.post("/api/manual-trade", json=_trade(signal_type="HOLD"), auth=AUTH)
        assert r.status_code == 400

    def test_rejects_zero_quantity(self, client):
        r = client.post("/api/manual-trade", json=_trade(position_size=0), auth=AUTH)
        assert r.status_code == 400

    def test_missing_field_still_rejected(self, client):
        body = _trade()
        del body["stop_loss_price"]
        r = client.post("/api/manual-trade", json=body, auth=AUTH)
        assert r.status_code == 400

    def test_unauthenticated_rejected(self, client):
        # Sanity: the endpoint is auth-gated (no creds → 401).
        r = client.post("/api/manual-trade", json=_trade())
        assert r.status_code == 401
