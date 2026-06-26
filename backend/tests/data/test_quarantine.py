"""Tests for symbol quarantine (auto-block after repeated fetch failures)."""

from pathlib import Path

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
        yield db
        await db.close()

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


class TestResolveSymbolsWithReplacements:
    """End-to-end behavior of the resolver used by every ingest path."""

    @pytest.fixture
    async def db(self, tmp_path):
        d = Database(str(tmp_path / "test.db"), migrations_dir=_MIGRATIONS_DIR)
        await d.initialize()
        yield d
        await d.close()

    async def test_active_symbols_pass_through(self, db):
        result = await db.resolve_symbols_with_replacements(["RELIANCE", "TCS"])
        assert result == ["RELIANCE", "TCS"]

    async def test_quarantined_without_replacement_is_dropped(self, db):
        # Quarantine ZOMATO with no replacement
        for _ in range(3):
            await db.record_fetch_failure("ZOMATO", "delisted")
        result = await db.resolve_symbols_with_replacements(
            ["RELIANCE", "ZOMATO", "TCS"],
        )
        assert result == ["RELIANCE", "TCS"]

    async def test_quarantined_with_replacement_is_substituted(self, db):
        for _ in range(3):
            await db.record_fetch_failure("ZOMATO", "delisted")
        await db.set_replacement_symbol("ZOMATO", "ETERNAL")
        result = await db.resolve_symbols_with_replacements(
            ["RELIANCE", "ZOMATO", "TCS"],
        )
        assert result == ["RELIANCE", "ETERNAL", "TCS"]

    async def test_replacement_dedupes_with_existing_active(self, db):
        for _ in range(3):
            await db.record_fetch_failure("ZOMATO", "delisted")
        await db.set_replacement_symbol("ZOMATO", "ETERNAL")
        # Both ZOMATO (renamed via repl) and a separately-listed ETERNAL appear.
        # The output should contain ETERNAL once, not twice.
        result = await db.resolve_symbols_with_replacements(
            ["ETERNAL", "ZOMATO", "TCS"],
        )
        assert result == ["ETERNAL", "TCS"]

    async def test_active_symbols_unchanged_when_no_quarantine(self, db):
        # No quarantine state set — resolver should be a passthrough modulo dedup
        result = await db.resolve_symbols_with_replacements(
            ["RELIANCE", "TCS", "RELIANCE", "INFY"],
        )
        assert result == ["RELIANCE", "TCS", "INFY"]
