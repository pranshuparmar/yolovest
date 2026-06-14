"""Skill: database-maintenance — Automated backup and data retention.

Trigger: CRON — daily at configured time (default 18:00 IST).
Runs backup first, then retention cleanup, then prunes old backups.
"""

import contextlib
import logging
from pathlib import Path
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class DatabaseMaintenanceSkill(SkillBase):
    name = "database-maintenance"
    description = "Daily DB backup and data retention cleanup"
    trigger = SkillTrigger.CRON
    schedule = None  # Set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        return self.ctx.config.database.backup_cron

    def should_run(self) -> bool:
        return bool(self.ctx.config.database.backup_enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        results: dict[str, Any] = {}

        # --- Step 1: Backup ---
        backup_dir = self.ctx.config.database.backup_dir
        keep = int(getattr(self.ctx.config.database, "backup_keep", 7))
        try:
            # Pre-flight: a backup copies the live DB, so it needs at least
            # the DB's size free. Refuse (and alert) rather than filling the
            # disk — a full disk corrupts SQLite writes and crashes the app.
            skip_backup, low_disk_msg = self._disk_preflight(backup_dir)
            if skip_backup:
                results["backup_success"] = False
                results["backup_error"] = low_disk_msg
                logger.error("DB backup skipped: %s", low_disk_msg)
                with contextlib.suppress(Exception):
                    await self.ctx.notify.send(
                        f"DB backup SKIPPED — {low_disk_msg}", alert_type="errors",
                    )
            else:
                model_dir = getattr(self.ctx.config.strategy, "model_dir", "./models")
                backup_path = await self.ctx.db.backup(backup_dir, model_dir=model_dir)
                results["backup_path"] = backup_path
                results["backup_success"] = True
                logger.info("DB backup created: %s", backup_path)

                # Notify on success
                with contextlib.suppress(Exception):
                    await self.ctx.notify.send(f"DB backup created: {Path(backup_path).name}")

                # Prune old backups (keep the configured count)
                pruned = self._prune_old_backups(backup_dir, keep=keep)
                results["backups_pruned"] = pruned

        except Exception as e:
            results["backup_success"] = False
            results["backup_error"] = str(e)
            logger.error("DB backup failed: %s", e)
            with contextlib.suppress(Exception):
                await self.ctx.notify.send(f"DB backup FAILED: {e}", alert_type="errors")

        # --- Step 2: Retention Cleanup ---
        try:
            retention = self.ctx.config.database.retention
            # Floor the DAILY OHLCV window at the model's training history
            # so the nightly prune can never delete bars the next retrain
            # needs. This is also the survivorship-bias guard: a symbol that
            # leaves the index (or gets delisted) stops being ingested, so
            # its newest bar is frozen — without this floor a too-short
            # `ohlcv_days` ages out that symbol's ENTIRE history wholesale,
            # erasing the exited/loser names from the training set. The
            # retrain has no current-membership filter, so as long as the
            # bars survive inside the window they keep teaching the model.
            train_floor = max(
                int(getattr(self.ctx.config.retraining, "max_training_days", 0)),
                int(getattr(self.ctx.config.market_data, "backfill_days", 0)),
            )
            effective_ohlcv_days = max(retention.ohlcv_days, train_floor)
            if effective_ohlcv_days > retention.ohlcv_days:
                logger.info(
                    "Retention: daily OHLCV window raised %dd -> %dd to cover "
                    "the training history (max_training_days/backfill_days); "
                    "preserves exited/delisted symbols for the next retrain.",
                    retention.ohlcv_days, effective_ohlcv_days,
                )
            # Same survivorship floor for the 5-minute series: a deliberate
            # deep backfill (intraday_backfill_days, for the intraday model)
            # must not be silently pruned back to the default retention
            # window by the nightly maintenance run.
            intraday_floor = int(
                getattr(self.ctx.config.market_data, "intraday_backfill_days", 0)
            )
            effective_intraday_days = max(retention.intraday_ohlcv_days, intraday_floor)
            if effective_intraday_days > retention.intraday_ohlcv_days:
                logger.info(
                    "Retention: intraday OHLCV window raised %dd -> %dd to cover "
                    "the backfill depth (intraday_backfill_days); preserves "
                    "intraday training history.",
                    retention.intraday_ohlcv_days, effective_intraday_days,
                )
            deleted = await self.ctx.db.run_retention_cleanup(
                ohlcv_days=effective_ohlcv_days,
                intraday_ohlcv_days=effective_intraday_days,
                audit_days=retention.audit_log_days,
                predictions_days=retention.predictions_days,
                news_days=retention.news_days,
                economic_events_days=retention.economic_events_days,
                dry_run_days=getattr(retention, "dry_run_days", None),
            )
            results["retention_cleanup"] = deleted
            results["retention_success"] = True

            total_deleted = sum(deleted.values())
            if total_deleted > 0:
                logger.info(
                    "Retention cleanup: deleted %d rows total (%s)",
                    total_deleted, deleted,
                )
        except Exception as e:
            results["retention_success"] = False
            results["retention_error"] = str(e)
            logger.error("Retention cleanup failed: %s", e)

        # --- Step 3: Clean up orphaned model artifacts ---
        try:
            model_dir = getattr(self.ctx.config.strategy, "model_dir", "./models")
            model_cleanup = await self.ctx.db.cleanup_orphaned_models(model_dir)
            results["orphaned_models_deleted"] = model_cleanup.get("orphaned_files_deleted", 0)
        except Exception as e:
            logger.warning("Orphaned model cleanup failed: %s", e)

        # Prune old model backup directories (keep same count as DB backups)
        try:
            self._prune_old_model_backups(backup_dir, keep=keep)
        except Exception as e:
            logger.warning("Model backup pruning failed: %s", e)

        # --- Step 4: Auto-delete old retired models ---
        try:
            cleanup_days = self.ctx.config.retraining.retired_model_cleanup_days
            if cleanup_days > 0:
                deleted_models = await self.ctx.db.cleanup_retired_models(cleanup_days)
                results["retired_models_deleted"] = deleted_models
                if deleted_models > 0:
                    logger.info("Auto-deleted %d retired models older than %dd", deleted_models, cleanup_days)
        except Exception as e:
            logger.warning("Retired model cleanup failed: %s", e)

        # --- Step 5: Integrity check ---
        # Moved off the startup path: PRAGMA quick_check scans the whole DB
        # from disk (minutes on a multi-GB file) and was advisory-only, so it
        # was pure boot tax. Running it here keeps corruption detection during
        # a nightly, post-market, low-activity window.
        try:
            integrity = await self.ctx.db.check_integrity()
            results["integrity"] = integrity
            if integrity != "ok":
                with contextlib.suppress(Exception):
                    await self.ctx.notify.send(
                        f"DB INTEGRITY CHECK FAILED: {integrity} — data may be "
                        f"corrupted. Restore from the latest backup.",
                        alert_type="errors",
                    )
        except Exception as e:
            results["integrity"] = f"error: {e}"
            logger.warning("Integrity check step failed: %s", e)

        # --- Audit log ---
        with contextlib.suppress(Exception):
            await self.ctx.db.log_audit(
                action_type="database_maintenance",
                skill_name=self.name,
                output_summary=results,
            )

        success = results.get("backup_success", False) and results.get("retention_success", False)
        return SkillResult(success=success, skill_name=self.name, data=results)

    def _disk_preflight(self, backup_dir: str) -> tuple[bool, str]:
        """Decide whether to skip the backup for lack of disk space.

        A backup copies the live DB, so it needs roughly the DB's own size
        free. Returns ``(should_skip, message)`` — skips only when free space
        on the backup volume is positively below ~1.5× the DB size. Any error
        in the check itself returns ``(False, "")`` so a flaky preflight never
        blocks an otherwise-healthy backup.
        """
        import shutil

        try:
            db_path = self.ctx.config.database.path
            db_size = Path(db_path).stat().st_size if Path(db_path).exists() else 0
            if db_size <= 0:
                # No DB to size against (fresh install / mocked path) — nothing
                # to pre-flight; let the backup proceed.
                return False, ""
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(backup_dir).free
            needed = int(db_size * 1.5)
            if free < needed:
                mb = 1024 * 1024
                return True, (
                    f"low disk: {free // mb} MB free on the backup volume, "
                    f"need ~{needed // mb} MB (1.5x DB size {db_size // mb} MB)"
                )
            return False, ""
        except Exception as e:
            logger.debug("disk preflight check failed: %s", e, exc_info=True)
            return False, ""

    @staticmethod
    def _prune_old_backups(backup_dir: str, keep: int = 7) -> int:
        """Delete backup files older than the most recent `keep` backups.

        Locked backups (those with a sibling `<filename>.lock` sentinel)
        are skipped entirely — they're neither counted toward `keep` nor
        deleted. So locking a backup is purely additive: the prune still
        keeps the N most recent unlocked snapshots on top.
        """
        backup_path = Path(backup_dir)
        if not backup_path.is_dir():
            return 0

        all_backups = sorted(
            backup_path.glob("yolovest_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # Partition by lock state so locks float out of the rotation.
        unlocked = [
            p for p in all_backups
            if not (backup_path / f"{p.name}.lock").exists()
        ]

        pruned = 0
        for old_backup in unlocked[keep:]:
            try:
                old_backup.unlink()
                pruned += 1
                logger.debug("Pruned old backup: %s", old_backup.name)
            except OSError as e:
                logger.warning("Failed to prune backup %s: %s", old_backup.name, e)

        return pruned

    @staticmethod
    def _prune_old_model_backups(backup_dir: str, keep: int = 7) -> int:
        """Delete model backup directories older than the most recent `keep`.

        A model directory is treated as locked when its matching
        `yolovest_<ts>.db.lock` sentinel exists in the same backup
        directory — so locking a DB backup also pins its model snapshot.
        """
        backup_path = Path(backup_dir)
        if not backup_path.is_dir():
            return 0

        model_dirs = sorted(
            [d for d in backup_path.iterdir() if d.is_dir() and d.name.startswith("models_")],
            key=lambda p: p.name,
            reverse=True,
        )
        unlocked = [
            d for d in model_dirs
            if not (backup_path / f"yolovest_{d.name.removeprefix('models_')}.db.lock").exists()
        ]

        pruned = 0
        import shutil

        for old_dir in unlocked[keep:]:
            try:
                shutil.rmtree(old_dir)
                pruned += 1
                logger.debug("Pruned old model backup: %s", old_dir.name)
            except OSError as e:
                logger.warning("Failed to prune model backup %s: %s", old_dir.name, e)

        return pruned
