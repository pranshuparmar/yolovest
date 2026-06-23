"""Backups, retention cleanup, storage stats, agent-memory persistence.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from yolovest.timezone import now_ist, now_utc

logger = logging.getLogger(__name__)


class MaintenanceMixin:
    # Declared to match DatabaseCore.__init__ — the mixin invalidates
    # the storage-stats cache after destructive operations.
    _storage_stats_cache: dict[str, Any] | None
    _storage_stats_cache_at: float
    # Backup & Retention
    # ------------------------------------------------------------------

    async def backup(self, backup_dir: str, model_dir: str | None = None) -> str:
        """Create a timestamped backup of the database and model artifacts.

        Uses SQLite's online backup API (via VACUUM INTO) which produces a
        consistent, self-contained backup even while the database is being
        written to. This is safer than checkpoint + file copy, which can
        produce corrupt backups if writes happen between the two operations.

        Args:
            backup_dir: Directory to store backup files.
            model_dir: Optional path to ML model artifacts (.pkl files).
                If provided, model files are copied into a subdirectory of the backup.
        """
        import shutil

        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        timestamp = now_ist().strftime("%Y%m%d_%H%M%S")
        backup_path = str(Path(backup_dir) / f"yolovest_{timestamp}.db")

        # VACUUM INTO creates a clean, defragmented, self-contained
        # copy (no WAL/SHM needed). It fails with "cannot VACUUM - SQL
        # statements in progress" when the connection has an open
        # transaction — and our write connection usually does, because
        # Python's deferred isolation auto-begins one on the first DML
        # and leaves it open. The fix is simply to COMMIT first to
        # close that transaction, then VACUUM on the SAME connection.
        #
        # NB: do NOT run VACUUM on a second connection to the same
        # WAL-mode DB — the two connections contend and VACUUM hangs.
        # Same-connection-after-commit is the reliable path.
        try:
            await self.conn.commit()
            await self.conn.execute("VACUUM INTO ?", (backup_path,))
            logger.info("Database backup created (VACUUM INTO): %s", backup_path)
        except Exception as e:
            # Fallback: checkpoint + copy. Still produces a usable
            # backup, just uncompacted and with a small torn-copy risk
            # if a write lands during the copy.
            logger.warning(
                "VACUUM INTO failed (%s), falling back to checkpoint + copy", e,
            )
            await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self.conn.commit()
            shutil.copy2(self._db_path, backup_path)
            logger.info("Database backup created (file copy): %s", backup_path)

        # Backup ML model artifacts alongside the DB
        models_backed_up = 0
        if model_dir:
            model_src = Path(model_dir)
            if model_src.is_dir():
                models_backup_dir = Path(backup_dir) / f"models_{timestamp}"
                models_backup_dir.mkdir(parents=True, exist_ok=True)
                for pkl_file in model_src.glob("*.pkl"):
                    try:
                        shutil.copy2(pkl_file, models_backup_dir / pkl_file.name)
                        models_backed_up += 1
                    except OSError as e:
                        logger.warning("Failed to backup model %s: %s", pkl_file.name, e)
                if models_backed_up:
                    logger.info(
                        "Backed up %d model artifacts to %s",
                        models_backed_up, models_backup_dir,
                    )

        return backup_path

    async def run_retention_cleanup(
        self,
        ohlcv_days: int = 730,
        audit_days: int = 365,
        predictions_days: int = 365,
        news_days: int = 180,
        economic_events_days: int = 365,
        intraday_ohlcv_days: int | None = None,
        dry_run_days: int | None = None,
    ) -> dict[str, Any]:
        """Delete data older than retention periods.

        Daily and intraday OHLCV are trimmed on SEPARATE windows because
        the intraday series (5-min / 1-min) is ~75-375× heavier per day,
        so it gets its own `intraday_ohlcv_days` (defaults to ohlcv_days
        for backwards-compat when the caller doesn't pass it). Both windows
        are training-history windows now: the daily window feeds the swing
        model, the intraday window feeds the 5-min intraday model — so
        db_maintenance floors each at the relevant backfill depth before
        calling this, and neither may be pruned below what the next retrain
        needs.
        """

        now = now_utc()
        deleted = {}

        # Daily OHLCV retention (the training-history window).
        cutoff = (now - timedelta(days=ohlcv_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM ohlcv WHERE interval = 'daily' AND timestamp < ?",
            (cutoff,),
        )
        deleted["ohlcv"] = cursor.rowcount

        # Intraday OHLCV retention (decoupled — 5-min / 1-min bars are
        # ~75-375× heavier per day, so they ride a separate, caller-floored
        # window that must still cover the intraday model's training depth).
        # When the caller doesn't supply intraday_ohlcv_days, fall back to
        # ohlcv_days so existing behaviour (single retention) is preserved.
        intraday_window = (
            intraday_ohlcv_days if intraday_ohlcv_days is not None else ohlcv_days
        )
        intraday_cutoff = (now - timedelta(days=intraday_window)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM ohlcv WHERE interval != 'daily' AND timestamp < ?",
            (intraday_cutoff,),
        )
        deleted["ohlcv_intraday"] = cursor.rowcount

        # Audit log retention
        cutoff = (now - timedelta(days=audit_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM audit_log WHERE timestamp_ist < ?", (cutoff,)
        )
        deleted["audit_log"] = cursor.rowcount

        # Predictions retention
        cutoff = (now - timedelta(days=predictions_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM predictions WHERE created_at < ?", (cutoff,)
        )
        deleted["predictions"] = cursor.rowcount

        # News articles retention
        cutoff = (now - timedelta(days=news_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM news_articles WHERE created_at < ?", (cutoff,)
        )
        deleted["news_articles"] = cursor.rowcount

        # Economic events retention
        cutoff = (now - timedelta(days=economic_events_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM economic_events WHERE created_at < ?", (cutoff,)
        )
        deleted["economic_events"] = cursor.rowcount

        # Sentiment retention: delete entries older than 7 days
        # (stale sentiment is already ignored in scanning via TTL,
        #  this just cleans up the table to prevent unbounded growth)
        cutoff = (now - timedelta(days=7)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM sentiment WHERE created_at < ?", (cutoff,)
        )
        deleted["sentiment"] = cursor.rowcount

        # Dry-run previews: one row per generated signal per run. No FK
        # dependents, so safe to time-prune. Skipped when the caller doesn't
        # pass a window (backwards-compatible).
        if dry_run_days is not None:
            cutoff = (now - timedelta(days=dry_run_days)).isoformat()
            cursor = await self.conn.execute(
                "DELETE FROM dry_run_results WHERE created_at < ?", (cutoff,)
            )
            deleted["dry_run_results"] = cursor.rowcount

        await self.conn.commit()
        logger.info("Retention cleanup: %s", deleted)
        return deleted

    # ------------------------------------------------------------------
    # Storage Stats & Manual Cleanup
    # ------------------------------------------------------------------

    # Cache TTL for storage stats. Stats are advisory — exact freshness
    # isn't required and the queries are expensive on populated DBs.
    _STORAGE_STATS_TTL_SEC: float = 60.0

    async def get_storage_stats(self, force_refresh: bool = False) -> dict[str, Any]:
        """Get row counts and date ranges for all major tables.

        Cached for `_STORAGE_STATS_TTL_SEC` so repeated dashboard
        polls don't re-scan multi-million-row tables. Pass
        force_refresh=True after a destructive operation (cleanup,
        bulk delete, restore) to invalidate the cache.
        """
        import os
        import time as _time

        now = _time.monotonic()
        if (
            not force_refresh
            and self._storage_stats_cache is not None
            and (now - self._storage_stats_cache_at) < self._STORAGE_STATS_TTL_SEC
        ):
            return self._storage_stats_cache

        tables = {
            "ohlcv": {"ts_col": "timestamp"},
            "news_articles": {"ts_col": "created_at"},
            "economic_events": {"ts_col": "created_at"},
            "audit_log": {"ts_col": "timestamp_ist"},
            "predictions": {"ts_col": "created_at"},
            "trades": {"ts_col": "created_at"},
            "agent_memory": {"ts_col": "updated_at"},
        }
        stats: dict[str, Any] = {}

        for table, meta in tables.items():
            ts_col = meta["ts_col"]
            try:
                cursor = await self.conn.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cursor.fetchone()
                count = row[0] if row else 0

                oldest = newest = None
                if count > 0:
                    cursor = await self.conn.execute(
                        f"SELECT MIN({ts_col}), MAX({ts_col}) FROM {table}"
                    )
                    row = await cursor.fetchone()
                    if row:
                        oldest, newest = row[0], row[1]

                stats[table] = {
                    "row_count": count,
                    "oldest": oldest,
                    "newest": newest,
                }
            except Exception:
                logger.debug("Failed to get stats for table %s", table, exc_info=True)
                stats[table] = {"row_count": 0, "oldest": None, "newest": None}

        # Database file size
        try:
            db_size = os.path.getsize(self._db_path)
            wal_path = self._db_path + "-wal"
            wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            stats["_db_file"] = {
                "db_bytes": db_size,
                "wal_bytes": wal_size,
                "total_bytes": db_size + wal_size,
            }
        except OSError:
            stats["_db_file"] = {"db_bytes": 0, "wal_bytes": 0, "total_bytes": 0}

        self._storage_stats_cache = stats
        self._storage_stats_cache_at = now
        return stats

    def invalidate_storage_stats_cache(self) -> None:
        """Drop the cached storage stats so the next call recomputes
        from scratch. Called after destructive operations.
        """
        self._storage_stats_cache = None
        self._storage_stats_cache_at = 0.0

    async def cleanup_table(self, table: str, older_than_days: int) -> int:
        """Delete rows older than N days from a specific table. Returns rows deleted."""

        # Whitelist of tables + their timestamp columns
        allowed = {
            "ohlcv": "timestamp",
            "news_articles": "created_at",
            "economic_events": "created_at",
            "audit_log": "timestamp_ist",
            "predictions": "created_at",
        }
        ts_col = allowed.get(table)
        if ts_col is None:
            raise ValueError(f"Cleanup not allowed for table: {table}")

        cutoff = (now_utc() - timedelta(days=older_than_days)).isoformat()
        cursor = await self.conn.execute(
            f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)
        )
        await self.conn.commit()
        deleted = cursor.rowcount
        logger.info("Manual cleanup: deleted %d rows from %s (older than %d days)", deleted, table, older_than_days)
        return deleted

    # ------------------------------------------------------------------
    # Agent Memory Persistence
    # ------------------------------------------------------------------

    async def get_memory(self, namespace: str, key: str) -> dict[str, Any] | None:
        """Retrieve a memory entry by namespace and key."""
        cursor = await self.conn.execute(
            "SELECT key, value, expires_at FROM agent_memory "
            "WHERE namespace = ? AND key = ?",
            (namespace, key),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def set_memory(
        self, namespace: str, key: str, value: str, expires_at: str | None = None
    ) -> None:
        """Upsert a memory entry."""
        now = now_utc().isoformat()
        await self.conn.execute(
            "INSERT INTO agent_memory (namespace, key, value, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at, "
            "expires_at = excluded.expires_at",
            (namespace, key, value, now, now, expires_at),
        )
        await self.conn.commit()

    async def delete_memory(self, namespace: str, key: str) -> None:
        """Delete a memory entry."""
        await self.conn.execute(
            "DELETE FROM agent_memory WHERE namespace = ? AND key = ?",
            (namespace, key),
        )
        await self.conn.commit()

    async def list_memory_keys(self, namespace: str) -> list[str]:
        """List all keys in a namespace."""
        cursor = await self.conn.execute(
            "SELECT key FROM agent_memory WHERE namespace = ?", (namespace,)
        )
        rows = await cursor.fetchall()
        return [row["key"] for row in rows]

    async def get_all_memory(self, namespace: str) -> list[dict[str, Any]]:
        """Get all entries in a namespace."""
        cursor = await self.conn.execute(
            "SELECT key, value, expires_at FROM agent_memory WHERE namespace = ?",
            (namespace,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def cleanup_expired_memory(self) -> int:
        """Delete expired memory entries. Returns count deleted."""
        now = now_utc().isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM agent_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        await self.conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
