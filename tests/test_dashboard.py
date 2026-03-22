"""Tests for FastAPI dashboard (Phase 5, FR-8)."""

import base64
import json

import pytest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from yolovest.config import AppConfig
from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus


@pytest.fixture
def dashboard_ctx(sample_config, mock_db, mock_broker, mock_llm, mock_market_data, mock_notify):
    """AppContext for dashboard tests."""
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
    """TestClient with auth headers."""
    app = create_app(dashboard_ctx)
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Basic auth headers."""
    creds = base64.b64encode(b"admin:yolovest").decode()
    return {"Authorization": f"Basic {creds}"}


class TestAuth:
    def test_unauthenticated_request_rejected(self, client):
        resp = client.get("/api/portfolio")
        assert resp.status_code == 401

    def test_wrong_password_rejected(self, client):
        creds = base64.b64encode(b"admin:wrongpass").decode()
        resp = client.get("/api/portfolio", headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 401

    def test_correct_password_accepted(self, client, auth_headers):
        resp = client.get("/api/portfolio", headers=auth_headers)
        assert resp.status_code == 200


class TestHealthEndpoint:
    def test_health_no_auth_required(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "paper"


class TestPortfolioEndpoints:
    def test_get_portfolio(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_portfolio_state = AsyncMock(return_value={
            "total_capital": 100000,
            "open_positions": 2,
            "daily_pnl_pct": 0.015,
        })

        resp = client.get("/api/portfolio", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_capital"] == 100000

    def test_get_positions(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_open_positions = AsyncMock(return_value=[
            {"symbol": "RELIANCE", "quantity": 10, "entry_price": 2500},
        ])

        resp = client.get("/api/positions", headers=auth_headers)

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_todays_trades(self, client, auth_headers):
        resp = client.get("/api/trades/today", headers=auth_headers)
        assert resp.status_code == 200


class TestTradesEndpoints:
    def test_get_trades_with_filters(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_trades_history = AsyncMock(return_value=[
            {"trade_id": "T-1", "symbol": "RELIANCE", "pnl": 500},
        ])

        resp = client.get(
            "/api/trades?start=2026-03-01&symbol=RELIANCE&limit=10",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_trade_detail(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_trade_detail = AsyncMock(return_value={
            "trade_id": "T-1",
            "symbol": "RELIANCE",
            "llm_review": {"decision": "APPROVE", "reasoning": "Good"},
            "prediction": {"direction_correct": True},
            "signal": {"confidence_score": 0.85},
            "audit_trail": [],
        })

        resp = client.get("/api/trades/T-1", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["trade_id"] == "T-1"
        assert data["llm_review"]["decision"] == "APPROVE"

    def test_get_trade_detail_not_found(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_trade_detail = AsyncMock(return_value=None)

        resp = client.get("/api/trades/NONEXISTENT", headers=auth_headers)

        assert resp.status_code == 404


class TestEquityCurve:
    def test_get_equity_curve(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_equity_curve = AsyncMock(return_value=[
            {"date": "2026-03-20", "daily_pnl": 500, "cumulative_pnl": 500, "trade_count": 3},
            {"date": "2026-03-21", "daily_pnl": -200, "cumulative_pnl": 300, "trade_count": 2},
        ])

        resp = client.get("/api/equity-curve?days=7", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[1]["cumulative_pnl"] == 300


class TestScoreboard:
    def test_get_prediction_scoreboard(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_prediction_scoreboard = AsyncMock(return_value=[
            {"group_key": "overall", "accuracy": 0.72, "total_predictions": 50},
        ])

        resp = client.get("/api/predictions/scoreboard", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()[0]["accuracy"] == 0.72


class TestReports:
    def test_get_reports_history(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_reports_history = AsyncMock(return_value=[
            {"report_type": "daily", "report_date": "2026-03-21", "content": {"total_pnl": 1500}},
        ])

        resp = client.get("/api/reports?report_type=daily", headers=auth_headers)

        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestWatchlist:
    def test_get_watchlist(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE", "composite_score": 0.85, "sector": "Energy"},
        ])

        resp = client.get("/api/watchlist", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()[0]["symbol"] == "RELIANCE"


class TestAuditLog:
    def test_get_audit_log(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_audit_log = AsyncMock(return_value=[
            {"action_type": "trade_executed", "timestamp_ist": "2026-03-21T10:30:00"},
        ])

        resp = client.get("/api/audit?limit=10", headers=auth_headers)

        assert resp.status_code == 200
        assert len(resp.json()) == 1
