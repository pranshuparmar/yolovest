"""Tests for symbol quarantine (auto-block after repeated fetch failures)."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from yolovest.data.db import Database

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class TestQuarantineDB:
    """Integration tests for quarantine DB methods using in-memory SQLite."""

    @pytest.fixture
    async def db(self, tmp_path):
        """Create a real DB with migrations applied."""
        db = Database(str(tmp_path / "test.db"), migrations_dir=_MIGRATIONS_DIR)
        await db.initialize()
        return db

    async def test_record_failure_increments(self, db):
        result = await db.record_fetch_failure("GMRINFRA", "delisted")
        assert result is False  # 1st failure, not quarantined yet

        result = await db.record_fetch_failure("GMRINFRA", "delisted")
        assert result is False  # 2nd failure

        result = await db.record_fetch_failure("GMRINFRA", "delisted")
        assert result is True  # 3rd failure — quarantined!

    async def test_quarantined_symbol_in_set(self, db):
        for _ in range(3):
            await db.record_fetch_failure("GMRINFRA", "delisted")

        qset = await db.get_all_quarantined_symbol_set()
        assert "GMRINFRA" in qset

    async def test_success_resets_counter(self, db):
        await db.record_fetch_failure("TCS", "timeout")
        await db.record_fetch_failure("TCS", "timeout")
        # 2 failures, now success resets
        await db.record_fetch_success("TCS")

        qset = await db.get_all_quarantined_symbol_set()
        assert "TCS" not in qset

        # Should need 3 fresh failures to quarantine again
        for _ in range(2):
            await db.record_fetch_failure("TCS", "timeout")
        assert await db.is_quarantined("TCS") is False

    async def test_unquarantine(self, db):
        for _ in range(3):
            await db.record_fetch_failure("GMRINFRA", "delisted")
        assert await db.is_quarantined("GMRINFRA") is True

        removed = await db.unquarantine_symbol("GMRINFRA")
        assert removed is True
        assert await db.is_quarantined("GMRINFRA") is False

    async def test_get_quarantined_symbols(self, db):
        for _ in range(3):
            await db.record_fetch_failure("GMRINFRA", "delisted")
        for _ in range(3):
            await db.record_fetch_failure("SUZLON", "no data")

        result = await db.get_quarantined_symbols()
        symbols = {r["symbol"] for r in result}
        assert symbols == {"GMRINFRA", "SUZLON"}

    async def test_non_quarantined_not_in_set(self, db):
        await db.record_fetch_failure("TCS", "timeout")  # only 1 failure
        qset = await db.get_all_quarantined_symbol_set()
        assert "TCS" not in qset
