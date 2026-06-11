"""Startup model loading must honor the model registry (Milestone 0.4).

`main._load_ml_models_background` fills the production inference slots.
The registry's `status='production'` row — set by promotion (shadow gate
or manual) — is binding: every retrain saves its candidate as a NEW
artifact with `status='shadow'`, so "load the newest file on disk" would
put an un-vetted candidate on the live account at every restart. These
tests pin the contract:

  1. Registry production row wins over a newer artifact on disk.
  2. No production row (fresh install) → latest artifact as bootstrap.
  3. Production row whose .pkl is gone → fall back to latest artifact
     (keep trading) instead of leaving the slot empty.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import joblib

from yolovest.main import _load_ml_models_background
from yolovest.strategy.ml_signal import XGBoostSignalModel


class _StubModel:
    """Minimal picklable stand-in for a trained model."""

    def predict(self, X):  # noqa: N803
        return [1]


def _write_artifact(model_dir: Path, version: str) -> None:
    joblib.dump(
        {"model": _StubModel(), "version": version, "feature_names": ["f0"]},
        model_dir / f"{version}.pkl",
    )


def _ctx(ml: XGBoostSignalModel, db: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(ml=ml, db=db)


async def test_startup_loads_registry_production_not_latest_artifact(tmp_path):
    # Older artifact = the registry's production model. Newer artifact =
    # the last retrain's candidate (shadow/retired) still on disk.
    _write_artifact(tmp_path, "swing_v20250101_120000")
    _write_artifact(tmp_path, "swing_v20990101_120000")
    _write_artifact(tmp_path, "intraday_v20250101_120000")
    _write_artifact(tmp_path, "intraday_v20990101_120000")

    ml = XGBoostSignalModel(model_dir=str(tmp_path))
    db = AsyncMock()

    async def _prod(model_type: str):
        return {
            "model_type": model_type,
            "version": f"{model_type}_v20250101_120000",
            "status": "production",
        }

    db.get_production_model = AsyncMock(side_effect=_prod)
    db.get_all_shadow_models = AsyncMock(return_value=[])

    await _load_ml_models_background(_ctx(ml, db))

    assert ml._swing_version == "swing_v20250101_120000"
    assert ml._intraday_version == "intraday_v20250101_120000"


async def test_startup_falls_back_to_latest_when_registry_empty(tmp_path):
    # Fresh install / pre-first-promotion: no production row → bootstrap
    # from the latest artifact so the system still has a model.
    _write_artifact(tmp_path, "swing_v20250101_120000")
    _write_artifact(tmp_path, "swing_v20260101_120000")

    ml = XGBoostSignalModel(model_dir=str(tmp_path))
    db = AsyncMock()
    db.get_production_model = AsyncMock(return_value=None)
    db.get_all_shadow_models = AsyncMock(return_value=[])

    await _load_ml_models_background(_ctx(ml, db))

    assert ml._swing_version == "swing_v20260101_120000"


async def test_startup_falls_back_when_production_artifact_missing(tmp_path):
    # Registry names a production version whose .pkl was deleted. Keep
    # trading: fall back to the latest artifact rather than no model.
    _write_artifact(tmp_path, "swing_v20260101_120000")

    ml = XGBoostSignalModel(model_dir=str(tmp_path))
    db = AsyncMock()
    db.get_production_model = AsyncMock(
        return_value={"version": "swing_v20000101_000000"},
    )
    db.get_all_shadow_models = AsyncMock(return_value=[])

    await _load_ml_models_background(_ctx(ml, db))

    assert ml._swing_version == "swing_v20260101_120000"


async def test_startup_registry_lookup_failure_falls_back_to_latest(tmp_path):
    # A DB error during the registry lookup must not strand the slots
    # empty — degrade to the latest artifact and keep going.
    _write_artifact(tmp_path, "swing_v20260101_120000")

    ml = XGBoostSignalModel(model_dir=str(tmp_path))
    db = AsyncMock()
    db.get_production_model = AsyncMock(side_effect=RuntimeError("db down"))
    db.get_all_shadow_models = AsyncMock(return_value=[])

    await _load_ml_models_background(_ctx(ml, db))

    assert ml._swing_version == "swing_v20260101_120000"
