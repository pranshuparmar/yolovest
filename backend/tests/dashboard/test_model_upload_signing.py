"""Endpoint tests proving the model-upload boundary rejects unsigned/forged
artifacts when MODEL_SIGNING_KEY is set — i.e. the malicious-pickle RCE is
closed before joblib.load runs.
"""

import io

import joblib
import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus
from yolovest.strategy.model_signing import wrap

AUTH = ("admin", "yolovest")
KEY = "secret-signing-key"


def _bundle_bytes(tmp_path):
    """Serialize a minimal valid YoloVest model bundle to bytes."""
    p = tmp_path / "_bundle.pkl"
    joblib.dump({"model": "dummy", "metrics": {"f1": 0.5}}, p)
    return p.read_bytes()


@pytest.fixture
def client_and_dir(
    tmp_path, monkeypatch, sample_config, mock_db, mock_broker, mock_llm,
    mock_market_data, mock_notify,
):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    # _model_dir() reads ctx.config.strategy.model_dir (a non-field attr that
    # defaults to ./models); patch the route's reference to a tmp dir.
    monkeypatch.setattr(
        "yolovest.dashboard.routes.models._model_dir", lambda ctx: str(models_dir),
    )
    ctx = AppContext(
        config=sample_config, db=mock_db, broker=mock_broker, llm=mock_llm,
        market_data=mock_market_data, notify=mock_notify,
        market_hours=MarketHoursChecker(sample_config), event_bus=EventBus(),
    )
    return TestClient(create_app(ctx)), models_dir


def _upload(client, content: bytes, name="m.pkl"):
    return client.post(
        "/api/ml-models/upload",
        files={"file": (name, io.BytesIO(content), "application/octet-stream")},
        auth=AUTH,
    )


class TestUploadSigning:
    def test_signed_upload_accepted(self, client_and_dir, tmp_path, monkeypatch):
        client, _ = client_and_dir
        monkeypatch.setenv("MODEL_SIGNING_KEY", KEY)
        envelope = wrap(KEY.encode(), _bundle_bytes(tmp_path))

        r = _upload(client, envelope)

        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_unsigned_upload_rejected_when_key_set(
        self, client_and_dir, tmp_path, monkeypatch,
    ):
        client, models_dir = client_and_dir
        monkeypatch.setenv("MODEL_SIGNING_KEY", KEY)

        # A perfectly valid bundle, but unsigned — must be refused before load.
        r = _upload(client, _bundle_bytes(tmp_path))

        assert r.status_code == 400
        assert "signature" in r.json()["detail"].lower()
        # Nothing was written to disk (rejected before the write/joblib.load).
        assert list(models_dir.glob("*.pkl")) == []

    def test_forged_signature_rejected(self, client_and_dir, tmp_path, monkeypatch):
        client, _ = client_and_dir
        monkeypatch.setenv("MODEL_SIGNING_KEY", KEY)
        # Signed with a different key than the server holds.
        envelope = wrap(b"attacker-key", _bundle_bytes(tmp_path))

        r = _upload(client, envelope)

        assert r.status_code == 400

    def test_unsigned_accepted_when_no_key(self, client_and_dir, tmp_path, monkeypatch):
        client, _ = client_and_dir
        monkeypatch.delenv("MODEL_SIGNING_KEY", raising=False)

        # Backward-compatible: without a key, raw .pkl uploads still work.
        r = _upload(client, _bundle_bytes(tmp_path))

        assert r.status_code == 200
