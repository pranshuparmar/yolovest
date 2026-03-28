"""Tests for the Bhavcopy CSV importer."""

import csv
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from yolovest.data.bhavcopy import BhavcopyImporter, _find_col, _parse_date


class TestParseDate:
    def test_dmy_short_month(self):
        result = _parse_date("20-Mar-2024")
        assert result == datetime(2024, 3, 20)

    def test_dmy_full_month(self):
        result = _parse_date("20-March-2024")
        assert result == datetime(2024, 3, 20)

    def test_iso_format(self):
        result = _parse_date("2024-03-20")
        assert result == datetime(2024, 3, 20)

    def test_slash_format(self):
        result = _parse_date("20/03/2024")
        assert result == datetime(2024, 3, 20)

    def test_compact_format(self):
        result = _parse_date("20240320")
        assert result == datetime(2024, 3, 20)

    def test_invalid_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_whitespace_stripped(self):
        result = _parse_date("  2024-03-20  ")
        assert result == datetime(2024, 3, 20)


class TestFindCol:
    def test_exact_match(self):
        assert _find_col(["SYMBOL", "OPEN"], ("SYMBOL",)) == "SYMBOL"

    def test_case_insensitive(self):
        assert _find_col(["symbol", "open"], ("SYMBOL",)) == "symbol"

    def test_first_candidate_wins(self):
        assert _find_col(["TckrSymb", "SYMBOL"], ("SYMBOL", "TckrSymb")) == "SYMBOL"

    def test_fallback_candidate(self):
        assert _find_col(["TckrSymb", "OPEN"], ("SYMBOL", "TckrSymb")) == "TckrSymb"

    def test_no_match_returns_none(self):
        assert _find_col(["FOO", "BAR"], ("SYMBOL",)) is None

    def test_whitespace_in_headers(self):
        assert _find_col(["  SYMBOL  ", "OPEN"], ("SYMBOL",)) == "SYMBOL"


class TestBhavcopyImporterParseCSV:
    def _write_csv(self, path: Path, headers: list[str], rows: list[list[str]]):
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)

    def test_standard_format(self, tmp_path):
        csv_file = tmp_path / "bhavcopy.csv"
        self._write_csv(csv_file, [
            "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TOTTRDQTY", "TIMESTAMP"
        ], [
            ["RELIANCE", "EQ", "2500", "2550", "2480", "2530", "1000000", "20-Mar-2024"],
            ["TCS", "EQ", "3800", "3850", "3780", "3820", "500000", "20-Mar-2024"],
            ["RELIANCE", "BE", "2500", "2550", "2480", "2530", "100", "20-Mar-2024"],
        ])

        result = BhavcopyImporter._parse_csv(csv_file, "EQ")
        assert "RELIANCE" in result["bars"]
        assert "TCS" in result["bars"]
        assert len(result["bars"]["RELIANCE"]) == 1  # BE series filtered out
        assert result["skipped"] == 1

        bar = result["bars"]["RELIANCE"][0]
        assert bar.open == 2500.0
        assert bar.close == 2530.0
        assert bar.volume == 1000000

    def test_new_format_columns(self, tmp_path):
        csv_file = tmp_path / "bhavcopy_new.csv"
        self._write_csv(csv_file, [
            "TckrSymb", "SctySrs", "OpnPric", "HghPric",
            "LwPric", "ClsPric", "TtlTradgVol", "TradDt",
        ], [
            ["INFY", "EQ", "1500", "1550", "1480", "1520", "2000000", "2024-03-20"],
        ])

        result = BhavcopyImporter._parse_csv(csv_file, "EQ")
        assert "INFY" in result["bars"]
        bar = result["bars"]["INFY"][0]
        assert bar.close == 1520.0

    def test_zero_price_rows_skipped(self, tmp_path):
        csv_file = tmp_path / "bad_prices.csv"
        self._write_csv(csv_file, [
            "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TOTTRDQTY", "TIMESTAMP"
        ], [
            ["BAD", "EQ", "0", "100", "50", "80", "1000", "20-Mar-2024"],
        ])

        result = BhavcopyImporter._parse_csv(csv_file, "EQ")
        assert len(result["bars"]) == 0
        assert result["skipped"] == 1

    def test_no_series_filter(self, tmp_path):
        csv_file = tmp_path / "all_series.csv"
        self._write_csv(csv_file, [
            "SYMBOL", "OPEN", "HIGH", "LOW", "CLOSE", "TOTTRDQTY", "TIMESTAMP"
        ], [
            ["RELIANCE", "2500", "2550", "2480", "2530", "1000000", "20-Mar-2024"],
        ])

        # No series column → no filtering
        result = BhavcopyImporter._parse_csv(csv_file, "EQ")
        assert "RELIANCE" in result["bars"]

    def test_missing_required_columns(self, tmp_path):
        csv_file = tmp_path / "bad_headers.csv"
        self._write_csv(csv_file, ["FOO", "BAR"], [["1", "2"]])

        with pytest.raises(ValueError, match="Missing required columns"):
            BhavcopyImporter._parse_csv(csv_file, "EQ")


class TestBhavcopyImporterImport:
    async def test_import_file(self, tmp_path):
        # Write a CSV
        csv_file = tmp_path / "test.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "SYMBOL", "SERIES", "OPEN", "HIGH",
                "LOW", "CLOSE", "TOTTRDQTY", "TIMESTAMP",
            ])
            writer.writerow([
                "RELIANCE", "EQ", "2500", "2550",
                "2480", "2530", "1000000", "20-Mar-2024",
            ])
            writer.writerow([
                "TCS", "EQ", "3800", "3850",
                "3780", "3820", "500000", "20-Mar-2024",
            ])

        mock_db = MagicMock()
        mock_db.upsert_ohlcv = AsyncMock(return_value=1)

        importer = BhavcopyImporter(mock_db)
        result = await importer.import_file(csv_file)

        assert result["rows_imported"] == 2
        assert result["rows_skipped"] == 0
        assert mock_db.upsert_ohlcv.call_count == 2  # one per symbol
        # Verify source is "bhavcopy"
        for call in mock_db.upsert_ohlcv.call_args_list:
            assert call.args[3] == "bhavcopy" or call.kwargs.get("source") == "bhavcopy"

    async def test_import_directory(self, tmp_path):
        for i in range(3):
            csv_file = tmp_path / f"bhavcopy_{i}.csv"
            with open(csv_file, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "SYMBOL", "SERIES", "OPEN", "HIGH",
                    "LOW", "CLOSE", "TOTTRDQTY", "TIMESTAMP",
                ])
                writer.writerow([
                    "RELIANCE", "EQ", "2500", "2550",
                    "2480", "2530", "1000000", f"2{i}-Mar-2024",
                ])

        mock_db = MagicMock()
        mock_db.upsert_ohlcv = AsyncMock(return_value=1)

        importer = BhavcopyImporter(mock_db)
        stats = await importer.import_directory(tmp_path)

        assert stats["files_processed"] == 3
        assert stats["rows_imported"] == 3
        assert len(stats["errors"]) == 0

    async def test_import_directory_not_found(self):
        mock_db = MagicMock()
        importer = BhavcopyImporter(mock_db)

        with pytest.raises(FileNotFoundError):
            await importer.import_directory("/nonexistent/path")

    async def test_import_empty_directory(self, tmp_path):
        mock_db = MagicMock()
        importer = BhavcopyImporter(mock_db)
        stats = await importer.import_directory(tmp_path)
        assert stats["files_processed"] == 0

    async def test_import_handles_bad_file_gracefully(self, tmp_path):
        # Write a good file and a bad file
        good = tmp_path / "good.csv"
        with open(good, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "SYMBOL", "SERIES", "OPEN", "HIGH",
                "LOW", "CLOSE", "TOTTRDQTY", "TIMESTAMP",
            ])
            writer.writerow([
                "RELIANCE", "EQ", "2500", "2550",
                "2480", "2530", "1000000", "20-Mar-2024",
            ])

        bad = tmp_path / "bad.csv"
        with open(bad, "w") as f:
            f.write("this is not csv\n")

        mock_db = MagicMock()
        mock_db.upsert_ohlcv = AsyncMock(return_value=1)

        importer = BhavcopyImporter(mock_db)
        stats = await importer.import_directory(tmp_path)

        assert stats["files_processed"] >= 1  # good file processed
        assert len(stats["errors"]) >= 1  # bad file errored
