"""ML model registry: promote/retire/shadow, artifact download/upload/import.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response

from yolovest.dashboard.helpers import _model_dir, _safe_path_in
from yolovest.strategy.model_signing import signing_key, unwrap, wrap

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials
    verify_download_credentials = deps.verify_download_credentials

    def _lib_version_safe(dist: str) -> str:
        try:
            from importlib.metadata import version
            return version(dist)
        except Exception:
            return "unknown"

    def _production_feature_names(model_type: str) -> list[str]:
        """Feature names of the model currently loaded for this type, used
        as a soft reference for the import feature-drift warning. Reads the
        provider's restored feature list; empty when nothing is loaded."""
        if not ctx.ml:
            return []
        attr = "_intraday_features" if model_type == "intraday" else "_swing_features"
        return list(getattr(ctx.ml, attr, None) or [])

    # ------------------------------------------------------------------
    # ML Models & Performance
    # ------------------------------------------------------------------

    @app.get("/api/ml-models")
    async def get_ml_models(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """ML model information: production, shadow, and retired models."""
        result: dict[str, Any] = {"production": {}, "shadow": [], "retired": []}
        for model_type in ["intraday", "swing"]:
            try:
                model = await ctx.db.get_production_model(model_type)
                if model:
                    result["production"][model_type] = model
            except Exception:
                logger.debug("Failed to get production model for %s", model_type, exc_info=True)
        try:
            shadow_models = await ctx.db.get_all_shadow_models()
            result["shadow"] = shadow_models
        except Exception:
            logger.debug("Failed to get shadow models", exc_info=True)
        try:
            retired_models = await ctx.db.get_retired_models()
            result["retired"] = retired_models
        except Exception:
            logger.debug("Failed to get retired models", exc_info=True)
        return result

    @app.post("/api/ml-models/{model_type}/{version}/promote")
    async def promote_model(
        model_type: str,
        version: str,
        force: bool = False,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Manually promote a shadow model to production.

        Honest-edge gate: a model whose untuned (argmax) Sharpe is below
        the configured floor is blocked unless `force=true`. argmax_sharpe
        isn't persisted on the registry row, so it's read from the
        artifact; if the artifact can't be read the gate is skipped
        rather than blocking on an infra error.
        """
        if "/" in version or "\\" in version or ".." in version:
            raise HTTPException(status_code=400, detail="Invalid version")
        if not force:
            pkl_path = _safe_path_in(_model_dir(ctx), f"{version}.pkl")
            if pkl_path.exists():
                try:
                    import joblib

                    from yolovest.skills.model_retrain import passes_edge_gate
                    artifact = await asyncio.to_thread(joblib.load, str(pkl_path))
                    metrics = (artifact or {}).get("metrics") or {}
                    edge_ok, edge_reason = passes_edge_gate(
                        metrics,
                        ctx.config.retraining.min_argmax_sharpe_for_promotion,
                    )
                    if not edge_ok:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Refusing to promote {model_type} {version}: "
                                f"{edge_reason}. Pass force=true to override."
                            ),
                        )
                except HTTPException:
                    raise
                except Exception:
                    logger.warning(
                        "Edge gate could not read %s; promoting without it",
                        pkl_path, exc_info=True,
                    )
        await ctx.db.promote_model(model_type, version)
        if ctx.ml:
            try:
                await ctx.ml.load_model(model_type, version)
            except Exception as e:
                logger.warning("Failed to load promoted model %s/%s: %s", model_type, version, e)
        return {"promoted": True, "model_type": model_type, "version": version}

    @app.get("/api/ml-models/{version}/download")
    async def download_model(
        version: str, _user: str = Depends(verify_download_credentials),
    ) -> Response:
        """Stream a trained model artifact (.pkl) to the browser so it
        can be moved to another machine (e.g. import a model trained on
        a higher-memory box).

        When MODEL_SIGNING_KEY is set, the artifact is wrapped in a signed
        HMAC envelope so the import side can verify authenticity before
        joblib.load (see strategy/model_signing). Without a key it streams the
        raw .pkl as before.
        """
        model_dir = _model_dir(ctx)
        if "/" in version or "\\" in version or ".." in version:
            raise HTTPException(status_code=400, detail="Invalid version")
        path = _safe_path_in(model_dir, f"{version}.pkl")
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"{version}.pkl not found")
        key = signing_key()
        if key is None:
            return FileResponse(
                str(path), media_type="application/octet-stream",
                filename=f"{version}.pkl",
            )
        data = await asyncio.to_thread(path.read_bytes)
        envelope = wrap(key, data)
        return Response(
            content=envelope,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{version}.pkl"'},
        )

    @app.post("/api/ml-models/upload")
    async def upload_model(
        file: UploadFile = File(...),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Accept a trained .pkl uploaded from another machine and place
        it in the models dir. Call POST /api/ml-models/import afterwards
        to register + (optionally) promote + hot-reload it."""
        import os

        model_dir = _model_dir(ctx)
        os.makedirs(model_dir, exist_ok=True)
        name = os.path.basename(file.filename or "")
        if not name.endswith(".pkl"):
            raise HTTPException(status_code=400, detail="Expected a .pkl file")
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status_code=400, detail="Invalid filename")
        # Buffer the upload so its signature can be verified BEFORE the bytes
        # are written to disk and joblib.load()ed — pickle executes arbitrary
        # code on load, so an unsigned/forged artifact must never reach joblib.
        raw = await file.read()

        key = signing_key()
        if key is not None:
            try:
                payload = unwrap(key, raw)
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Model signature check failed: {e}. Upload an artifact "
                        "downloaded from a YoloVest instance that shares this "
                        "MODEL_SIGNING_KEY."
                    ),
                ) from e
        else:
            payload = raw
            logger.warning(
                "MODEL_SIGNING_KEY not set — accepting UNVERIFIED model upload "
                "%s. Set MODEL_SIGNING_KEY on both machines to require signed "
                "artifacts (guards against malicious-pickle RCE).", name,
            )

        dest = _safe_path_in(model_dir, name)
        dest.write_bytes(payload)
        # Sanity-check it loads as a YoloVest model bundle before
        # reporting success — a bad file shouldn't sit around looking
        # importable.
        try:
            import joblib
            artifact = await asyncio.to_thread(joblib.load, str(dest))
            if not isinstance(artifact, dict) or "model" not in artifact:
                raise ValueError("not a YoloVest model bundle")
        except Exception as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400, detail=f"Invalid model artifact: {e}",
            ) from e
        version = name[:-4]  # strip .pkl
        logger.info("Uploaded model artifact %s (%d bytes)", name, len(payload))
        return {
            "success": True, "version": version, "filename": name,
            "size_bytes": len(payload),
            "metrics": artifact.get("metrics", {}),
        }

    @app.post("/api/ml-models/import")
    async def import_model(
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Register a model artifact trained on another machine.

        Workflow: run the full app in a container on a higher-memory box,
        retrain via the model-retrain skill, download the produced
        <version>.pkl, then upload it here (POST /api/ml-models/upload)
        and POST here to register + (optionally) promote + hot-reload.

        Body: {"model_type": "intraday"|"swing", "version": "<stem>",
               "promote": bool}
        `version` is the .pkl filename without extension (the artifact's
        own version string). Metrics are read straight from the
        artifact so the registry row matches what was trained.
        """
        body = await request.json()
        model_type = body.get("model_type")
        version = body.get("version")
        promote = bool(body.get("promote", False))
        force = bool(body.get("force", False))
        if model_type not in ("intraday", "swing") or not version:
            raise HTTPException(
                status_code=400,
                detail="model_type must be 'intraday' or 'swing' and version is required",
            )

        model_dir = _model_dir(ctx)
        pkl_path = _safe_path_in(model_dir, f"{version}.pkl")
        if not pkl_path.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{version}.pkl not found in {model_dir}. Copy the "
                    "trained artifact there first."
                ),
            )

        # Read metrics + sanity-check the artifact loads + matches type.
        try:
            import joblib
            artifact = await asyncio.to_thread(joblib.load, str(pkl_path))
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Failed to load artifact: {e}",
            ) from e
        if not isinstance(artifact, dict) or "model" not in artifact:
            raise HTTPException(
                status_code=400,
                detail="Artifact is not a valid YoloVest model bundle.",
            )
        metrics = artifact.get("metrics") or {}

        # Compatibility gate — a model trained against a different feature
        # schema would be silently fed wrong inputs at inference (missing
        # features resolve to 0.0, no crash). Hard-block on schema mismatch
        # unless the caller explicitly forces. Library-version drift and a
        # feature-name diff vs the current production model are surfaced as
        # soft warnings (won't block).
        from yolovest.data.features import MODEL_SCHEMA_VERSION

        warnings: list[str] = []
        artifact_schema = artifact.get("schema_version")
        if artifact_schema != MODEL_SCHEMA_VERSION and not force:
            if artifact_schema is None:
                detail = (
                    "Artifact has no schema_version (trained before schema "
                    f"versioning; current is {MODEL_SCHEMA_VERSION}). It may "
                    "feed the model stale features. Re-train on current code, "
                    "or pass force=true to import anyway."
                )
            else:
                detail = (
                    f"Schema mismatch: artifact is schema_version "
                    f"{artifact_schema}, this code expects "
                    f"{MODEL_SCHEMA_VERSION}. The feature set or label "
                    "geometry changed since this model was trained. Re-train "
                    "on current code, or pass force=true to import anyway."
                )
            raise HTTPException(status_code=422, detail=detail)
        if artifact_schema != MODEL_SCHEMA_VERSION:
            warnings.append(
                f"Forced import despite schema mismatch (artifact "
                f"{artifact_schema} vs code {MODEL_SCHEMA_VERSION})."
            )

        # Soft: library-version drift (unpickled estimators can misbehave
        # across major XGBoost / scikit-learn versions).
        for lib, key in (("xgboost", "xgboost_version"),
                         ("scikit-learn", "sklearn_version")):
            stamped = artifact.get(key)
            current = _lib_version_safe(lib)
            if stamped and current != "unknown" and stamped != current:
                warnings.append(
                    f"{lib} version differs (artifact {stamped} vs runtime "
                    f"{current}); verify predictions look sane."
                )

        # Soft: feature-name drift vs the model currently in production for
        # this type — a cheap automatic guard against a forgotten schema
        # bump (compares to the last validated model rather than to code).
        prod_features = _production_feature_names(model_type)
        new_features = artifact.get("feature_names") or []
        if prod_features and new_features:
            added = sorted(set(new_features) - set(prod_features))
            removed = sorted(set(prod_features) - set(new_features))
            if added or removed:
                warnings.append(
                    "Feature set differs from current production model"
                    + (f"; added {added}" if added else "")
                    + (f"; removed {removed}" if removed else "")
                    + "."
                )

        # Honest-edge gate on promotion. An imported model trained
        # elsewhere bypasses the retrain skill's gates entirely, so a
        # net-losing model (negative argmax Sharpe whose backtest profit
        # is a threshold-selected artifact) could otherwise be promoted
        # straight to live — exactly how a −7 argmax model reached
        # production before. Registering as shadow is always allowed
        # (shadow only observes); promotion requires clearing the floor
        # unless the caller explicitly forces.
        if promote:
            from yolovest.skills.model_retrain import passes_edge_gate

            edge_ok, edge_reason = passes_edge_gate(
                metrics,
                ctx.config.retraining.min_argmax_sharpe_for_promotion,
            )
            if not edge_ok and not force:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Refusing to promote {model_type} {version}: "
                        f"{edge_reason}. Import as shadow (promote=false) to "
                        f"observe it, or pass force=true to promote anyway."
                    ),
                )
            if not edge_ok:
                warnings.append(f"Forced promotion despite edge gate: {edge_reason}.")

        # Register as shadow first (mirrors the retrain path), then
        # optionally promote. Hot-reload the running provider so the
        # change takes effect without a server restart.
        await ctx.db.save_model_version(
            model_type, version, f"models/{version}.pkl", metrics,
        )
        loaded = False
        if ctx.ml:
            try:
                if promote:
                    await ctx.db.promote_model(model_type, version)
                    await ctx.ml.load_model(model_type, version)
                else:
                    await ctx.ml.load_shadow_model(model_type, version)
                loaded = True
            except Exception as e:
                logger.warning(
                    "Imported model %s/%s registered but hot-reload failed: %s",
                    model_type, version, e,
                )
        return {
            "imported": True,
            "model_type": model_type,
            "version": version,
            "promoted": promote,
            "hot_reloaded": loaded,
            "metrics": metrics,
            "warnings": warnings,
        }

    @app.post("/api/ml-models/{model_type}/{version}/reshadow")
    async def reshadow_model(
        model_type: str,
        version: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Move a retired model back to shadow for re-evaluation."""
        # Check if .pkl file exists before changing status
        model_dir = _model_dir(ctx)
        pkl_path = _safe_path_in(model_dir, f"{version}.pkl")
        if not pkl_path.exists():
            return {
                "reshadowed": False,
                "error": f"Model file {version}.pkl not found — it was already deleted. Cannot re-shadow.",
            }

        ok = await ctx.db.reshadow_model(model_type, version)
        if ok and ctx.ml:
            try:
                await ctx.ml.load_shadow_model(model_type, version)
            except Exception as e:
                # Revert to retired if load fails
                await ctx.db.retire_model(model_type, version)
                logger.warning("Failed to load re-shadowed model %s/%s: %s", model_type, version, e)
                return {
                    "reshadowed": False,
                    "error": "Model file exists but failed to load (see server logs)",
                }
        return {"reshadowed": ok, "model_type": model_type, "version": version}

    @app.post("/api/ml-models/{model_type}/{version}/retire")
    async def retire_model_endpoint(
        model_type: str,
        version: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Retire a shadow model (stop A/B testing, move to retired)."""
        await ctx.db.retire_model(model_type, version)
        if ctx.ml:
            ctx.ml.clear_shadow(model_type)
        logger.info("Retired %s model %s", model_type, version)
        return {"retired": True, "model_type": model_type, "version": version}

    @app.get("/api/ml-models/{model_type}/shadow-comparison")
    async def get_shadow_comparison(
        model_type: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Head-to-head shadow vs production prediction metrics."""
        shadow_models = await ctx.db.get_all_shadow_models()
        shadow = next((s for s in shadow_models if s["model_type"] == model_type), None)
        if not shadow:
            return {"shadow": {}, "production": {}}
        return await ctx.db.get_shadow_vs_production_metrics(
            model_type, since_date=shadow.get("shadow_start_date", "2000-01-01"),
        )

    @app.delete("/api/ml-models/{model_type}/{version}")
    async def delete_model(
        model_type: str, version: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a model version (DB record + .pkl artifact)."""
        result = await ctx.db.delete_model_version(
            model_type, version, model_dir=_model_dir(ctx),
        )
        return {"success": True, **result}
