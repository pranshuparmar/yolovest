"""Bhavcopy CSV importer for deep historical backtesting.

Bulk imports NSE Bhavcopy CSV files (daily EOD data, 2013+) into the OHLCV table.
Designed as a one-time seed operation. Supports both old-format and new-format CSVs.

NSE Bhavcopy CSV columns (standard format):
  SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE, TOTTRDQTY, TOTTRDVAL, TIMESTAMP, TOTALTRADES, ISIN

Usage:
    importer = BhavcopyImporter(db)
    stats = await importer.import_directory("./data/bhavcopy")
"""

import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from yolovest.models.schemas import OHLCVBar

logger = logging.getLogger(__name__)

# Known column name variants across different Bhavcopy formats
_SYMBOL_COLS = ("SYMBOL", "TckrSymb", "TCKRSYMB")
_SERIES_COLS = ("SERIES", "SctySrs", "SCTYSRS")
_OPEN_COLS = ("OPEN", "OpnPric", "OPNPRIC")
_HIGH_COLS = ("HIGH", "HghPric", "HGHPRIC")
_LOW_COLS = ("LOW", "LwPric", "LWPRIC")
_CLOSE_COLS = ("CLOSE", "ClsPric", "CLSPRIC")
_VOLUME_COLS = ("TOTTRDQTY", "TtlTradgVol", "TTLTRADGVOL", "VOLUME")
_DATE_COLS = ("TIMESTAMP", "TradDt", "TRADDT", "DATE")

# Date formats seen in various Bhavcopy CSVs
_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d")


def _find_col(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    """Find the first matching column name from candidates (case-insensitive)."""
    header_map = {h.strip().upper(): h.strip() for h in headers}
    for c in candidates:
        if c.upper() in header_map:
            return header_map[c.upper()]
    return None


def _parse_date(value: str) -> datetime | None:
    """Try multiple date formats to parse a date string."""
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


class BhavcopyImporter:
    """Import NSE Bhavcopy CSV files into the database OHLCV table."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def import_directory(
        self,
        directory: str | Path,
        series_filter: str = "EQ",
        batch_size: int = 500,
    ) -> dict[str, Any]:
        """Import all CSV files from a directory.

        Args:
            directory: Path to directory containing Bhavcopy CSV files.
            series_filter: Only import rows with this series (default "EQ" for equity).
            batch_size: Number of bars to upsert per DB call.

        Returns:
            Dict with import stats: files_processed, rows_imported, rows_skipped, errors.
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise FileNotFoundError(f"Bhavcopy directory not found: {directory}")

        csv_files = sorted(directory.glob("*.csv"))
        if not csv_files:
            logger.warning("No CSV files found in %s", directory)
            return {"files_processed": 0, "rows_imported": 0, "rows_skipped": 0, "errors": []}

        stats: dict[str, Any] = {
            "files_processed": 0,
            "rows_imported": 0,
            "rows_skipped": 0,
            "errors": [],
        }

        for csv_file in csv_files:
            try:
                file_stats = await self.import_file(
                    csv_file, series_filter=series_filter, batch_size=batch_size
                )
                stats["files_processed"] += 1
                stats["rows_imported"] += file_stats["rows_imported"]
                stats["rows_skipped"] += file_stats["rows_skipped"]
            except Exception as e:
                stats["errors"].append(f"{csv_file.name}: {e}")
                logger.warning("Failed to import %s: %s", csv_file.name, e)

        logger.info(
            "Bhavcopy import complete: %d files, %d rows imported, %d skipped, %d errors",
            stats["files_processed"],
            stats["rows_imported"],
            stats["rows_skipped"],
            len(stats["errors"]),
        )
        return stats

    async def import_file(
        self,
        file_path: str | Path,
        series_filter: str = "EQ",
        batch_size: int = 500,
    ) -> dict[str, int]:
        """Import a single Bhavcopy CSV file.

        Returns:
            Dict with rows_imported and rows_skipped counts.
        """
        file_path = Path(file_path)
        bars_by_symbol: dict[str, list[OHLCVBar]] = {}
        rows_skipped = 0

        # Parse CSV in a thread to avoid blocking the event loop
        parsed = await asyncio.to_thread(
            self._parse_csv, file_path, series_filter
        )
        bars_by_symbol = parsed["bars"]
        rows_skipped = parsed["skipped"]

        # Upsert in batches per symbol
        rows_imported = 0
        for symbol, bars in bars_by_symbol.items():
            for i in range(0, len(bars), batch_size):
                batch = bars[i : i + batch_size]
                count = await self._db.upsert_ohlcv(symbol, "daily", batch, "bhavcopy")
                rows_imported += count

        logger.debug(
            "Imported %s: %d rows from %d symbols, %d skipped",
            file_path.name,
            rows_imported,
            len(bars_by_symbol),
            rows_skipped,
        )
        return {"rows_imported": rows_imported, "rows_skipped": rows_skipped}

    @staticmethod
    def _parse_csv(
        file_path: Path, series_filter: str
    ) -> dict[str, Any]:
        """Parse a Bhavcopy CSV file (synchronous, runs in thread)."""
        bars_by_symbol: dict[str, list[OHLCVBar]] = {}
        skipped = 0

        with open(file_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return {"bars": {}, "skipped": 0}

            headers = list(reader.fieldnames)

            # Map columns
            sym_col = _find_col(headers, _SYMBOL_COLS)
            series_col = _find_col(headers, _SERIES_COLS)
            open_col = _find_col(headers, _OPEN_COLS)
            high_col = _find_col(headers, _HIGH_COLS)
            low_col = _find_col(headers, _LOW_COLS)
            close_col = _find_col(headers, _CLOSE_COLS)
            vol_col = _find_col(headers, _VOLUME_COLS)
            date_col = _find_col(headers, _DATE_COLS)

            if not all([sym_col, open_col, high_col, low_col, close_col, date_col]):
                raise ValueError(
                    f"Missing required columns in {file_path.name}. "
                    f"Found: {headers}"
                )

            for row in reader:
                # Filter by series if column exists
                if series_col and series_filter:
                    series = row.get(series_col, "").strip()
                    if series != series_filter:
                        skipped += 1
                        continue

                try:
                    symbol = row[sym_col].strip()
                    ts = _parse_date(row[date_col])
                    if ts is None:
                        skipped += 1
                        continue

                    open_price = float(row[open_col].strip())
                    high_price = float(row[high_col].strip())
                    low_price = float(row[low_col].strip())
                    close_price = float(row[close_col].strip())
                    volume = int(float(row[vol_col].strip())) if vol_col and row.get(vol_col) else 0

                    # Skip rows with zero/negative prices
                    if any(p <= 0 for p in (open_price, high_price, low_price, close_price)):
                        skipped += 1
                        continue

                    bar = OHLCVBar(
                        timestamp=ts,
                        open=open_price,
                        high=high_price,
                        low=low_price,
                        close=close_price,
                        volume=volume,
                    )

                    if symbol not in bars_by_symbol:
                        bars_by_symbol[symbol] = []
                    bars_by_symbol[symbol].append(bar)

                except (ValueError, KeyError):
                    skipped += 1
                    continue

        return {"bars": bars_by_symbol, "skipped": skipped}
