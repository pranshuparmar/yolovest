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


class TestLibSkewWarning:
    """Artifacts trained under a different sklearn/xgboost than the
    running image must warn loudly at load time (fail-open) — a shifted
    calibrator silently changes every probability the signal gates read."""

    def test_mismatch_warns(self, caplog):
        import logging

        from yolovest.strategy.ml_signal import _lib_version, _warn_on_lib_skew

        artifact = {
            "version": "swing_v20990101_000000",
            "sklearn_version": "0.0.1-not-real",
            "xgboost_version": _lib_version("xgboost"),  # matches runtime
        }
        with caplog.at_level(logging.WARNING, logger="yolovest.strategy.ml_signal"):
            _warn_on_lib_skew(artifact, "load_model[swing]")
        msgs = [r.getMessage() for r in caplog.records]
        assert any("scikit-learn 0.0.1-not-real" in m and "model-retrain" in m for m in msgs)
        # The matching xgboost stamp must NOT warn.
        assert not any("xgboost" in m and "trained under xgboost" in m for m in msgs)

    def test_matching_versions_silent(self, caplog):
        import logging

        from yolovest.strategy.ml_signal import _lib_version, _warn_on_lib_skew

        artifact = {
            "version": "v1",
            "sklearn_version": _lib_version("scikit-learn"),
            "xgboost_version": _lib_version("xgboost"),
        }
        with caplog.at_level(logging.WARNING, logger="yolovest.strategy.ml_signal"):
            _warn_on_lib_skew(artifact, "load_model[swing]")
        assert not caplog.records

    def test_pre_stamp_artifacts_skipped(self, caplog):
        import logging

        from yolovest.strategy.ml_signal import _warn_on_lib_skew

        with caplog.at_level(logging.WARNING, logger="yolovest.strategy.ml_signal"):
            _warn_on_lib_skew({"version": "old"}, "load_model[swing]")
            _warn_on_lib_skew({"sklearn_version": "unknown"}, "load_model[swing]")
        assert not caplog.records


class TestArtifactIntegrity:
    """save_model writes atomically (tmp + rename) and records a sha256
    sidecar; load refuses a corrupted artifact instead of deserializing
    garbage into the live inference slots. Sidecar-less artifacts
    (legacy saves, dashboard uploads) still load."""

    async def _save_one(self, tmp_path) -> tuple[XGBoostSignalModel, str]:
        model = XGBoostSignalModel(model_dir=str(tmp_path))
        model._set_model("swing", {"dummy": True})
        model._swing_features = ["f0"]
        version = await model.save_model("swing", {"sharpe_ratio": 1.0})
        return model, version

    async def test_save_writes_sidecar_and_no_tmp_left(self, tmp_path):
        _, version = await self._save_one(tmp_path)
        assert (tmp_path / f"{version}.pkl").exists()
        assert (tmp_path / f"{version}.pkl.sha256").exists()
        assert not list(tmp_path.glob("*.tmp"))

    async def test_roundtrip_with_checksum_ok(self, tmp_path):
        _, version = await self._save_one(tmp_path)
        fresh = XGBoostSignalModel(model_dir=str(tmp_path))
        await fresh.load_model("swing", version)
        assert fresh._swing_version == version

    async def test_corrupted_artifact_refused(self, tmp_path):
        import pytest

        _, version = await self._save_one(tmp_path)
        pkl = tmp_path / f"{version}.pkl"
        pkl.write_bytes(pkl.read_bytes()[:-7] + b"GARBAGE")
        fresh = XGBoostSignalModel(model_dir=str(tmp_path))
        with pytest.raises(ValueError, match="integrity check"):
            await fresh.load_model("swing", version)

    async def test_sidecarless_legacy_artifact_loads(self, tmp_path):
        _, version = await self._save_one(tmp_path)
        (tmp_path / f"{version}.pkl.sha256").unlink()
        fresh = XGBoostSignalModel(model_dir=str(tmp_path))
        await fresh.load_model("swing", version)
        assert fresh._swing_version == version
