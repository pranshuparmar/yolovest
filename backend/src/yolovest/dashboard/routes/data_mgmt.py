"""Storage stats, cleanup, backups, bulk delete, reset.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse

from yolovest.dashboard.helpers import _model_dir, _safe_path_in

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials
    verify_download_credentials = deps.verify_download_credentials

    # ------------------------------------------------------------------
    # Data Management: Storage Stats & Cleanup
    # ------------------------------------------------------------------

    @app.get("/api/storage-stats")
    async def storage_stats(_user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Get row counts, date ranges, and DB file size for all tables."""
        return await ctx.db.get_storage_stats()

    @app.post("/api/cleanup")
    async def cleanup_data(
        table: str = Query(..., description="Table to clean up"),
        older_than_days: int = Query(..., ge=1, description="Delete rows older than N days"),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete old data from a specific table to free up space."""
        try:
            deleted = await ctx.db.cleanup_table(table, older_than_days)
            # VACUUM to reclaim disk space after large deletes
            if deleted > 100:
                await ctx.db.conn.execute("VACUUM")
            ctx.db.invalidate_storage_stats_cache()
            return {"success": True, "table": table, "rows_deleted": deleted}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/backup")
    async def create_backup(_user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Create a manual database backup including ML model artifacts."""
        backup_dir = ctx.config.database.backup_dir
        backup_path = await ctx.db.backup(backup_dir, model_dir=_model_dir(ctx))
        return {"success": True, "backup_path": backup_path}

    @app.get("/api/backups")
    async def list_backups(_user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """List available database backups."""
        backup_dir = ctx.config.database.backup_dir
        return await ctx.db.list_backups(backup_dir)

    @app.post("/api/restore/{filename}")
    async def restore_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Restore a database backup. Application should be restarted after restore."""
        backup_dir = ctx.config.database.backup_dir
        # Defense-in-depth: reject traversal / absolute paths before handing
        # the name to the DB layer (which copies it over the live DB). The
        # {filename} path convertor already blocks slashes, but this matches
        # the guard every sibling backup endpoint already applies.
        _safe_in_dir(backup_dir, filename)
        result = await ctx.db.restore_backup(
            backup_dir, filename, model_dir=_model_dir(ctx),
        )
        ctx.db.invalidate_storage_stats_cache()
        return {"success": True, **result}

    @app.delete("/api/backups/{filename}")
    async def delete_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a single backup file. Returns the freed size in bytes."""
        backup_dir = ctx.config.database.backup_dir
        try:
            result = await ctx.db.delete_backup(backup_dir, filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except PermissionError as e:
            # Locked backup — refuse with 409 Conflict so the UI can
            # prompt the user to unlock first.
            raise HTTPException(status_code=409, detail=str(e)) from e
        logger.info("Deleted backup %s (%d bytes)", filename, result["size_bytes"])
        return {"success": True, **result}

    def _safe_in_dir(base_dir: str, filename: str) -> Path:
        """Resolve `filename` strictly inside `base_dir`. Rejects path
        traversal (../, absolute paths, separators). Raises HTTP 400."""
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise HTTPException(status_code=400, detail="Invalid filename")
        # Containment via realpath + commonpath (CodeQL-recognised barrier).
        return _safe_path_in(base_dir, filename)

    @app.get("/api/backups/{filename}/download")
    async def download_backup(
        filename: str, _user: str = Depends(verify_download_credentials),
    ) -> FileResponse:
        """Stream a backup .db file to the browser. Used to move a full
        DB (data + settings) to another machine for offline retraining."""
        backup_dir = ctx.config.database.backup_dir
        path = _safe_in_dir(backup_dir, filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Backup not found: {filename}")
        return FileResponse(
            str(path), media_type="application/octet-stream", filename=filename,
        )

    @app.post("/api/backups/upload")
    async def upload_backup(
        file: UploadFile = File(...),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Accept a backup .db uploaded from another machine, validate
        it's a SQLite file, and place it in the backup dir so it can be
        restored. Pairs with download_backup for cross-machine moves."""
        import os

        backup_dir = ctx.config.database.backup_dir
        os.makedirs(backup_dir, exist_ok=True)
        name = os.path.basename(file.filename or "")
        if not name.endswith(".db"):
            raise HTTPException(status_code=400, detail="Expected a .db file")
        dest = _safe_in_dir(backup_dir, name)
        # Validate SQLite magic header on the first chunk before
        # committing the whole upload to disk.
        first = await file.read(16)
        if not first.startswith(b"SQLite format 3\x00"):
            raise HTTPException(
                status_code=400, detail="Not a valid SQLite database file",
            )
        size = len(first)
        with open(dest, "wb") as out:
            out.write(first)
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                size += len(chunk)
        ctx.db.invalidate_storage_stats_cache()
        logger.info("Uploaded backup %s (%d bytes)", name, size)
        return {"success": True, "filename": name, "size_bytes": size}

    @app.post("/api/backups/{filename}/lock")
    async def lock_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Mark a backup as locked so the daily prune + manual delete
        paths skip it. Idempotent.
        """
        backup_dir = ctx.config.database.backup_dir
        try:
            result = await ctx.db.set_backup_lock(backup_dir, filename, locked=True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        logger.info("Locked backup %s", filename)
        return {"success": True, **result}

    @app.post("/api/backups/{filename}/unlock")
    async def unlock_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Clear the lock sentinel on a backup so it's eligible for
        prune / delete again. Idempotent.
        """
        backup_dir = ctx.config.database.backup_dir
        try:
            result = await ctx.db.set_backup_lock(backup_dir, filename, locked=False)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        logger.info("Unlocked backup %s", filename)
        return {"success": True, **result}

    @app.post("/api/bulk-delete/{group}")
    async def bulk_delete(
        group: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a group of related data: paper, live, dry_runs, predictions, signals."""
        try:
            deleted = await ctx.db.bulk_delete(group)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        ctx.db.invalidate_storage_stats_cache()
        total = sum(deleted.values())
        logger.info("Bulk delete [%s]: %d rows", group, total)
        return {"success": True, "group": group, "deleted": deleted, "total": total}

    @app.post("/api/reset")
    async def reset_all_data(_user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Delete ALL data from all tables and model artifacts. Schema is preserved."""
        deleted = await ctx.db.reset_all_data()
        ctx.db.invalidate_storage_stats_cache()
        total = sum(deleted.values())
        # Also clean up all model artifacts
        model_cleanup = await ctx.db.cleanup_orphaned_models(_model_dir(ctx))
        return {
            "success": True,
            "total_rows_deleted": total,
            "by_table": deleted,
            "model_files_deleted": model_cleanup.get("orphaned_files_deleted", 0),
        }

