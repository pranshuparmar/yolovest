"""Dry-run signal preview storage and scoring.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yolovest.scoring import path_aware_score
from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)


class DryRunMixin:
    # Dry-Run Signal Preview
    # ------------------------------------------------------------------

    async def _get_table_columns(self, table: str) -> set[str]:
        """Return the set of column names for a table."""
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in await cursor.fetchall()}

    async def insert_dry_run_results(
        self, run_id: str, signals: list[dict[str, Any]], as_of: str | None = None
    ) -> int:
        """Save dry-run signal results for next-day comparison.

        ``as_of`` is the historical date the run was evaluated against
        (None = latest data); stamped on every row so the history view can
        show which date a past run's signals were generated for.
        """
        columns = await self._get_table_columns("dry_run_results")

        # Ensure strategy_mode column exists (may be missing if migration 013 was skipped)
        if "strategy_mode" not in columns:
            try:
                await self.conn.execute(
                    "ALTER TABLE dry_run_results ADD COLUMN strategy_mode TEXT DEFAULT 'balanced'"
                )
                await self.conn.commit()
                columns.add("strategy_mode")
                logger.info("Added missing strategy_mode column to dry_run_results")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    logger.warning("Could not add strategy_mode column: %s", e)

        # Base columns (always present from migration 007)
        base_cols = [
            "run_id", "symbol", "signal_type", "entry_price", "target_price",
            "stop_loss_price", "confidence_score", "position_size", "model_version",
            "composite_score", "technical_score", "volume_momentum_score",
            "news_sentiment_score", "fundamental_score", "created_at",
        ]
        # Optional columns (from migration 013+, 016+, 047+)
        optional_cols = [
            "holding_period", "product", "volatility_score",
            "estimated_costs", "strategy_mode", "expected_holding_days", "as_of",
        ]
        insert_cols = base_cols + [c for c in optional_cols if c in columns]
        placeholders = ", ".join("?" if c != "created_at" else "datetime('now')" for c in insert_cols)
        col_names = ", ".join(insert_cols)
        value_cols = [c for c in insert_cols if c != "created_at"]

        for s in signals:
            values = tuple(
                run_id if c == "run_id"
                else as_of if c == "as_of"
                else s.get(c)
                for c in value_cols
            )
            await self.conn.execute(
                f"INSERT INTO dry_run_results ({col_names}) VALUES ({placeholders})",
                values,
            )
        await self.conn.commit()
        return len(signals)

    async def get_dry_run_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get dry-run results grouped by run_id, most recent first."""
        try:
            cursor = await self.conn.execute(
                "SELECT run_id, COUNT(*) as signal_count, "
                "MIN(created_at) as created_at, "
                "SUM(CASE WHEN direction_correct = 1 THEN 1 ELSE 0 END) as correct, "
                "SUM(CASE WHEN scored_at IS NOT NULL THEN 1 ELSE 0 END) as scored, "
                "MAX(strategy_mode) as strategy_mode, "
                "MAX(as_of) as as_of, MAX(model_version) as model_version "
                "FROM dry_run_results "
                "GROUP BY run_id ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        except Exception:
            # Fallback if strategy_mode / as_of columns don't exist (pre-migration 013/047)
            cursor = await self.conn.execute(
                "SELECT run_id, COUNT(*) as signal_count, "
                "MIN(created_at) as created_at, "
                "SUM(CASE WHEN direction_correct = 1 THEN 1 ELSE 0 END) as correct, "
                "SUM(CASE WHEN scored_at IS NOT NULL THEN 1 ELSE 0 END) as scored, "
                "MAX(model_version) as model_version "
                "FROM dry_run_results "
                "GROUP BY run_id ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def get_dry_run_signals(self, run_id: str) -> list[dict[str, Any]]:
        """Get all signals for a specific dry-run."""
        cursor = await self.read_conn.execute(
            "SELECT * FROM dry_run_results WHERE run_id = ? ORDER BY confidence_score DESC",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def delete_dry_run(self, run_id: str) -> int:
        """Delete all signals for a specific dry-run."""
        cursor = await self.conn.execute(
            "DELETE FROM dry_run_results WHERE run_id = ?",
            (run_id,),
        )
        await self.conn.commit()
        return cursor.rowcount

    async def get_dry_run_ids_needing_scoring(self) -> list[str]:
        """Run ids that still have at least one unscored signal."""
        cursor = await self.read_conn.execute(
            "SELECT DISTINCT run_id FROM dry_run_results WHERE scored_at IS NULL"
        )
        return [r[0] for r in await cursor.fetchall()]

    async def get_daily_ohlc_between(
        self, symbol: str, after_date: str, through_date: str,
    ) -> list[tuple[Any, ...]]:
        """Daily OHLC bars with after_date < date <= through_date (ascending).

        Returns (open, high, low, close, date) tuples — the holding-window
        slice used for path-aware scoring of predictions.
        """
        cursor = await self.read_conn.execute(
            "SELECT open, high, low, close, SUBSTR(timestamp, 1, 10) AS d "
            "FROM ohlcv WHERE symbol = ? AND interval = 'daily' "
            "AND SUBSTR(timestamp, 1, 10) > ? AND SUBSTR(timestamp, 1, 10) <= ? "
            "ORDER BY timestamp ASC",
            (symbol, after_date, through_date),
        )
        return [tuple(r) for r in await cursor.fetchall()]

    async def get_daily_bar_on(
        self, symbol: str, date: str,
    ) -> tuple[Any, ...] | None:
        """Single daily OHLC bar on a given date (for same-day predictions)."""
        cursor = await self.read_conn.execute(
            "SELECT open, high, low, close, SUBSTR(timestamp, 1, 10) AS d "
            "FROM ohlcv WHERE symbol = ? AND interval = 'daily' "
            "AND SUBSTR(timestamp, 1, 10) = ? LIMIT 1",
            (symbol, date),
        )
        row = await cursor.fetchone()
        return tuple(row) if row else None

    async def score_dry_run(self, run_id: str) -> dict[str, Any]:
        """Score a dry-run against each signal's TARGET-DATE actuals.

        A signal's target date is its as-of date (the date the run was
        evaluated for; falls back to the run's created date for a
        latest-data run) plus ``expected_holding_days`` *trading* days —
        realised by walking the daily bars that exist after the as-of
        date, so market holidays need no special handling. Scoring is
        path-aware over that holding window (``target_hit`` = price
        touched the target on any bar; direction / move measured at the
        window-end close) and PARTIAL: signals whose window hasn't fully
        elapsed are left pending, so a mixed-horizon run (balanced /
        long_term) scores whatever is ready and the rest on a later pass.

        Per-signal outcomes:
          - scored: full holding window elapsed and compared
          - pending: window not fully elapsed yet (try again later)
          - not_found: window should have elapsed but OHLCV is missing
        """
        signals = await self.get_dry_run_signals(run_id)
        if not signals:
            return {"scored": 0, "not_found": 0}

        today = now_ist().date()
        already_scored = scored = pending = not_found = 0
        unfound: list[dict[str, Any]] = []

        for sig in signals:
            if sig.get("scored_at"):
                already_scored += 1
                continue

            # Base date the signal was evaluated for. as_of (historical
            # run) is authoritative; otherwise the run's own created date.
            base_date = sig.get("as_of")
            if not base_date:
                raw_created = str(sig["created_at"])
                try:
                    ts = datetime.fromisoformat(raw_created.replace(" ", "T"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    base_date = ts.astimezone(IST).strftime("%Y-%m-%d")
                except Exception:
                    base_date = raw_created[:10]
            base_date = str(base_date)[:10]

            horizon = int(sig.get("expected_holding_days") or 0)
            if horizon < 1:
                horizon = 1

            # The first `horizon` trading bars strictly after the as-of date
            # ARE the holding window; the last is the target date.
            cursor = await self.read_conn.execute(
                "SELECT open, high, low, close, SUBSTR(timestamp, 1, 10) AS d "
                "FROM ohlcv WHERE symbol = ? AND interval = 'daily' "
                "AND SUBSTR(timestamp, 1, 10) > ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (sig["symbol"], base_date, horizon),
            )
            bars = await cursor.fetchall()

            if len(bars) < horizon:
                # Window not fully covered. If enough calendar time has
                # passed that the bars *should* exist, it's a data gap;
                # otherwise the window simply hasn't elapsed yet.
                try:
                    gap_days = (today - datetime.strptime(base_date, "%Y-%m-%d").date()).days
                except Exception:
                    gap_days = 0
                if gap_days > horizon * 2 + 7:
                    not_found += 1
                    unfound.append({
                        "symbol": sig["symbol"], "base_date": base_date,
                        "have": len(bars), "need": horizon,
                    })
                else:
                    pending += 1
                continue

            m = path_aware_score(
                bars, sig["entry_price"], sig.get("target_price"),
                sig.get("stop_loss_price"), sig["signal_type"],
            )
            await self.conn.execute(
                "UPDATE dry_run_results SET "
                "actual_open = ?, actual_close = ?, actual_high = ?, actual_low = ?, "
                "direction_correct = ?, target_hit = ?, actual_move_pct = ?, "
                "scored_at = datetime('now') "
                "WHERE id = ?",
                (m["actual_open"], m["actual_close"], m["actual_high"], m["actual_low"],
                 m["direction_correct"], m["target_hit"], m["actual_move_pct"],
                 sig["id"]),
            )
            scored += 1

        if scored > 0:
            await self.conn.commit()

        result: dict[str, Any] = {
            "scored": scored,
            "already_scored": already_scored,
            "pending": pending,
            "not_found": not_found,
        }
        if scored == 0 and pending > 0 and not_found == 0:
            result["message"] = (
                f"{pending} signal(s) not scored yet — the holding window "
                "hasn't fully elapsed. They'll score automatically once each "
                "target date passes."
            )
        if not_found > 0:
            sample = ", ".join(
                f"{u['symbol']} (as-of {u['base_date']})" for u in unfound[:5]
            )
            if len(unfound) > 5:
                sample += f", +{len(unfound) - 5} more"
            result["unfound"] = unfound
            result["message"] = (
                "Holding window elapsed but OHLCV is missing for: "
                + sample
                + ". Ingest the daily bars covering those target dates, then re-score."
            )
            logger.info(
                "score_dry_run %s: %d signals missing OHLCV — %s",
                run_id, not_found, sample,
            )
        return result

    async def bulk_delete(self, group: str) -> dict[str, int]:
        """Delete a group of related data. Returns {table: rows_deleted}.

        Groups:
        - paper / live: trades + predictions + signals + pending_trades for that mode
        - dry_runs: all dry run results
        - predictions / signals / pending_trades: clears the table across all modes

        Foreign-key safety: predictions.signal_id REFERENCES signals(id)
        and predictions.trade_id REFERENCES trades(trade_id) — both
        without ON DELETE CASCADE. With PRAGMA foreign_keys=ON,
        deleting a signal that has a linked prediction would raise
        SQLITE_CONSTRAINT_FOREIGNKEY and the row would survive. We
        delete predictions FIRST in any path that touches signals
        or trades.
        """
        deleted: dict[str, int] = {}

        async def _delete(table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
            try:
                if where:
                    cursor = await self.conn.execute(
                        f"DELETE FROM {table} WHERE {where}", params,
                    )
                else:
                    cursor = await self.conn.execute(f"DELETE FROM {table}")
                return cursor.rowcount
            except Exception:
                logger.warning(
                    "bulk_delete: DELETE from %s failed",
                    table, exc_info=True,
                )
                return 0

        if group in ("paper", "live"):
            mode = group
            # Predictions reference signals + trades; drop first.
            deleted["predictions"] = await _delete(
                "predictions", "mode = ?", (mode,),
            )
            deleted["signals"] = await _delete("signals", "mode = ?", (mode,))
            deleted["pending_trades"] = await _delete(
                "pending_trades", "mode = ?", (mode,),
            )
            deleted["trades"] = await _delete("trades", "mode = ?", (mode,))
            # llm_reviews for paper-mode trades only (live trades keep audit trail)
            if group == "paper":
                deleted["llm_reviews"] = await _delete(
                    "llm_reviews",
                    "trade_id IN (SELECT symbol FROM trades WHERE mode = 'paper')",
                )

        elif group == "dry_runs":
            deleted["dry_run_results"] = await _delete("dry_run_results")

        elif group == "predictions":
            for table in ("predictions", "prediction_scoreboard", "failure_analyses"):
                deleted[table] = await _delete(table)

        elif group == "signals":
            # Drop predictions referencing any signal first (FK-safe),
            # then signals. We NULL signal_id rather than deleting the
            # prediction so model-drift / scored-outcome history
            # survives the wipe.
            try:
                await self.conn.execute(
                    "UPDATE predictions SET signal_id = NULL "
                    "WHERE signal_id IS NOT NULL"
                )
            except Exception:
                logger.warning(
                    "bulk_delete: failed to null predictions.signal_id",
                    exc_info=True,
                )
            deleted["signals"] = await _delete("signals")

        elif group == "pending_trades":
            deleted["pending_trades"] = await _delete("pending_trades")

        else:
            raise ValueError(f"Unknown group: {group}")

        await self.conn.commit()
        total = sum(deleted.values())
        logger.warning("Bulk delete [%s]: deleted %d total rows — %s", group, total, deleted)
        return deleted

    async def reset_all_data(self) -> dict[str, int]:
        """Delete ALL rows from all data tables. Schema and migrations are preserved.

        Returns dict of table -> rows deleted.
        """
        tables = [
            "ohlcv", "news_articles", "economic_events", "audit_log",
            "predictions", "trades", "signals", "watchlist", "sentiment",
            "premarket", "llm_reviews", "fundamentals", "model_versions",
            "failure_analyses", "prediction_scoreboard", "reports",
            "agent_memory", "price_alerts", "dry_run_results",
            "user_watchlist",
        ]
        deleted: dict[str, int] = {}
        for table in tables:
            try:
                cursor = await self.conn.execute(f"DELETE FROM {table}")
                deleted[table] = cursor.rowcount
            except Exception:
                logger.debug("Could not reset table %s (may not exist)", table)
                deleted[table] = 0  # Table may not exist yet
        await self.conn.commit()
        # Reclaim disk space
        await self.conn.execute("VACUUM")
        total = sum(deleted.values())
        logger.warning("Full database reset: deleted %d total rows across %d tables", total, len(tables))
        return deleted

    async def restore_backup(
        self, backup_dir: str, filename: str, model_dir: str | None = None,
    ) -> dict[str, Any]:
        """Restore a database backup. Replaces current DB and optionally restores models.

        IMPORTANT: Caller must restart the application after restore.
        """
        import shutil

        backup_file = Path(backup_dir) / filename
        if not backup_file.exists():
            raise FileNotFoundError(f"Backup not found: {filename}")

        # Extract timestamp from filename (yolovest_YYYYMMDD_HHMMSS.db)
        stem = backup_file.stem  # yolovest_YYYYMMDD_HHMMSS
        timestamp_part = stem.replace("yolovest_", "")
        models_backup_dir = Path(backup_dir) / f"models_{timestamp_part}"

        # Close every connection before overwriting — the read
        # connections would otherwise keep serving the replaced inode.
        await self.close()

        # Restore database
        db_path = Path(self._db_path)
        # Remove WAL/SHM files
        for suffix in ["-wal", "-shm"]:
            wal_file = db_path.with_suffix(db_path.suffix + suffix)
            if wal_file.exists():
                wal_file.unlink()
        shutil.copy2(backup_file, db_path)
        logger.info("Database restored from %s", filename)

        result: dict[str, Any] = {"db_restored": True, "backup_file": filename}

        # Restore model artifacts if backup has them
        models_restored = 0
        if model_dir and models_backup_dir.is_dir():
            model_dest = Path(model_dir)
            model_dest.mkdir(parents=True, exist_ok=True)
            for pkl_file in models_backup_dir.glob("*.pkl"):
                try:
                    shutil.copy2(pkl_file, model_dest / pkl_file.name)
                    models_restored += 1
                except OSError as e:
                    logger.warning("Failed to restore model %s: %s", pkl_file.name, e)
            result["models_restored"] = models_restored

        # Reopen all connections and run any migrations the restored
        # snapshot is missing. (The old code assigned to the read-only
        # `conn` property — an AttributeError on every restore.)
        await self.initialize()

        return result

    async def delete_model_version(
        self, model_type: str, version: str, model_dir: str | None = None,
    ) -> dict[str, Any]:
        """Delete a model version from DB and remove its .pkl artifact from disk."""
        # Remove from DB
        cursor = await self.conn.execute(
            "DELETE FROM model_versions WHERE model_type = ? AND version = ?",
            (model_type, version),
        )
        await self.conn.commit()
        db_deleted = cursor.rowcount > 0

        # Remove .pkl file (and its sha256 sidecar) from disk
        file_deleted = False
        if model_dir:
            pkl_path = Path(model_dir) / f"{version}.pkl"
            if pkl_path.exists():
                pkl_path.unlink()
                file_deleted = True
                logger.info("Deleted model artifact: %s", pkl_path)
            sidecar = pkl_path.with_name(pkl_path.name + ".sha256")
            if sidecar.exists():
                sidecar.unlink()

        return {
            "model_type": model_type,
            "version": version,
            "db_deleted": db_deleted,
            "file_deleted": file_deleted,
        }

    async def cleanup_orphaned_models(self, model_dir: str) -> dict[str, Any]:
        """Remove .pkl files on disk that have NO matching DB row.

        "Orphan" means a file with no `model_versions` record at all —
        not "retired and therefore unused". Retired models keep their
        `.pkl` on disk until `cleanup_retired_models` deletes them
        based on `retraining.retired_model_cleanup_days`, which is the
        age-gated path that respects the configured grace period for
        rollback / re-shadow. The previous behaviour (deleting any
        file not in production/shadow status) nuked retired model
        artifacts on the very next maintenance run, making the
        `retired_model_cleanup_days` setting silently meaningless.
        """
        model_path = Path(model_dir)
        if not model_path.is_dir():
            return {"orphaned_files_deleted": 0}

        # Get every version on record, regardless of status. A file
        # whose version appears here is owned by the DB lifecycle —
        # promotion / retirement / age-based cleanup are responsible
        # for its eventual deletion, not this skill.
        cursor = await self.conn.execute(
            "SELECT version FROM model_versions"
        )
        rows = await cursor.fetchall()
        known_versions = {row[0] for row in rows}

        deleted = 0
        for pkl_file in model_path.glob("*.pkl"):
            # Extract version from filename (e.g., intraday_v20260325_180000.pkl → intraday_v20260325_180000)
            version = pkl_file.stem
            if version not in known_versions:
                try:
                    pkl_file.unlink()
                    deleted += 1
                    logger.info("Removed orphaned model: %s", pkl_file.name)
                except OSError as e:
                    logger.warning("Failed to remove orphaned model %s: %s", pkl_file.name, e)

        return {"orphaned_files_deleted": deleted}

    async def list_backups(self, backup_dir: str) -> list[dict[str, Any]]:
        """List available backup files with size, timestamp, and lock state.

        A backup is considered locked when a sibling sentinel file
        `<filename>.lock` exists in the same directory. Locked backups
        are skipped by the daily prune path and refused by the manual
        delete endpoint until explicitly unlocked. Storing the lock as
        a sentinel file (instead of a DB row) means it survives a
        volume restore and can be inspected with `ls`.
        """
        backup_path = Path(backup_dir)
        if not backup_path.is_dir():
            return []

        backups = []
        for f in sorted(backup_path.glob("yolovest_*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            backups.append({
                "filename": f.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=IST).isoformat(),
                "locked": (backup_path / f"{f.name}.lock").exists(),
            })
        return backups

    async def set_backup_lock(
        self, backup_dir: str, filename: str, locked: bool,
    ) -> dict[str, Any]:
        """Lock or unlock a backup so the daily prune / manual delete
        paths skip it. Same path-traversal guards as `delete_backup`.
        Idempotent: locking an already-locked backup is a no-op.
        """
        backup_path = Path(backup_dir).resolve()
        if not backup_path.is_dir():
            raise ValueError(f"Backup directory does not exist: {backup_dir}")

        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise ValueError(f"Invalid backup filename: {filename!r}")
        if not (filename.startswith("yolovest_") and filename.endswith(".db")):
            raise ValueError(
                f"Refusing to lock {filename!r}: not a recognised backup file",
            )

        target = (backup_path / filename).resolve()
        if backup_path not in target.parents:
            raise ValueError(f"Path escape attempt: {filename!r}")
        if not target.is_file():
            raise FileNotFoundError(f"Backup not found: {filename}")

        sentinel = backup_path / f"{filename}.lock"
        if locked:
            sentinel.touch(exist_ok=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                sentinel.unlink()
        return {"filename": filename, "locked": locked}

    async def delete_backup(self, backup_dir: str, filename: str) -> dict[str, Any]:
        """Delete a single backup file. Validates the name belongs to the
        backup directory and matches the standard yolovest_*.db pattern so
        a crafted path can't escape into other parts of the filesystem.
        Refuses to delete locked backups — caller must unlock first.
        """
        backup_path = Path(backup_dir).resolve()
        if not backup_path.is_dir():
            raise ValueError(f"Backup directory does not exist: {backup_dir}")

        # Disallow path components entirely — filename only.
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise ValueError(f"Invalid backup filename: {filename!r}")
        if not (filename.startswith("yolovest_") and filename.endswith(".db")):
            raise ValueError(
                f"Refusing to delete {filename!r}: not a recognised backup file",
            )

        target = (backup_path / filename).resolve()
        # Resolved path must still live inside backup_dir.
        if backup_path not in target.parents:
            raise ValueError(f"Path escape attempt: {filename!r}")
        if not target.is_file():
            raise FileNotFoundError(f"Backup not found: {filename}")

        if (backup_path / f"{filename}.lock").exists():
            raise PermissionError(
                f"Backup {filename!r} is locked; unlock it before deleting",
            )

        size_bytes = target.stat().st_size
        target.unlink()
        # Best-effort: also delete the matching model snapshot dir if it
        # exists, so the freed-bytes report reflects what actually went
        # away. Keyed off the timestamp portion (yolovest_<ts>.db ->
        # models_<ts>).
        import shutil as _shutil
        ts = filename.removeprefix("yolovest_").removesuffix(".db")
        model_dir = backup_path / f"models_{ts}"
        if model_dir.is_dir():
            try:
                model_size = sum(p.stat().st_size for p in model_dir.rglob("*") if p.is_file())
                _shutil.rmtree(model_dir)
                size_bytes += model_size
            except OSError as e:
                logger.warning("Failed to prune model dir %s: %s", model_dir.name, e)
        return {"filename": filename, "size_bytes": size_bytes}

    # ------------------------------------------------------------------
