"""Tests for database-maintenance skill."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from yolovest.skills.db_maintenance import DatabaseMaintenanceSkill

IST = ZoneInfo("Asia/Kolkata")


def _make_ctx(backup_enabled=True, backup_cron="0 18 * * *", backup_dir="./backups"):
    """Create a mock AppContext for testing."""
    ctx = MagicMock()
    ctx.config.database.backup_enabled = backup_enabled
    ctx.config.database.backup_cron = backup_cron
    ctx.config.database.backup_dir = backup_dir
    ctx.config.database.retention.ohlcv_days = 730
    ctx.config.database.retention.audit_log_days = 365
    ctx.config.database.retention.predictions_days = 365
    ctx.db.backup = AsyncMock(return_value="/backups/yolovest_20260322_180000.db")
    ctx.db.run_retention_cleanup = AsyncMock(
        return_value={"ohlcv": 5, "audit_log": 2, "predictions": 1}
    )
    ctx.db.log_audit = AsyncMock()
    ctx.notify.send = AsyncMock()
    return ctx


class TestDatabaseMaintenanceSkill:
    async def test_should_run_when_enabled(self):
        ctx = _make_ctx(backup_enabled=True)
        skill = DatabaseMaintenanceSkill(ctx)
        assert skill.should_run() is True

    async def test_should_not_run_when_disabled(self):
        ctx = _make_ctx(backup_enabled=False)
        skill = DatabaseMaintenanceSkill(ctx)
        assert skill.should_run() is False

    async def test_schedule_from_config(self):
        ctx = _make_ctx(backup_cron="30 17 * * *")
        skill = DatabaseMaintenanceSkill(ctx)
        assert skill.schedule == "30 17 * * *"

    async def test_successful_backup_and_retention(self):
        ctx = _make_ctx()
        skill = DatabaseMaintenanceSkill(ctx)
        result = await skill.execute()

        assert result.success is True
        assert result.data["backup_success"] is True
        assert result.data["backup_path"] == "/backups/yolovest_20260322_180000.db"
        assert result.data["retention_success"] is True
        assert result.data["retention_cleanup"]["ohlcv"] == 5

        ctx.db.backup.assert_called_once()
        ctx.db.run_retention_cleanup.assert_called_once_with(
            ohlcv_days=730, audit_days=365, predictions_days=365,
            news_days=ctx.config.database.retention.news_days,
            economic_events_days=ctx.config.database.retention.economic_events_days,
        )
        ctx.db.log_audit.assert_called_once()
        ctx.notify.send.assert_called()

    async def test_backup_failure_still_runs_retention(self):
        ctx = _make_ctx()
        ctx.db.backup = AsyncMock(side_effect=OSError("disk full"))
        skill = DatabaseMaintenanceSkill(ctx)
        result = await skill.execute()

        assert result.success is False
        assert result.data["backup_success"] is False
        assert "disk full" in result.data["backup_error"]
        # Retention should still run
        assert result.data["retention_success"] is True
        ctx.db.run_retention_cleanup.assert_called_once()

    async def test_retention_failure_reported(self):
        ctx = _make_ctx()
        ctx.db.run_retention_cleanup = AsyncMock(side_effect=Exception("db locked"))
        skill = DatabaseMaintenanceSkill(ctx)
        result = await skill.execute()

        assert result.success is False
        assert result.data["backup_success"] is True
        assert result.data["retention_success"] is False
        assert "db locked" in result.data["retention_error"]


class TestPruneOldBackups:
    def test_prune_keeps_recent(self, tmp_path):
        # Create 10 backup files with different mtimes
        for i in range(10):
            f = tmp_path / f"yolovest_{20260322 + i}_180000.db"
            f.write_text("data")

        pruned = DatabaseMaintenanceSkill._prune_old_backups(str(tmp_path), keep=7)
        assert pruned == 3
        remaining = list(tmp_path.glob("yolovest_*.db"))
        assert len(remaining) == 7

    def test_prune_no_backups(self, tmp_path):
        pruned = DatabaseMaintenanceSkill._prune_old_backups(str(tmp_path), keep=7)
        assert pruned == 0

    def test_prune_fewer_than_keep(self, tmp_path):
        for i in range(3):
            (tmp_path / f"yolovest_{20260322 + i}_180000.db").write_text("data")

        pruned = DatabaseMaintenanceSkill._prune_old_backups(str(tmp_path), keep=7)
        assert pruned == 0

    def test_prune_nonexistent_dir(self):
        pruned = DatabaseMaintenanceSkill._prune_old_backups("/nonexistent/path", keep=7)
        assert pruned == 0


class TestBackupRetentionDB:
    """Integration tests using the real Database class."""

    async def test_backup_creates_file(self, tmp_path):
        from yolovest.data.db import Database

        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        await db.initialize()

        try:
            backup_dir = str(tmp_path / "backups")
            path = await db.backup(backup_dir)
            assert Path(path).exists()
            assert Path(path).stat().st_size > 0
            assert "yolovest_" in Path(path).name
        finally:
            await db.close()

    async def test_retention_cleanup_deletes_old_data(self, tmp_path):
        from yolovest.data.db import Database
        from yolovest.models.schemas import OHLCVBar

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            # Insert old OHLCV data (3 years ago)
            old_bar = OHLCVBar(
                timestamp=datetime.now(IST) - timedelta(days=800),
                open=100, high=110, low=90, close=105, volume=1000,
            )
            await db.upsert_ohlcv("OLD_STOCK", "daily", [old_bar], "test")

            # Insert recent data
            new_bar = OHLCVBar(
                timestamp=datetime.now(IST) - timedelta(days=10),
                open=200, high=210, low=190, close=205, volume=2000,
            )
            await db.upsert_ohlcv("NEW_STOCK", "daily", [new_bar], "test")

            # Run retention with 730-day (2-year) cutoff
            deleted = await db.run_retention_cleanup(ohlcv_days=730)
            assert deleted["ohlcv"] == 1  # old bar deleted

            # Verify new data still exists
            bars = await db.get_ohlcv("NEW_STOCK", "daily", days=30)
            assert len(bars) == 1

            # Verify old data gone
            bars = await db.get_ohlcv("OLD_STOCK", "daily", days=1000)
            assert len(bars) == 0
        finally:
            await db.close()
