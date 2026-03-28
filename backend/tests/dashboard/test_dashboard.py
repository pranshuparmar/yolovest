"""Tests for FastAPI dashboard."""

import base64
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus
from yolovest.models.schemas import MLPrediction, OHLCVBar


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


def _make_bars(n: int) -> list[OHLCVBar]:
    """Create n dummy OHLCV bars for testing."""
    return [
        OHLCVBar(
            timestamp=datetime(2026, 1, 1 + i % 28 + 1),
            open=100.0, high=105.0, low=95.0, close=102.0, volume=10000,
        )
        for i in range(n)
    ]


def _hold_prediction(*_args, **_kwargs) -> MLPrediction:
    return MLPrediction(
        signal_type="HOLD", entry_price=100.0, target_price=105.0,
        stop_loss_price=95.0, position_size=1, holding_period="3d",
        confidence=0.45, model_version="test-v1",
    )


def _low_confidence_prediction(*_args, **_kwargs) -> MLPrediction:
    return MLPrediction(
        signal_type="BUY", entry_price=100.0, target_price=110.0,
        stop_loss_price=95.0, position_size=1, holding_period="3d",
        confidence=0.50, model_version="test-v1",
    )


def _high_confidence_prediction(*_args, **_kwargs) -> MLPrediction:
    return MLPrediction(
        signal_type="BUY", entry_price=100.0, target_price=110.0,
        stop_loss_price=95.0, position_size=1, holding_period="3d",
        confidence=0.85, model_version="test-v1",
    )


class TestDryRunDiagnostics:
    """Tests for dry-run signal diagnostics (filter_counts + rejection_details)."""

    def _setup_universe(self, dashboard_ctx, symbols: list[str]):
        """Configure mock DB to return stocks in the universe."""
        dashboard_ctx.db.get_nse_universe = AsyncMock(return_value=[
            {"symbol": s, "avg_daily_volume": 500_000} for s in symbols
        ])

    def test_dry_run_all_hold_shows_diagnostics(self, client, auth_headers, dashboard_ctx):
        symbols = ["RELIANCE", "TCS", "INFY"]
        self._setup_universe(dashboard_ctx, symbols)
        dashboard_ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        dashboard_ctx.ml = AsyncMock()
        dashboard_ctx.ml.predict_swing = AsyncMock(side_effect=_hold_prediction)
        dashboard_ctx.db.insert_dry_run_results = AsyncMock()

        resp = client.post("/api/dry-run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        diag = data["diagnostics"]
        assert diag["filter_counts"]["hold_signal"] == 3
        assert diag["filter_counts"]["passed"] == 0
        assert len(diag["rejection_details"]) == 3
        assert all(r["reason"] == "hold_signal" for r in diag["rejection_details"])

    def test_dry_run_low_confidence_shows_diagnostics(self, client, auth_headers, dashboard_ctx):
        symbols = ["RELIANCE", "TCS"]
        self._setup_universe(dashboard_ctx, symbols)
        dashboard_ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        dashboard_ctx.ml = AsyncMock()
        dashboard_ctx.ml.predict_swing = AsyncMock(side_effect=_low_confidence_prediction)
        dashboard_ctx.db.insert_dry_run_results = AsyncMock()

        resp = client.post("/api/dry-run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        diag = data["diagnostics"]
        assert diag["filter_counts"]["low_confidence"] == 2
        assert diag["filter_counts"]["passed"] == 0
        assert diag["min_confidence_threshold"] == 0.65
        assert all(r["reason"] == "low_confidence" for r in diag["rejection_details"])

    def test_dry_run_insufficient_bars_shows_diagnostics(self, client, auth_headers, dashboard_ctx):
        symbols = ["RELIANCE", "TCS"]
        self._setup_universe(dashboard_ctx, symbols)
        dashboard_ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(30))
        dashboard_ctx.ml = AsyncMock()
        dashboard_ctx.db.insert_dry_run_results = AsyncMock()

        resp = client.post("/api/dry-run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        diag = data["diagnostics"]
        assert diag["filter_counts"]["insufficient_bars"] == 2
        assert diag["filter_counts"]["passed"] == 0

    def test_dry_run_signals_pass_through_with_diagnostics(self, client, auth_headers, dashboard_ctx):
        symbols = ["RELIANCE"]
        self._setup_universe(dashboard_ctx, symbols)
        dashboard_ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        dashboard_ctx.ml = AsyncMock()
        dashboard_ctx.ml.predict_swing = AsyncMock(side_effect=_high_confidence_prediction)
        dashboard_ctx.db.insert_dry_run_results = AsyncMock()

        resp = client.post("/api/dry-run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["signals"]) == 1
        diag = data["diagnostics"]
        assert diag["filter_counts"]["passed"] == 1
        assert diag["filter_counts"]["hold_signal"] == 0
        assert diag["ml_available"] is True

    def test_dry_run_empty_shortlist_has_diagnostics(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.get_nse_universe = AsyncMock(return_value=[])

        resp = client.post("/api/dry-run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "diagnostics" in data
        assert data["diagnostics"]["filter_counts"]["passed"] == 0

    def test_dry_run_ml_unavailable_shows_diagnostics(self, client, auth_headers, dashboard_ctx):
        symbols = ["RELIANCE"]
        self._setup_universe(dashboard_ctx, symbols)
        dashboard_ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        dashboard_ctx.ml = None
        dashboard_ctx.db.insert_dry_run_results = AsyncMock()

        resp = client.post("/api/dry-run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        diag = data["diagnostics"]
        assert diag["ml_available"] is False
        assert diag["filter_counts"]["ml_unavailable"] == 1
        assert "warning" in data

    def test_dry_run_delete(self, client, auth_headers, dashboard_ctx):
        dashboard_ctx.db.delete_dry_run = AsyncMock(return_value=3)

        resp = client.delete("/api/dry-run/abc123", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["run_id"] == "abc123"
        assert data["deleted"] == 3
        dashboard_ctx.db.delete_dry_run.assert_called_once_with("abc123")
