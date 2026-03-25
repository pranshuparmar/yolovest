"""Skill: database-maintenance — Automated backup and data retention (FR-10.2, FR-10.3).

Trigger: CRON — daily at configured time (default 18:00 IST).
Runs backup first, then retention cleanup, then prunes old backups.
"""

import contextlib
import logging
from pathlib import Path
from typing import Any
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST

logger = logging.getLogger(__name__)


class DatabaseMaintenanceSkill(SkillBase):
    name = "database-maintenance"
    description = "Daily DB backup and data retention cleanup"
    trigger = SkillTrigger.CRON
    schedule = None  # Set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.ctx.config.database.backup_cron

    def should_run(self) -> bool:
        return bool(self.ctx.config.database.backup_enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        results: dict[str, Any] = {}

        # --- Step 1: Backup (FR-10.2) ---
        try:
            backup_dir = self.ctx.config.database.backup_dir
            backup_path = await self.ctx.db.backup(backup_dir)
            results["backup_path"] = backup_path
            results["backup_success"] = True
            logger.info("DB backup created: %s", backup_path)

            # Notify on success
            with contextlib.suppress(Exception):
                await self.ctx.notify.send(f"DB backup created: {Path(backup_path).name}")

            # Prune old backups (keep last 7)
            pruned = self._prune_old_backups(backup_dir, keep=7)
            results["backups_pruned"] = pruned

        except Exception as e:
            results["backup_success"] = False
            results["backup_error"] = str(e)
            logger.error("DB backup failed: %s", e)
            with contextlib.suppress(Exception):
                await self.ctx.notify.send(f"DB backup FAILED: {e}")

        # --- Step 2: Retention Cleanup (FR-10.3) ---
        try:
            retention = self.ctx.config.database.retention
            deleted = await self.ctx.db.run_retention_cleanup(
                ohlcv_days=retention.ohlcv_days,
                audit_days=retention.audit_log_days,
                predictions_days=retention.predictions_days,
                news_days=retention.news_days,
                economic_events_days=retention.economic_events_days,
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

        # --- Audit log ---
        with contextlib.suppress(Exception):
            await self.ctx.db.log_audit(
                action_type="database_maintenance",
                skill_name=self.name,
                output_summary=results,
            )

        success = results.get("backup_success", False) and results.get("retention_success", False)
        return SkillResult(success=success, skill_name=self.name, data=results)

    @staticmethod
    def _prune_old_backups(backup_dir: str, keep: int = 7) -> int:
        """Delete backup files older than the most recent `keep` backups."""
        backup_path = Path(backup_dir)
        if not backup_path.is_dir():
            return 0

        backups = sorted(
            backup_path.glob("yolovest_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        pruned = 0
        for old_backup in backups[keep:]:
            try:
                old_backup.unlink()
                pruned += 1
                logger.debug("Pruned old backup: %s", old_backup.name)
            except OSError as e:
                logger.warning("Failed to prune backup %s: %s", old_backup.name, e)

        return pruned
