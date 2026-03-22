"""SQLite database layer with WAL mode and migration support.

Implements DatabaseProtocol from context.py. Uses aiosqlite for async access.
Schema versioned via numbered SQL migration files in migrations/ directory (FR-10.1).
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from yolovest.models.schemas import OHLCVBar

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Default migrations directory (relative to project root)
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


class Database:
    """Async SQLite database with WAL mode and migration support."""

    def __init__(self, db_path: str, migrations_dir: Path | None = None) -> None:
        self._db_path = db_path
        self._migrations_dir = migrations_dir or _DEFAULT_MIGRATIONS_DIR
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open connection, run migrations, enable WAL mode."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._run_migrations()
        logger.info("Database initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    # ------------------------------------------------------------------
    # Migration Runner (FR-10.1)
    # ------------------------------------------------------------------

    async def _run_migrations(self) -> None:
        """Apply numbered SQL migration files that haven't been applied yet."""
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  filename TEXT NOT NULL,"
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        await self.conn.commit()

        # Get already-applied versions
        cursor = await self.conn.execute("SELECT version FROM schema_version")
        applied = {row[0] for row in await cursor.fetchall()}

        # Find migration files
        if not self._migrations_dir.exists():
            logger.warning("Migrations directory not found: %s", self._migrations_dir)
            return

        migration_files = sorted(self._migrations_dir.glob("*.sql"))
        for mf in migration_files:
            version = int(mf.name.split("_")[0])
            if version in applied:
                continue

            logger.info("Applying migration %s", mf.name)
            sql = mf.read_text()
            await self.conn.executescript(sql)
            await self.conn.execute(
                "INSERT INTO schema_version (version, filename) VALUES (?, ?)",
                (version, mf.name),
            )
            await self.conn.commit()
            logger.info("Migration %s applied", mf.name)

    async def get_schema_version(self) -> int:
        """Return the highest applied migration version, or 0 if none."""
        cursor = await self.conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check if the database is accessible and writable."""
        try:
            await self.conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # System State
    # ------------------------------------------------------------------

    async def is_kill_switch_active(self) -> bool:
        cursor = await self.conn.execute(
            "SELECT value FROM system_state WHERE key = 'kill_switch'"
        )
        row = await cursor.fetchone()
        return row is not None and row[0] == "active"

    async def set_system_state(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )
        await self.conn.commit()

    async def get_system_state(self, key: str) -> str | None:
        cursor = await self.conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # OHLCV Data
    # ------------------------------------------------------------------

    async def upsert_ohlcv(
        self,
        symbol: str,
        interval: str,
        bars: list[OHLCVBar],
        source: str,
    ) -> int:
        """Insert or update OHLCV bars. Returns count of rows upserted."""
        if not bars:
            return 0
        rows = [
            (
                symbol,
                interval,
                bar.timestamp.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                source,
            )
            for bar in bars
        ]
        await self.conn.executemany(
            "INSERT INTO ohlcv (symbol, interval, timestamp, open, high, low, close, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, interval, timestamp) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume, source=excluded.source, "
            "ingested_at=datetime('now')",
            rows,
        )
        await self.conn.commit()
        return len(rows)

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30
    ) -> list[OHLCVBar]:
        """Fetch OHLCV bars for a symbol, most recent `days` days."""
        cursor = await self.conn.execute(
            "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
            "WHERE symbol = ? AND interval = ? "
            "AND timestamp >= datetime('now', ? || ' days') "
            "ORDER BY timestamp ASC",
            (symbol, interval, str(-days)),
        )
        rows = await cursor.fetchall()
        return [
            OHLCVBar(
                timestamp=datetime.fromisoformat(row[0]),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    async def upsert_watchlist(self, stocks: list[dict]) -> None:
        """Replace watchlist with new scored stocks."""
        await self.conn.execute("DELETE FROM watchlist")
        for stock in stocks:
            await self.conn.execute(
                "INSERT INTO watchlist (symbol, composite_score, technical_score, "
                "volume_momentum_score, news_sentiment_score, fundamental_score, sector) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stock.get("symbol"),
                    stock.get("composite_score"),
                    stock.get("technical_score"),
                    stock.get("volume_momentum_score"),
                    stock.get("news_sentiment_score"),
                    stock.get("fundamental_score"),
                    stock.get("sector"),
                ),
            )
        await self.conn.commit()

    async def get_watchlist(self) -> list[dict]:
        """Get current watchlist ordered by composite score."""
        cursor = await self.conn.execute(
            "SELECT symbol, composite_score, technical_score, volume_momentum_score, "
            "news_sentiment_score, fundamental_score, sector, updated_at "
            "FROM watchlist ORDER BY composite_score DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Positions (read from trades table)
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[dict]:
        """Get trades with status 'open' or 'partially_filled'."""
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE status IN ('open', 'partially_filled')"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Audit Log (NFR-5)
    # ------------------------------------------------------------------

    async def log_audit(
        self,
        action_type: str,
        skill_name: str | None = None,
        input_summary: dict | None = None,
        output_summary: dict | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Log an audit entry for decision traceability."""
        now_ist = datetime.now(IST).isoformat()
        await self.conn.execute(
            "INSERT INTO audit_log (timestamp_ist, action_type, skill_name, "
            "input_summary, output_summary, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (
                now_ist,
                action_type,
                skill_name,
                json.dumps(input_summary) if input_summary else None,
                json.dumps(output_summary) if output_summary else None,
                duration_ms,
            ),
        )
        await self.conn.commit()
