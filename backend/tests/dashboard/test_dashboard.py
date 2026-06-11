"""Tests for FastAPI dashboard."""

import base64
from datetime import datetime
from unittest.mock import AsyncMock, Mock

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
        dashboard_ctx.config.strategy.mode = "short_term"
        dashboard_ctx.config.strategy.allowed_holding_periods = ["short_term", "long_term"]
        dashboard_ctx.db.get_nse_universe = AsyncMock(return_value=[
            {"symbol": s, "avg_daily_volume": 500_000} for s in symbols
        ])

    def test_dry_run_all_hold_shows_diagnostics(self, client, auth_headers, dashboard_ctx):
        symbols = ["RELIANCE", "TCS", "INFY"]
        self._setup_universe(dashboard_ctx, symbols)
        dashboard_ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        dashboard_ctx.ml = AsyncMock()
        # get_effective_thresholds is a SYNC method on the real model; a
        # blanket AsyncMock would make it return an un-awaited coroutine
        # that the dry-run conviction diagnostics can't serialize.
        dashboard_ctx.ml.get_effective_thresholds = Mock(
            return_value={"buy": 0.5, "sell": 0.5}
        )
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
        # get_effective_thresholds is a SYNC method on the real model; a
        # blanket AsyncMock would make it return an un-awaited coroutine
        # that the dry-run conviction diagnostics can't serialize.
        dashboard_ctx.ml.get_effective_thresholds = Mock(
            return_value={"buy": 0.5, "sell": 0.5}
        )
        dashboard_ctx.ml.predict_swing = AsyncMock(side_effect=_low_confidence_prediction)
        dashboard_ctx.db.insert_dry_run_results = AsyncMock()

        resp = client.post("/api/dry-run", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        diag = data["diagnostics"]
        assert diag["filter_counts"]["low_confidence"] == 2
        assert diag["filter_counts"]["passed"] == 0
        assert diag["min_confidence_threshold"] == 0.60
        assert all(r["reason"] == "low_confidence" for r in diag["rejection_details"])

    def test_dry_run_insufficient_bars_shows_diagnostics(self, client, auth_headers, dashboard_ctx):
        symbols = ["RELIANCE", "TCS"]
        self._setup_universe(dashboard_ctx, symbols)
        dashboard_ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(30))
        dashboard_ctx.ml = AsyncMock()
        # get_effective_thresholds is a SYNC method on the real model; a
        # blanket AsyncMock would make it return an un-awaited coroutine
        # that the dry-run conviction diagnostics can't serialize.
        dashboard_ctx.ml.get_effective_thresholds = Mock(
            return_value={"buy": 0.5, "sell": 0.5}
        )
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
        # get_effective_thresholds is a SYNC method on the real model; a
        # blanket AsyncMock would make it return an un-awaited coroutine
        # that the dry-run conviction diagnostics can't serialize.
        dashboard_ctx.ml.get_effective_thresholds = Mock(
            return_value={"buy": 0.5, "sell": 0.5}
        )
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


class TestExportImport:
    """Backup / model / config download + upload for cross-machine moves."""

    def test_config_export_download(self, client, auth_headers):
        resp = client.get("/api/config/export", headers=auth_headers)
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("content-disposition", "")
        body = resp.json()
        assert "config" in body and len(body["config"]) > 0
        # File-only keys (secrets/paths) must be excluded
        assert "database.path" not in body["config"]

    def test_config_import_roundtrip(self, client, auth_headers, dashboard_ctx):
        import io
        import json as _json
        dashboard_ctx.db.get_all_config = AsyncMock(return_value={})
        dashboard_ctx.db.set_config_bulk = AsyncMock()
        payload = _json.dumps({"config": {"risk.max_open_positions": 7}}).encode()
        resp = client.post(
            "/api/config/import", headers=auth_headers,
            files={"file": ("cfg.json", io.BytesIO(payload), "application/json")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] == 1
        dashboard_ctx.db.set_config_bulk.assert_awaited_once()

    def test_backup_upload_rejects_non_sqlite(self, client, auth_headers):
        import io
        resp = client.post(
            "/api/backups/upload", headers=auth_headers,
            files={"file": ("x.db", io.BytesIO(b"not a sqlite file"), "application/octet-stream")},
        )
        assert resp.status_code == 400

    def test_backup_upload_accepts_sqlite(self, client, auth_headers, dashboard_ctx, tmp_path):
        import io
        dashboard_ctx.config.database.backup_dir = str(tmp_path)
        dashboard_ctx.db.invalidate_storage_stats_cache = lambda: None
        # Minimal valid SQLite header
        data = b"SQLite format 3\x00" + b"\x00" * 100
        resp = client.post(
            "/api/backups/upload", headers=auth_headers,
            files={"file": ("yolovest_x.db", io.BytesIO(data), "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["filename"] == "yolovest_x.db"

    def test_backup_download_missing_returns_404(self, client, auth_headers):
        # A normal-but-nonexistent filename reaches the handler and 404s
        # (proves the endpoint is wired and only serves real files, never
        # an arbitrary path).
        resp = client.get("/api/backups/yolovest_nope.db/download", headers=auth_headers)
        assert resp.status_code == 404

    def test_download_token_issued(self, client, auth_headers):
        resp = client.get("/api/download-token", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json().get("token")

    def test_download_with_query_token_authorizes(self, client, auth_headers):
        # Native downloads can't send the Authorization header, so a valid
        # ?token= must authorize on its own. A missing file then 404s
        # (not 401) — proving the token auth passed without a header.
        token = client.get("/api/download-token", headers=auth_headers).json()["token"]
        resp = client.get(f"/api/backups/yolovest_nope.db/download?token={token}")
        assert resp.status_code == 404

    def test_download_with_bad_token_rejected(self, client):
        resp = client.get("/api/backups/x.db/download?token=garbage.sig")
        assert resp.status_code == 401

    def test_download_without_auth_rejected(self, client):
        resp = client.get("/api/backups/x.db/download")
        assert resp.status_code == 401

    def test_model_upload_rejects_non_pkl(self, client, auth_headers):
        import io
        resp = client.post(
            "/api/ml-models/upload", headers=auth_headers,
            files={"file": ("x.txt", io.BytesIO(b"nope"), "text/plain")},
        )
        assert resp.status_code == 400

    def _write_artifact(self, version: str, schema_version, *, model_dir="models"):
        """Dump a minimal model bundle to <model_dir>/<version>.pkl (relative
        to cwd — pair with monkeypatch.chdir). schema_version=None omits the
        key (simulates a pre-versioning legacy artifact)."""
        import os

        import joblib
        os.makedirs(model_dir, exist_ok=True)
        artifact = {"model": {}, "metrics": {"sharpe_ratio": 1.0},
                    "feature_names": ["rsi_14", "macd_histogram_pct"]}
        if schema_version is not None:
            artifact["schema_version"] = schema_version
        joblib.dump(artifact, os.path.join(model_dir, f"{version}.pkl"))

    def test_model_import_matching_schema_succeeds(self, client, auth_headers, dashboard_ctx, tmp_path, monkeypatch):
        from yolovest.data.features import MODEL_SCHEMA_VERSION
        monkeypatch.chdir(tmp_path)
        dashboard_ctx.db.save_model_version = AsyncMock()
        self._write_artifact("swing_v1", MODEL_SCHEMA_VERSION)
        resp = client.post(
            "/api/ml-models/import", headers=auth_headers,
            json={"model_type": "swing", "version": "swing_v1"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] is True
        dashboard_ctx.db.save_model_version.assert_awaited_once()

    def test_model_import_schema_mismatch_rejected(self, client, auth_headers, dashboard_ctx, tmp_path, monkeypatch):
        from yolovest.data.features import MODEL_SCHEMA_VERSION
        monkeypatch.chdir(tmp_path)
        dashboard_ctx.db.save_model_version = AsyncMock()
        self._write_artifact("swing_v2", MODEL_SCHEMA_VERSION + 1)
        resp = client.post(
            "/api/ml-models/import", headers=auth_headers,
            json={"model_type": "swing", "version": "swing_v2"},
        )
        assert resp.status_code == 422, resp.text
        assert "schema" in resp.json()["detail"].lower()
        dashboard_ctx.db.save_model_version.assert_not_awaited()

    def test_model_import_legacy_no_schema_rejected(self, client, auth_headers, dashboard_ctx, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        dashboard_ctx.db.save_model_version = AsyncMock()
        self._write_artifact("swing_v3", None)
        resp = client.post(
            "/api/ml-models/import", headers=auth_headers,
            json={"model_type": "swing", "version": "swing_v3"},
        )
        assert resp.status_code == 422, resp.text

    def test_model_import_force_overrides_schema(self, client, auth_headers, dashboard_ctx, tmp_path, monkeypatch):
        from yolovest.data.features import MODEL_SCHEMA_VERSION
        monkeypatch.chdir(tmp_path)
        dashboard_ctx.db.save_model_version = AsyncMock()
        self._write_artifact("swing_v4", MODEL_SCHEMA_VERSION + 1)
        resp = client.post(
            "/api/ml-models/import", headers=auth_headers,
            json={"model_type": "swing", "version": "swing_v4", "force": True},
        )
        assert resp.status_code == 200, resp.text
        assert any("schema mismatch" in w.lower() for w in resp.json()["warnings"])

    def test_model_upload_accepts_valid_bundle(self, client, auth_headers, tmp_path, monkeypatch):
        # Exercises the joblib.load happy path (asyncio.to_thread) so a
        # missing `import asyncio` can't silently NameError on the path
        # the reject-tests never reach. _model_dir() falls back to
        # "./models", so chdir into tmp keeps the write out of the repo.
        import io

        import joblib
        monkeypatch.chdir(tmp_path)
        buf = io.BytesIO()
        joblib.dump({"model": {}, "metrics": {"sharpe_ratio": 1.5}}, buf)
        buf.seek(0)
        resp = client.post(
            "/api/ml-models/upload", headers=auth_headers,
            files={"file": ("swing_v2.pkl", buf, "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "swing_v2"
        assert body["metrics"]["sharpe_ratio"] == 1.5
