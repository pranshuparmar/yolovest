"""save_model must stamp compatibility metadata into the artifact so the
cross-machine import gate (POST /api/ml-models/import) can fail loudly on a
model trained against incompatible code."""
import joblib

from yolovest.data.features import MODEL_SCHEMA_VERSION
from yolovest.strategy.ml_signal import XGBoostSignalModel


async def test_save_model_stamps_compat_metadata(tmp_path):
    model = XGBoostSignalModel(model_dir=str(tmp_path))
    # A trained estimator isn't needed for the persistence path — any
    # picklable object satisfies save_model's "model present" precondition.
    model._set_model("swing", {"dummy": True})
    model._swing_features = ["rsi_14", "macd_histogram_pct"]

    version = await model.save_model("swing", {"sharpe_ratio": 1.2})

    artifact = joblib.load(tmp_path / f"{version}.pkl")
    assert artifact["schema_version"] == MODEL_SCHEMA_VERSION
    assert "xgboost_version" in artifact
    assert "sklearn_version" in artifact
    assert artifact["feature_names"] == ["rsi_14", "macd_histogram_pct"]
