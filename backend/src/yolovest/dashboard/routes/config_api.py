"""Runtime config: read, export, import, defaults, update.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import json
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
from fastapi.responses import JSONResponse

from yolovest.context import MarketHoursChecker
from yolovest.dashboard.security import DEFAULT_DASHBOARD_PASSWORD

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials
    verify_download_credentials = deps.verify_download_credentials
    _password = deps.password


    # ------------------------------------------------------------------
    # Config (UI-editable settings)
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Return all DB-editable config values grouped by section."""
        from yolovest.config import config_to_ui_sections
        sections = config_to_ui_sections(ctx.config)
        return {"sections": sections}

    @app.get("/api/config/export")
    async def export_config(
        _user: str = Depends(verify_download_credentials),
    ) -> JSONResponse:
        """Download all DB-editable config as a flat {key: value} JSON
        file. Import on another instance by uploading it (the Settings
        importer PUTs these through the same validation as manual
        edits). Excludes file-only keys (secrets, paths)."""
        from yolovest.config import FILE_ONLY_KEYS, _flatten_model
        from yolovest.timezone import now_ist as _now_ist
        flat = {
            k: v for k, v in _flatten_model(ctx.config).items()
            if k not in FILE_ONLY_KEYS
        }
        ts = _now_ist().strftime("%Y%m%d_%H%M%S")
        return JSONResponse(
            content={"config": flat},
            headers={
                "Content-Disposition": f'attachment; filename="yolovest_config_{ts}.json"',
            },
        )

    @app.post("/api/config/import")
    async def import_config(
        file: UploadFile = File(...),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Apply a config JSON exported from another instance. Runs
        every key through the same Pydantic validation + apply path as
        manual Settings edits. File-only keys in the upload are ignored."""
        from yolovest.config import (
            FILE_ONLY_KEYS,
            apply_db_config,
            config_to_ui_sections,
        )

        raw = await file.read()
        try:
            parsed = json.loads(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e
        flat = parsed.get("config") if isinstance(parsed, dict) else None
        if not isinstance(flat, dict):
            raise HTTPException(
                status_code=400,
                detail='Expected {"config": {key: value, ...}}',
            )
        updates = {
            k: v for k, v in flat.items() if k not in FILE_ONLY_KEYS
        }
        if not updates:
            raise HTTPException(status_code=400, detail="No importable keys")
        str_updates = {
            k: (json.dumps(v) if isinstance(v, (list, dict, bool)) or v is None else str(v))
            for k, v in updates.items()
        }
        # Validate the merged config BEFORE persisting (same order as
        # PUT /api/config) so a bad value never lands in the DB.
        db_values = await ctx.db.get_all_config()
        db_values.update(str_updates)
        try:
            new_config = apply_db_config(ctx.config, db_values)
        except Exception as e:
            raise HTTPException(
                status_code=422, detail=f"Config validation failed: {e}",
            ) from e
        await ctx.db.set_config_bulk(str_updates)
        ctx.config = new_config
        ctx.market_hours = MarketHoursChecker(ctx.config)
        if hasattr(ctx.notify, "_config"):
            ctx.notify._config = ctx.config
        return {
            "success": True,
            "imported": len(updates),
            "sections": config_to_ui_sections(ctx.config),
        }

    @app.get("/api/config/defaults")
    async def get_config_defaults(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Return the default values for every DB-editable config key,
        in the same {section: {key: value}} shape as /api/config so the
        frontend can diff current vs default and offer a per-tab reset.

        `field_kinds` maps every numeric key to "int"/"float" from the
        Pydantic annotations — JSON erases the distinction (1.0 -> 1),
        so a value-based frontend heuristic misclassifies whole-valued
        float fields and rejects valid decimals."""
        from yolovest.config import (
            AppConfig,
            config_field_kinds,
            config_to_ui_sections,
        )
        defaults = config_to_ui_sections(AppConfig())
        return {"sections": defaults, "field_kinds": config_field_kinds()}

    @app.put("/api/config")
    async def update_config(
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Update config values. Body: {"updates": {"risk.max_open_positions": 5, ...}}

        Validates all changes through Pydantic before persisting.
        Returns the updated config sections.
        """
        from yolovest.config import (
            FILE_ONLY_KEYS,
            apply_db_config,
            config_to_ui_sections,
        )

        body = await request.json()
        updates: dict[str, Any] = body.get("updates", {})
        if not updates:
            raise HTTPException(status_code=400, detail="No updates provided")

        # Reject file-only keys
        rejected = [k for k in updates if k in FILE_ONLY_KEYS]
        if rejected:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot modify file-only keys via UI: {rejected}",
            )

        # Convert all values to strings for DB storage
        import json as _json

        str_updates: dict[str, str] = {}
        for k, v in updates.items():
            if isinstance(v, (bool, list, dict)) or v is None:
                str_updates[k] = _json.dumps(v)
            else:
                str_updates[k] = str(v)

        # Load current DB config, overlay updates, validate via Pydantic
        db_values = await ctx.db.get_all_config()
        db_values.update(str_updates)
        try:
            new_config = apply_db_config(ctx.config, db_values)
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Validation failed: {e}",
            ) from e

        # Refuse to arm live trading while the dashboard password is still the
        # shipped default — that password is the only gate on real-money
        # execution, so flipping to live behind it would expose the account to
        # anyone who reaches the dashboard. Change the password first.
        if (
            "mode" in updates
            and str(new_config.mode).lower() == "live"
            and _password.get("current") == DEFAULT_DASHBOARD_PASSWORD
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Refusing to switch to live trading while the dashboard "
                    "password is the default. Change the password first "
                    "(Settings → Change Password)."
                ),
            )

        # Capture old values BEFORE persisting so the diff log shows
        # what each key actually changed from. `db_values` was loaded
        # above before the overlay was applied, so it still holds the
        # pre-update state.
        old_values: dict[str, str | None] = {}
        for k in updates:
            # Note: read from the freshly-loaded snapshot (it was
            # mutated by the overlay; use ctx.config flattened instead
            # to be robust).
            try:
                v = await ctx.db.get_config(k)
            except Exception:
                v = None
            old_values[k] = v

        # Persist to DB
        await ctx.db.set_config_bulk(str_updates)

        # Hot-apply to running config
        old_config = ctx.config
        ctx.config = new_config
        ctx.market_hours = MarketHoursChecker(ctx.config)
        # Sync Notifier's config reference
        if hasattr(ctx.notify, "_config"):
            ctx.notify._config = ctx.config

        # Side effects for specific keys
        if any(k.startswith("log.") for k in updates):
            try:
                from yolovest.main import setup_logging
                setup_logging(ctx.config)
                logger.info("Log levels reloaded: console=%s, file=%s",
                            ctx.config.log.level, ctx.config.log.file_level)
            except Exception as e:
                logger.warning("Failed to reload log levels: %s", e)

        if "mode" in updates:
            logger.info("Trading mode changed: %s -> %s", old_config.mode, new_config.mode)
            # Sync to broker — it stores its own _mode for order routing
            if hasattr(ctx.broker, "_mode"):
                ctx.broker._mode = new_config.mode
                logger.info("Broker mode synced to: %s", new_config.mode)

        # Emit per-key diff so the audit trail records what each key
        # actually changed from -> to (instead of just the key list).
        for k, new_v in str_updates.items():
            old_v = old_values.get(k)
            if old_v == new_v:
                continue
            logger.info(
                "Config updated via UI: %s: %r -> %r",
                k,
                old_v if old_v is not None else "<unset>",
                new_v,
            )

        sections = config_to_ui_sections(ctx.config)
        return {"status": "ok", "updated": list(updates.keys()), "sections": sections}
