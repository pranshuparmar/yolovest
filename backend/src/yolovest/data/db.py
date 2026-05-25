"""SQLite database layer with WAL mode and migration support.

Implements DatabaseProtocol from context.py. Uses aiosqlite for async access.
Schema versioned via numbered SQL migration files in migrations/ directory.
"""

import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from pathlib import Path

import aiosqlite

from typing import Any

from yolovest.models.schemas import EconomicEvent, NewsArticle, OHLCVBar, SentimentResult
from yolovest.timezone import IST, UTC, now_ist, now_utc

logger = logging.getLogger(__name__)

# Default migrations directory (relative to project root)
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def _normalize_iso_date(raw: Any) -> str | None:
    """Parse an NSE deal-date string into ISO YYYY-MM-DD form.

    NSE has shipped at least these formats over time:
      - "19-May-2026"  (display, %d-%b-%Y)
      - "19/05/2026"   (slash-DDMMYYYY)
      - "19-05-2026"   (dash-DDMMYYYY)
      - "2026-05-19"   (already ISO)
    Returns None if the value is empty or unparseable so the caller
    can fall back to "today".
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


class DuplicateSignalError(Exception):
    """Raised when a trade insert collides with an existing
    trades.signal_id (the UNIQUE index added in migration 042). Lets
    trade-execute recognise "this signal already produced a trade"
    and return the existing row instead of crashing the heartbeat.
    """

    def __init__(self, signal_id: int, existing_trade_id: str) -> None:
        super().__init__(
            f"signal_id={signal_id} already attached to trade "
            f"{existing_trade_id}",
        )
        self.signal_id = signal_id
        self.existing_trade_id = existing_trade_id


def _canonical_ohlcv_ts(ts: datetime, interval: str) -> str:
    """Canonical tz-NAIVE timestamp string for an OHLCV bar so the same bar
    from different providers collapses onto ONE unique key instead of
    duplicating. The root cause of 581K duplicate day-rows was kite writing
    tz-aware ('...+05:30') and yfinance/jugaad writing tz-naive ('...T00:00:00')
    for the same day — different strings, so the (symbol, interval, timestamp)
    constraint didn't dedupe. Daily → date at midnight; intraday → wall-clock
    to the second (all bars are IST clock time, so dropping tz is correct)."""
    if interval == "daily":
        return ts.strftime("%Y-%m-%dT00:00:00")
    return ts.strftime("%Y-%m-%dT%H:%M:%S")


class Database:
    """Async SQLite database with WAL mode, read/write separation, and migration support.

    Uses two connection types:
    - Write connection (_conn): single connection for all writes, with
      PRAGMA synchronous=FULL for crash safety.
    - Read connection (_read_conn): separate read-only connection, allowing
      concurrent reads even during writes (WAL mode benefit).
    """

    def __init__(self, db_path: str, migrations_dir: Path | None = None) -> None:
        self._db_path = db_path
        self._migrations_dir = migrations_dir or _DEFAULT_MIGRATIONS_DIR
        self._conn: aiosqlite.Connection | None = None
        self._read_conn: aiosqlite.Connection | None = None
        # Cached storage_stats result. Each COUNT(*)+MIN/MAX over the
        # large tables (ohlcv, audit_log, predictions) is a full scan;
        # the page was waiting on 7 of them serially. Stats shift
        # slowly so a short TTL is fine.
        self._storage_stats_cache: dict[str, Any] | None = None
        self._storage_stats_cache_at: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open connection, run migrations, enable WAL mode with hardened settings."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row

        # -- Durability & concurrency hardening --
        # WAL mode: concurrent reads during writes, crash-safe journal
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Sync WAL to disk on every commit (FULL = safest, ~2x slower than NORMAL)
        await self._conn.execute("PRAGMA synchronous=FULL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Wait up to 5s for locks instead of failing immediately with SQLITE_BUSY
        await self._conn.execute("PRAGMA busy_timeout=5000")

        # -- Integrity check on startup (fast check, not full page scan) --
        await self._check_integrity()

        # Read connection — separate, for concurrent reads during writes.
        # Opens in read-only mode so it can't accidentally mutate data.
        try:
            self._read_conn = await aiosqlite.connect(
                f"file:{self._db_path}?mode=ro", uri=True,
            )
            self._read_conn.row_factory = aiosqlite.Row
            await self._read_conn.execute("PRAGMA busy_timeout=5000")
        except Exception as e:
            logger.warning("Read-only connection failed (%s), using single connection", e)
            self._read_conn = None

        await self._run_migrations()
        logger.info("Database initialized at %s (read_conn=%s)", self._db_path,
                     "enabled" if self._read_conn else "disabled")

    async def _check_integrity(self) -> None:
        """Run a quick integrity check on startup.

        Uses `PRAGMA quick_check` (checks B-tree structure without scanning
        every page) which is much faster than `PRAGMA integrity_check`.
        Logs a critical warning if corruption is detected but does NOT
        abort — allows the app to start so backups can be taken.
        """
        try:
            cursor = await self._conn.execute("PRAGMA quick_check")
            row = await cursor.fetchone()
            result = row[0] if row else "unknown"
            if result != "ok":
                logger.critical(
                    "DATABASE INTEGRITY CHECK FAILED: %s — "
                    "data may be corrupted. Take a backup immediately.",
                    result,
                )
            else:
                logger.debug("Database integrity check passed")
        except Exception as e:
            logger.warning("Database integrity check could not run: %s", e)

    async def close(self) -> None:
        """Close all database connections."""
        if self._read_conn:
            await self._read_conn.close()
            self._read_conn = None
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Write connection — use for INSERT/UPDATE/DELETE."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    @property
    def read_conn(self) -> aiosqlite.Connection:
        """Read connection — use for SELECT queries.

        Falls back to write connection if read connection is not available
        (e.g., in-memory databases or older SQLite without URI support).
        """
        if self._read_conn is not None:
            return self._read_conn
        return self.conn

    # ------------------------------------------------------------------
    # Migration Runner
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
            stmts = self._split_sql(sql)

            # Separate ALTER TABLE statements from others — ALTER TABLE
            # in SQLite cannot run inside explicit transactions in some builds.
            alter_stmts = [s for s in stmts if s.lstrip("-").lstrip().upper().startswith("ALTER")]
            other_stmts = [s for s in stmts if s not in alter_stmts]

            try:
                # Run ALTER TABLE statements outside transaction, one at a time
                for stmt in alter_stmts:
                    try:
                        await self.conn.execute(stmt)
                        await self.conn.commit()
                    except Exception as stmt_err:
                        if "duplicate column" in str(stmt_err).lower():
                            logger.info("Skipping (column already exists): %s", stmt[:80])
                            continue
                        raise

                # Run remaining statements in a transaction. NOTE:
                # under the default deferred isolation_level, sqlite3
                # implicitly COMMITs before each DDL statement on
                # Python < 3.12, so a CREATE TABLE that ran before a
                # later statement failed is NOT undone by rollback().
                # The schema_version row is still not written (the
                # raise below skips it), so the migration is retried
                # on next startup — which is why every migration's
                # CREATE/ALTER must be IF NOT EXISTS / tolerant of
                # partial prior application.
                if other_stmts:
                    try:
                        for stmt in other_stmts:
                            await self.conn.execute(stmt)
                        await self.conn.commit()
                    except Exception:
                        await self.conn.rollback()
                        raise

                # Record migration as applied
                await self.conn.execute(
                    "INSERT OR IGNORE INTO schema_version (version, filename) VALUES (?, ?)",
                    (version, mf.name),
                )
                await self.conn.commit()
            except Exception:
                logger.error("Migration %s failed", mf.name, exc_info=True)
                raise
            logger.info("Migration %s applied", mf.name)
            logger.info("Migration %s applied", mf.name)

    @staticmethod
    def _split_sql(sql: str) -> list[str]:
        """Split SQL text into individual statements, skipping empty ones.

        Strips ``--`` line comments first so that semicolons inside
        commented prose don't fragment the statement on the wrong
        boundary.
        """
        lines = [
            line for line in sql.splitlines()
            if not line.lstrip().startswith("--")
        ]
        cleaned = "\n".join(lines)
        return [s.strip() for s in cleaned.split(";") if s.strip()]

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
            logger.exception("Database health check failed")
            return False

    # ------------------------------------------------------------------
    # System State
    # ------------------------------------------------------------------

    async def is_kill_switch_active(self) -> bool:
        cursor = await self.read_conn.execute(
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
        cursor = await self.read_conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Config (UI-editable settings)
    # ------------------------------------------------------------------

    async def get_all_config(self) -> dict[str, str]:
        """Return all config key-value pairs from the config table."""
        cursor = await self.read_conn.execute(
            "SELECT key, value FROM config ORDER BY key"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_config(self, key: str) -> str | None:
        """Get a single config value by key."""
        cursor = await self.read_conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_config(self, key: str, value: str) -> None:
        """Upsert a single config value."""
        await self.conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )
        await self.conn.commit()

    async def set_config_bulk(self, items: dict[str, str]) -> int:
        """Upsert multiple config values in a single transaction. Returns count."""
        if not items:
            return 0
        # First DML auto-begins a transaction (Python sqlite3 default
        # isolation_level). Explicit BEGIN would conflict with concurrent
        # writers sharing the same aiosqlite connection.
        try:
            for key, value in items.items():
                await self.conn.execute(
                    "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value),
                )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return len(items)

    async def is_config_empty(self) -> bool:
        """Check if the config table has any rows."""
        cursor = await self.read_conn.execute("SELECT COUNT(*) FROM config")
        row = await cursor.fetchone()
        return row[0] == 0

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
                _canonical_ohlcv_ts(bar.timestamp, interval),
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

    async def update_delivery_pct(
        self, symbol: str, delivery_pct: float, date_str: str | None = None,
    ) -> bool:
        """Stamp `delivery_pct` on the daily bar for `symbol` on
        `date_str` (defaults to today IST). Used as both a live
        institutional-conviction signal and a future ML feature.
        Returns True when a row was updated.
        """
        ts = date_str or now_ist().strftime("%Y-%m-%d")
        cursor = await self.conn.execute(
            "UPDATE ohlcv SET delivery_pct = ? "
            "WHERE symbol = ? AND interval = 'daily' "
            "  AND substr(timestamp, 1, 10) = ?",
            (float(delivery_pct), symbol, ts),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_recent_delivery_pct(
        self, symbol: str, lookback_days: int = 5,
    ) -> float | None:
        """Average delivery % over the last N daily bars for a symbol,
        or None when no data is available. Used by both risk_check
        (live conviction signal) and model_retrain (ML feature).
        """
        cursor = await self.read_conn.execute(
            "SELECT AVG(delivery_pct) FROM ("
            "  SELECT delivery_pct FROM ohlcv "
            "  WHERE symbol = ? AND interval = 'daily' "
            "    AND delivery_pct IS NOT NULL "
            "  ORDER BY timestamp DESC LIMIT ?"
            ")",
            (symbol, int(lookback_days)),
        )
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

    async def get_ohlcv(
        self, symbol: str, interval: str, days: int = 30,
        end: datetime | None = None,
    ) -> list[OHLCVBar]:
        """Fetch OHLCV bars for a symbol.

        By default returns the most recent `days` days. When `end` is set
        (an "as of" timestamp), returns the `days`-day window ENDING at
        `end` instead — used by the historical dry-run to evaluate signals
        as they would have looked on a past date (no look-ahead).
        """
        from datetime import timedelta

        end_dt = end or now_utc()
        cutoff = (end_dt - timedelta(days=days)).isoformat()
        query = (
            "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
            "WHERE symbol = ? AND interval = ? AND timestamp >= ? "
        )
        params: list[Any] = [symbol, interval, cutoff]
        if end is not None:
            query += "AND timestamp <= ? "
            params.append(end_dt.isoformat())
        query += "ORDER BY timestamp ASC"
        cursor = await self.read_conn.execute(query, tuple(params))
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
    # Symbol sectors (canonical lookup populated from NSE constituents)
    # ------------------------------------------------------------------

    async def upsert_symbol_sectors(self, records: list[dict[str, str]]) -> int:
        """Bulk-upsert sector / industry records keyed by symbol.

        `records` is a list of `{"symbol", "industry"}` (and optionally
        "sector"). We treat the CSV's Industry as the sector when no
        explicit sector is provided — niftyindices.com only exposes
        Industry but it's specific enough to drive sector-cap logic.

        Returns the number of rows touched.
        """
        if not records:
            return 0
        ts = now_utc().isoformat()
        touched = 0
        for r in records:
            sym = (r.get("symbol") or "").upper()
            if not sym:
                continue
            sector = r.get("sector") or r.get("industry") or None
            industry = r.get("industry") or None
            await self.conn.execute(
                "INSERT INTO symbol_sectors (symbol, sector, industry, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "  sector = COALESCE(excluded.sector, symbol_sectors.sector), "
                "  industry = COALESCE(excluded.industry, symbol_sectors.industry), "
                "  updated_at = excluded.updated_at",
                (sym, sector, industry, ts),
            )
            touched += 1
        await self.conn.commit()
        return touched

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    async def upsert_watchlist(self, stocks: list[dict[str, Any]]) -> None:
        """Replace watchlist with new scored stocks (atomic).

        Relies on Python sqlite3's default deferred isolation: the first
        DML auto-begins, commit() ends. Explicit BEGIN here would conflict
        with any other concurrent writer on the same connection.
        """
        try:
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
        except Exception:
            await self.conn.rollback()
            raise

    async def get_watchlist(self) -> list[dict[str, Any]]:
        """Get current watchlist ordered by composite score.

        Sector is resolved via COALESCE(symbol_sectors, watchlist) so
        rows added before ingest-universe populated the canonical lookup
        still show their sector once it's available.
        """
        cursor = await self.read_conn.execute(
            "SELECT w.symbol, w.composite_score, w.technical_score, "
            "w.volume_momentum_score, w.news_sentiment_score, "
            "w.fundamental_score, COALESCE(ss.sector, w.sector) as sector, "
            "w.updated_at "
            "FROM watchlist w "
            "LEFT JOIN symbol_sectors ss ON w.symbol = ss.symbol "
            "ORDER BY w.composite_score DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def add_watchlist_symbol(self, symbol: str, sector: str | None = None) -> bool:
        """Add a symbol to the algorithmic watchlist (legacy, used by market-scan)."""
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, composite_score, technical_score, "
                "volume_momentum_score, news_sentiment_score, fundamental_score, sector) "
                "VALUES (?, NULL, NULL, NULL, NULL, NULL, ?)",
                (symbol.upper(), sector),
            )
            await self.conn.commit()
            return True
        except Exception:
            logger.warning("Failed to add %s to watchlist", symbol, exc_info=True)
            return False

    async def remove_watchlist_symbol(self, symbol: str) -> bool:
        """Remove a symbol from the algorithmic watchlist (legacy)."""
        cursor = await self.conn.execute(
            "DELETE FROM watchlist WHERE symbol = ?", (symbol.upper(),)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # User Watchlist (separate from algorithmic watchlist)
    # ------------------------------------------------------------------

    async def get_user_watchlist(self) -> list[dict[str, Any]]:
        """Get user-managed watchlist symbols."""
        cursor = await self.conn.execute(
            "SELECT uw.symbol, uw.sector, uw.notes, uw.created_at, "
            "w.composite_score, w.technical_score, w.volume_momentum_score, "
            "w.news_sentiment_score, w.fundamental_score "
            "FROM user_watchlist uw "
            "LEFT JOIN watchlist w ON uw.symbol = w.symbol "
            "ORDER BY uw.created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def add_user_watchlist_symbol(
        self, symbol: str, sector: str | None = None, notes: str | None = None
    ) -> bool:
        """Add a symbol to the user watchlist. Returns True if inserted."""
        try:
            await self.conn.execute(
                "INSERT OR IGNORE INTO user_watchlist (symbol, sector, notes) VALUES (?, ?, ?)",
                (symbol.upper(), sector, notes),
            )
            await self.conn.commit()
            return True
        except Exception:
            logger.warning("Failed to add %s to user watchlist", symbol, exc_info=True)
            return False

    async def remove_user_watchlist_symbol(self, symbol: str) -> bool:
        """Remove a symbol from the user watchlist."""
        cursor = await self.conn.execute(
            "DELETE FROM user_watchlist WHERE symbol = ?", (symbol.upper(),)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_combined_watchlist(self) -> list[dict[str, Any]]:
        """Get merged watchlist for signal generation: algorithmic + user picks.

        User watchlist symbols are included even if not in algorithmic top-N.
        Deduplicates by symbol, preferring the algorithmic entry (has scores).
        Adds a 'source' field: 'algo', 'user', or 'both'.
        """
        algo = await self.get_watchlist()
        user = await self.get_user_watchlist()

        algo_symbols = {s["symbol"] for s in algo}
        user_symbols = {s["symbol"] for s in user}

        combined = []
        for s in algo:
            s["source"] = "both" if s["symbol"] in user_symbols else "algo"
            combined.append(s)

        # Add user-only symbols (not in algo list)
        for s in user:
            if s["symbol"] not in algo_symbols:
                combined.append({
                    "symbol": s["symbol"],
                    "composite_score": s.get("composite_score"),
                    "technical_score": s.get("technical_score"),
                    "volume_momentum_score": s.get("volume_momentum_score"),
                    "news_sentiment_score": s.get("news_sentiment_score"),
                    "fundamental_score": s.get("fundamental_score"),
                    "sector": s.get("sector"),
                    "updated_at": s.get("created_at"),
                    "source": "user",
                })

        # Exclude quarantined symbols
        quarantined = await self.get_all_quarantined_symbol_set()
        if quarantined:
            combined = [s for s in combined if s["symbol"] not in quarantined]

        return combined

    # ------------------------------------------------------------------
    # Watchlist rotation stats
    # ------------------------------------------------------------------

    async def record_signal_outcome(
        self, symbol: str, produced_signal: bool,
        threshold: int = 8, cooldown_hours: int = 4,
    ) -> None:
        """Track per-symbol signal productivity. Symbols that fail to produce an
        actionable signal for `threshold` consecutive heartbeats are placed on a
        rotation cooldown so market-scan can free the slot for a fresh candidate.
        """
        symbol = symbol.upper()
        if produced_signal:
            await self.conn.execute(
                "INSERT INTO watchlist_signal_stats (symbol, no_signal_streak, cooldown_until, updated_at) "
                "VALUES (?, 0, NULL, datetime('now')) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "no_signal_streak = 0, cooldown_until = NULL, updated_at = datetime('now')",
                (symbol,),
            )
            await self.conn.commit()
            return

        cursor = await self.conn.execute(
            "SELECT no_signal_streak FROM watchlist_signal_stats WHERE symbol = ?",
            (symbol,),
        )
        row = await cursor.fetchone()
        streak = (row[0] if row else 0) + 1
        if streak >= threshold:
            cooldown_until = (datetime.now(UTC) + timedelta(hours=cooldown_hours)).isoformat()
            await self.conn.execute(
                "INSERT INTO watchlist_signal_stats (symbol, no_signal_streak, cooldown_until, updated_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "no_signal_streak = excluded.no_signal_streak, "
                "cooldown_until = excluded.cooldown_until, "
                "updated_at = datetime('now')",
                (symbol, streak, cooldown_until),
            )
        else:
            await self.conn.execute(
                "INSERT INTO watchlist_signal_stats (symbol, no_signal_streak, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(symbol) DO UPDATE SET "
                "no_signal_streak = excluded.no_signal_streak, "
                "updated_at = datetime('now')",
                (symbol, streak),
            )
        await self.conn.commit()

    async def get_rotation_cooldown_symbols(self) -> set[str]:
        """Return symbols currently in rotation cooldown (cooldown_until > now)."""
        now_iso = datetime.now(UTC).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT symbol FROM watchlist_signal_stats "
            "WHERE cooldown_until IS NOT NULL AND cooldown_until > ?",
            (now_iso,),
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def clear_rotation_cooldown(self, symbol: str | None = None) -> int:
        """One-shot reset of the watchlist-rotation cooldown. When the
        threshold/cooldown defaults were too aggressive, ~80% of a
        nifty500 universe could end up benched within hours. This
        clears the cooldown flag (and resets the streak counter) so
        market-scan immediately reconsiders the affected symbols.
        Pass a symbol to clear just that row; otherwise clears all.
        Returns the number of rows affected.
        """
        if not await self._table_exists("watchlist_signal_stats"):
            return 0
        if symbol:
            cursor = await self.conn.execute(
                "UPDATE watchlist_signal_stats "
                "SET no_signal_streak = 0, cooldown_until = NULL, "
                "    updated_at = datetime('now') "
                "WHERE symbol = ?",
                (symbol.upper(),),
            )
        else:
            cursor = await self.conn.execute(
                "UPDATE watchlist_signal_stats "
                "SET no_signal_streak = 0, cooldown_until = NULL, "
                "    updated_at = datetime('now')",
            )
        await self.conn.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Positions (read from trades table)
    # ------------------------------------------------------------------

    async def get_open_positions(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get trades with status 'open' or 'partially_filled'.

        Args:
            mode: Filter by trading mode ('paper' or 'live'). None = all modes.
        """
        query = "SELECT * FROM trades WHERE status IN ('open', 'partially_filled')"
        params: list[Any] = []
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        cursor = await self.read_conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Audit Log
    # ------------------------------------------------------------------

    async def log_audit(
        self,
        action_type: str,
        skill_name: str | None = None,
        input_summary: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        *,
        auto_commit: bool = True,
    ) -> None:
        """Log an audit entry for decision traceability.

        Set auto_commit=False when batching multiple audit entries,
        then call flush_audit() to commit them all at once.
        """
        ts_now = now_utc().isoformat()
        await self.conn.execute(
            "INSERT INTO audit_log (timestamp_ist, action_type, skill_name, "
            "input_summary, output_summary, duration_ms) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ts_now,
                action_type,
                skill_name,
                json.dumps(input_summary) if input_summary else None,
                json.dumps(output_summary) if output_summary else None,
                duration_ms,
            ),
        )
        if auto_commit:
            await self.conn.commit()

    async def flush_audit(self) -> None:
        """Commit any pending audit log entries."""
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Pre-market Data
    # ------------------------------------------------------------------

    async def upsert_premarket(self, data: dict[str, Any]) -> None:
        """Insert or update today's pre-market context."""
        from datetime import date

        today = date.today().isoformat()
        gift = data.get("gift_nifty", {})
        us = data.get("us_markets", {})
        llm = data.get("llm_summary")
        # Extract bias from LLM summary if it's a WebGroundingResult-like object
        bias = None
        summary_text = None
        if llm is not None and hasattr(llm, "summary"):
            summary_text = llm.summary
        elif isinstance(llm, dict):
            summary_text = llm.get("summary")
            bias = llm.get("bias")
        elif isinstance(llm, str):
            summary_text = llm

        await self.conn.execute(
            "INSERT INTO premarket (date, gift_nifty_change_pct, us_sp500_change_pct, "
            "market_bias, llm_summary, created_at) VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(date) DO UPDATE SET gift_nifty_change_pct=excluded.gift_nifty_change_pct, "
            "us_sp500_change_pct=excluded.us_sp500_change_pct, market_bias=excluded.market_bias, "
            "llm_summary=excluded.llm_summary",
            (
                today,
                gift.get("change_pct"),
                us.get("sp500_change_pct"),
                bias,
                summary_text,
            ),
        )
        await self.conn.commit()

    async def get_latest_premarket(self) -> dict[str, Any]:
        """Get the most recent pre-market context."""
        cursor = await self.conn.execute(
            "SELECT * FROM premarket ORDER BY date DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else {}

    # ------------------------------------------------------------------
    # Sentiment
    # ------------------------------------------------------------------

    async def upsert_sentiment(self, symbol: str, result: SentimentResult) -> None:
        """Insert or update sentiment for a symbol."""
        await self.conn.execute(
            "INSERT INTO sentiment (symbol, sentiment, confidence, key_drivers, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(symbol) DO UPDATE SET sentiment=excluded.sentiment, "
            "confidence=excluded.confidence, key_drivers=excluded.key_drivers, "
            "created_at=excluded.created_at",
            (
                symbol,
                result.sentiment,
                result.confidence,
                json.dumps(result.key_drivers),
            ),
        )
        await self.conn.commit()

    async def get_sentiment(
        self, symbol: str, max_age_hours: int = 48,
    ) -> SentimentResult | None:
        """Get latest sentiment for a symbol. Returns None if older than max_age_hours."""
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(hours=max_age_hours)).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT symbol, sentiment, confidence, key_drivers "
            "FROM sentiment WHERE symbol = ? AND created_at >= ?",
            (symbol, cutoff),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        drivers = json.loads(row["key_drivers"]) if row["key_drivers"] else []
        return SentimentResult(
            symbol=row["symbol"],
            sentiment=row["sentiment"],
            confidence=row["confidence"],
            key_drivers=drivers,
        )

    # ------------------------------------------------------------------
    # News Articles
    # ------------------------------------------------------------------

    async def upsert_news_articles(self, articles: list[NewsArticle]) -> int:
        """Insert news articles, skipping duplicates. Returns count inserted."""
        inserted = 0
        for article in articles:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO news_articles "
                    "(content_hash, headline, source, url, symbols, published_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        article.content_hash,
                        article.headline,
                        article.source,
                        article.url,
                        json.dumps(article.symbols),
                        article.published_at.isoformat() if article.published_at else None,
                    ),
                )
                inserted += 1
            except Exception:
                logger.debug("Skipped duplicate news article", exc_info=True)
        await self.conn.commit()
        return inserted

    async def get_news_articles(
        self,
        symbol: str | None = None,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Retrieve recent news articles with optional filters."""
        query = (
            "SELECT content_hash, headline, source, url, symbols, published_at "
            "FROM news_articles WHERE 1=1"
        )
        params: list[Any] = []
        if symbol:
            # Match the symbol as a standalone JSON-string element so
            # the filter for "ITC" doesn't also return rows tagged
            # ["BITCOIN"]. symbols is stored as `["ITC", ...]` so we
            # search for the quoted form.
            query += " AND symbols LIKE ?"
            params.append(f'%"{symbol}"%')
        if source:
            query += " AND source = ?"
            params.append(source)
        if date_from:
            # ISO 8601 strings are lexicographically sortable, so string
            # comparison with 'YYYY-MM-DD' works correctly.
            query += " AND published_at >= ?"
            params.append(date_from)
        if date_to:
            query += " AND published_at < ?"
            params.append(date_to)
        query += " ORDER BY published_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        results = []
        for r in rows:
            symbols_raw = r[4]
            try:
                symbols_parsed = json.loads(symbols_raw) if symbols_raw else []
            except (json.JSONDecodeError, TypeError):
                symbols_parsed = []
            results.append({
                "content_hash": r[0],
                "headline": r[1],
                "source": r[2],
                "url": r[3],
                "symbols": symbols_parsed,
                "published_at": r[5],
            })
        return results

    # ------------------------------------------------------------------
    # Economic Calendar
    # ------------------------------------------------------------------

    async def upsert_economic_events(self, events: list[dict[str, Any]]) -> int:
        """Insert economic calendar events, skipping duplicates. Returns count inserted."""
        inserted = 0
        for event in events:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO economic_events "
                    "(event_date, event_type, title, country, impact, source, symbol, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["event_date"],
                        event["event_type"],
                        event["title"],
                        event["country"],
                        event.get("impact", "medium"),
                        event["source"],
                        event.get("symbol"),
                        event["content_hash"],
                    ),
                )
                inserted += 1
            except Exception:
                logger.debug("Skipped duplicate economic event", exc_info=True)
        await self.conn.commit()
        return inserted

    async def get_upcoming_economic_events(
        self, days: int = 7, country: str | None = None, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get economic events within the next N days, optionally filtered."""
        from datetime import timedelta

        today = now_ist().date().isoformat()
        end = (now_ist().date() + timedelta(days=days)).isoformat()

        query = (
            "SELECT event_date, event_type, title, country, impact, source, symbol "
            "FROM economic_events WHERE event_date >= ? AND event_date <= ?"
        )
        params: list[str] = [today, end]

        if country:
            query += " AND country = ?"
            params.append(country)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        query += " ORDER BY event_date ASC"

        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_earnings_events(self, symbol: str | None = None, days: int = 30) -> list[dict[str, Any]]:
        """Get upcoming earnings/board meeting dates, optionally for a specific symbol."""
        from datetime import timedelta

        today = now_ist().date().isoformat()
        end = (now_ist().date() + timedelta(days=days)).isoformat()

        query = (
            "SELECT event_date, title, symbol, impact, source "
            "FROM economic_events WHERE event_type = 'earnings' "
            "AND event_date >= ? AND event_date <= ?"
        )
        params: list[str] = [today, end]

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        query += " ORDER BY event_date ASC"

        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    async def insert_signal(self, signal: dict[str, Any]) -> int:
        """Persist a generated signal. Caller should set `mode` to the
        active trading mode so bulk-delete and analytics can scope by it.
        attribution_json holds the top-N feature contributions surfaced
        on TradeDetailPage; None when the ML layer couldn't compute
        them (e.g. booster unreachable through calibration wrapper).

        Returns the autoincrement id of the inserted row. trade-execute
        carries this id onto the trade row so the UNIQUE index on
        trades.signal_id can enforce one-trade-per-signal at the DB
        layer (defence-in-depth against a missed in-memory dedup).
        """
        attribution = signal.get("attribution")
        attribution_json = json.dumps(attribution) if attribution else None
        cursor = await self.conn.execute(
            "INSERT INTO signals (symbol, signal_type, entry_price, target_price, "
            "stop_loss_price, position_size, confidence_score, model_version, "
            "features_snapshot, mode, attribution_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                signal["symbol"],
                signal["signal_type"],
                signal["entry_price"],
                signal["target_price"],
                signal["stop_loss_price"],
                signal["position_size"],
                signal["confidence_score"],
                signal.get("model_version", ""),
                json.dumps(signal.get("features_snapshot", {})),
                signal.get("mode", "paper"),
                attribution_json,
            ),
        )
        await self.conn.commit()
        return int(cursor.lastrowid or 0)

    async def update_signal_disposition(
        self,
        symbol: str,
        disposition: str,
        reason: str | None = None,
        position_size: int | None = None,
        mode: str | None = None,
    ) -> None:
        """Update disposition for the most recent signal for a symbol today.

        `insert_signal` stamps `created_at` via SQLite `datetime('now')`,
        i.e. UTC in space-separated form (`2026-05-13 04:18:30`). We scope
        to "today's IST trading session" by converting IST-midnight to its
        UTC instant and matching `created_at >= that`. Comparing the
        IST *calendar date* against the UTC date prefix used to silently
        no-op during the 00:00–05:30 IST window (when the IST date is a day
        ahead of UTC), leaving disposition stuck at the seeded value.

        When mode is provided, the update is scoped to rows of that
        mode so a live execution can't accidentally flip a stale paper
        signal's disposition (or vice versa).
        """
        ist_day_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Match the space-separated UTC format that datetime('now') writes
        # so the lexical string comparison is also chronological.
        day_start_utc = ist_day_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        mode_clause = " AND mode = ?" if mode else ""
        # ml_signal seeds position_size=1 as a placeholder; risk-check
        # determines the real number. Update both columns here so the
        # signals row reflects the actual planned size when this
        # function is called from the dispatch path (which has the
        # post-risk-check value handy). When the caller doesn't pass
        # position_size, leave it untouched via COALESCE.
        if position_size is not None and position_size > 0:
            params: tuple[Any, ...] = (
                disposition, reason, int(position_size), symbol, day_start_utc,
            )
            if mode:
                params = params + (mode,)
            await self.conn.execute(
                "UPDATE signals "
                "SET disposition = ?, disposition_reason = ?, "
                "    position_size = ? "
                "WHERE id = (SELECT id FROM signals WHERE symbol = ? "
                f"AND created_at >= ?{mode_clause} "
                "ORDER BY created_at DESC LIMIT 1)",
                params,
            )
        else:
            params = (disposition, reason, symbol, day_start_utc)
            if mode:
                params = params + (mode,)
            await self.conn.execute(
                "UPDATE signals SET disposition = ?, disposition_reason = ? "
                "WHERE id = (SELECT id FROM signals WHERE symbol = ? "
                f"AND created_at >= ?{mode_clause} "
                "ORDER BY created_at DESC LIMIT 1)",
                params,
            )
        await self.conn.commit()

    async def get_todays_recommendations(self) -> list[dict[str, Any]]:
        """Today's signals with disposition — what the system suggested + outcome."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT id, symbol, signal_type, entry_price, target_price, "
            "stop_loss_price, position_size, confidence_score, model_version, "
            "disposition, disposition_reason, attribution_json, created_at "
            "FROM signals WHERE created_at >= ? ORDER BY created_at DESC",
            (today_start,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_signaled_symbols(
        self, mode: str | None = None,
        risk_rejected_retry_cap: int = 5,
    ) -> set[str]:
        """Get symbols that should be skipped from new signal generation today.

        Includes:
        - Symbols with non-retryable signals today (executed,
          llm_rejected, awaiting_approval, or in-flight NULL).
        - Symbols whose retryable-disposition count (risk_rejected,
          expired) has reached risk_rejected_retry_cap — guard against
          chronically-failing setups generating a fresh row every
          heartbeat.
        - Symbols with open SYSTEM-generated positions in the current
          mode (avoid double-trading).

        Retryable dispositions (re-evaluated when count < cap):
        - risk_rejected: most risk-check reasons (exposure, drift,
          depth, correlation, cooldown) clear within the same day.
        - expired: the user didn't approve in time, but conditions may
          still favour the setup — give it another shot. Hard "no"
          should come via /reject, which routes through the separate
          rejection_cooldown_hours mechanism.
        - trade_execute_failed: the broker rejected the order or the
          skill crashed — usually a transient condition.
        - skill_error: risk-check or llm-review itself threw an
          exception. Treated as retryable so a persistent skill bug
          doesn't burn the cap on the *risk decision* side; the cap
          still protects against churn loops.

        Deferred dispositions (re-evaluated freely, cap-exempt):
        - time_blocked: signal was generated outside the order window
          (e.g. heartbeat fired between market.open and order_start).
          The underlying condition is purely time-based ("wait N
          minutes"), so consuming a retry slot would punish the
          symbol for a scheduler edge case rather than a real signal
          problem. Counts as neither "other" nor "retryable" in the
          dedup math.
        """
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()

        # Symbols where dedup applies — either has at least one
        # non-retryable signal today (count_other > 0) or the retryable
        # budget is exhausted (count_retryable >= cap). Mode-scoped so
        # paper signals don't block live and vice versa.
        retryable = (
            'risk_rejected', 'expired',
            'trade_execute_failed', 'skill_error',
        )
        deferred = ('time_blocked',)
        ignorable = retryable + deferred  # neither blocks nor counts toward cap when "other"-counting
        ignorable_ph = ", ".join(["?"] * len(ignorable))
        retryable_ph = ", ".join(["?"] * len(retryable))
        mode_clause = " AND mode = ?" if mode else ""
        sig_params: tuple[Any, ...] = (today_start,)
        if mode:
            sig_params = sig_params + (mode,)
        # Bound params order: first the ignorable set for the "other"
        # count (deferred rows must NOT count as other), then the
        # retryable set for the cap count (deferred rows must NOT
        # count toward the cap), then the cap value itself.
        sig_params = sig_params + ignorable + retryable + (int(risk_rejected_retry_cap),)
        cursor = await self.read_conn.execute(
            "SELECT symbol FROM signals "
            f"WHERE created_at >= ?{mode_clause} "
            "GROUP BY symbol "
            f"HAVING SUM(CASE WHEN disposition IN ({ignorable_ph}) THEN 0 ELSE 1 END) > 0 "
            f"   OR SUM(CASE WHEN disposition IN ({retryable_ph}) THEN 1 ELSE 0 END) >= ?",
            sig_params,
        )
        signaled = {row[0] for row in await cursor.fetchall()}

        # Symbols with open SYSTEM-generated positions only (skip adopted).
        # Filter by mode so old paper positions don't block live signal generation.
        query = (
            "SELECT DISTINCT symbol FROM trades "
            "WHERE status IN ('open', 'partially_filled') "
            "AND COALESCE(origin, 'system') = 'system'"
        )
        params: list[Any] = []
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        cursor = await self.read_conn.execute(query, params)
        positioned = {row[0] for row in await cursor.fetchall()}
        return signaled | positioned

    async def clear_todays_signals(self) -> dict[str, int]:
        """Clear today's actionable signals so the next heartbeat can
        regenerate fresh ones. Preserves:
          - `executed` rows (real trades happened — keep the audit link)
          - `rejected` rows (user explicitly said no — don't re-suggest)

        Deletes everything else: NULL (never processed), `awaiting_approval`,
        `risk_rejected`, `llm_rejected`, and `expired`. Pending trades in
        `pending` / `expired` state are also dropped so the symbols are
        free for re-evaluation.

        Cascades to predictions.signal_id since the schema has no ON
        DELETE CASCADE — without that, foreign_keys=ON would reject
        the DELETE on signals that already have a linked prediction
        (typical after a full heartbeat ran predict-track).

        Returns counts of deleted rows per table.
        """
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()

        # First null out predictions.signal_id for rows we're about to
        # delete from signals. NULLing instead of cascading because the
        # predictions table is part of the model-drift audit trail —
        # we don't want to lose the scored outcomes just because the
        # source signal was re-evaluated.
        await self.conn.execute(
            "UPDATE predictions SET signal_id = NULL "
            "WHERE signal_id IN ("
            "  SELECT id FROM signals WHERE created_at >= ? "
            "  AND (disposition IS NULL "
            "       OR disposition IN ('awaiting_approval', 'risk_rejected', "
            "                          'llm_rejected', 'expired', 'time_blocked'))"
            ")",
            (today_start,),
        )

        cursor = await self.conn.execute(
            "DELETE FROM signals "
            "WHERE created_at >= ? "
            "AND (disposition IS NULL "
            "     OR disposition IN ('awaiting_approval', 'risk_rejected', "
            "                        'llm_rejected', 'expired', 'time_blocked'))",
            (today_start,),
        )
        signals_deleted = cursor.rowcount

        cursor = await self.conn.execute(
            "DELETE FROM pending_trades WHERE status IN ('pending', 'expired')",
        )
        pending_deleted = cursor.rowcount

        await self.conn.commit()
        logger.info(
            "Cleared %d signals (today, non-terminal) and %d pending/expired trades",
            signals_deleted, pending_deleted,
        )
        return {"signals_deleted": signals_deleted, "pending_deleted": pending_deleted}

    async def get_recently_traded_symbols(
        self, lookback_days: int, mode: str | None = None,
    ) -> dict[str, str]:
        """Get symbols traded in the last N days with their most recent trade date.

        Returns {symbol: last_trade_date_iso} for symbols with trades
        in the lookback window. Filters by mode and excludes adopted holdings.
        """
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        query = (
            "SELECT symbol, MAX(created_at) as last_trade "
            "FROM trades WHERE created_at >= ? "
            "AND COALESCE(origin, 'system') = 'system'"
        )
        params: list[Any] = [cutoff]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " GROUP BY symbol"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    async def upsert_fundamentals(self, symbol: str, data: dict[str, Any]) -> None:
        """Insert or update fundamental data for a symbol."""
        await self.conn.execute(
            "INSERT INTO fundamentals (symbol, pe_ratio, pb_ratio, debt_to_equity, "
            "promoter_holding_pct, quarterly_revenue_growth_pct, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(symbol) DO UPDATE SET pe_ratio=excluded.pe_ratio, "
            "pb_ratio=excluded.pb_ratio, debt_to_equity=excluded.debt_to_equity, "
            "promoter_holding_pct=excluded.promoter_holding_pct, "
            "quarterly_revenue_growth_pct=excluded.quarterly_revenue_growth_pct, "
            "updated_at=excluded.updated_at",
            (
                symbol,
                data.get("pe_ratio"),
                data.get("pb_ratio"),
                data.get("debt_to_equity"),
                data.get("promoter_holding_pct"),
                data.get("quarterly_revenue_growth_pct"),
            ),
        )
        await self.conn.commit()

    async def get_stale_fundamentals_symbols(
        self, symbols: list[str], max_age_hours: int = 24,
    ) -> list[str]:
        """Return the subset of `symbols` that need a fundamentals refresh.

        A symbol is "stale" if it has no row in `fundamentals` yet, or its
        `updated_at` is older than `max_age_hours`. Order is preserved.

        Fundamentals only move at quarterly result announcements, so a
        24h refresh window is generous. Used by ingest-data to avoid
        hitting Screener.in / Trendlyne for symbols we already refreshed
        recently — those scrapers self-throttle at 2s/symbol and would
        otherwise blow the per-source ingest budget.
        """
        if not symbols:
            return []
        placeholders = ",".join("?" for _ in symbols)
        cutoff_expr = f"datetime('now', '-{int(max_age_hours)} hours')"
        cur = await self.conn.execute(
            f"SELECT symbol FROM fundamentals "
            f"WHERE symbol IN ({placeholders}) AND updated_at >= {cutoff_expr}",
            list(symbols),
        )
        fresh = {row[0] for row in await cur.fetchall()}
        return [s for s in symbols if s not in fresh]

    # ------------------------------------------------------------------
    # NSE Universe
    # ------------------------------------------------------------------

    async def get_nse_universe(
        self, sentiment_ttl_hours: int = 48,
    ) -> list[dict[str, Any]]:
        """Get all symbols with OHLCV data, enriched with sentiment and fundamentals.

        Only includes sentiment data that is newer than sentiment_ttl_hours.
        Stale sentiment is treated as neutral (NULL) to avoid outdated signals
        influencing the scan.
        """
        from datetime import timedelta
        sentiment_cutoff = (now_utc() - timedelta(hours=sentiment_ttl_hours)).isoformat()

        # Prefer the canonical sector from symbol_sectors (populated by
        # ingest-universe from the NSE Industry column). Fall back to
        # watchlist.sector for symbols the user added manually.
        cursor = await self.read_conn.execute(
            "SELECT o.symbol, "
            "  AVG(o.volume) as avg_daily_volume, "
            "  CASE WHEN s.created_at >= ? THEN s.sentiment ELSE NULL END as sentiment, "
            "  CASE WHEN s.created_at >= ? THEN s.confidence ELSE NULL END as sentiment_confidence, "
            "  f.pe_ratio, f.debt_to_equity, f.promoter_holding_pct, "
            "  COALESCE(ss.sector, w.sector) as sector "
            "FROM ohlcv o "
            "LEFT JOIN sentiment s ON o.symbol = s.symbol "
            "LEFT JOIN fundamentals f ON o.symbol = f.symbol "
            "LEFT JOIN watchlist w ON o.symbol = w.symbol "
            "LEFT JOIN symbol_sectors ss ON o.symbol = ss.symbol "
            "WHERE o.interval = 'daily' "
            "AND o.symbol NOT IN ("
            "  SELECT symbol FROM quarantined_symbols WHERE quarantined_at IS NOT NULL"
            ") "
            "GROUP BY o.symbol "
            "ORDER BY avg_daily_volume DESC",
            (sentiment_cutoff, sentiment_cutoff),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Model Versions
    # ------------------------------------------------------------------

    async def save_model_version(
        self, model_type: str, version: str, file_path: str, metrics: dict[str, Any]
    ) -> None:
        """Save a new model version record."""
        await self.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, "
            "sharpe_ratio, sharpe_lower, argmax_sharpe, max_drawdown_pct, "
            "win_rate, profit_factor, status, shadow_start_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'shadow', datetime('now'))",
            (
                model_type,
                version,
                file_path,
                metrics.get("sharpe") or metrics.get("sharpe_ratio"),
                metrics.get("sharpe_lower"),
                metrics.get("argmax_sharpe"),
                metrics.get("max_drawdown_pct"),
                metrics.get("win_rate"),
                metrics.get("profit_factor"),
            ),
        )
        await self.conn.commit()

    async def get_production_model(self, model_type: str) -> dict[str, Any] | None:
        """Get the current production model for a model type."""
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions "
            "WHERE model_type = ? AND status = 'production' "
            "ORDER BY created_at DESC LIMIT 1",
            (model_type,),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def promote_model(self, model_type: str, version: str) -> None:
        """Promote a shadow model to production, retire the current production.

        First UPDATE auto-begins the transaction (Python sqlite3 default
        deferred isolation), commit() ends it. Explicit BEGIN omitted —
        it conflicts when other writers are active on the same connection.
        """
        try:
            # Retire current production
            await self.conn.execute(
                "UPDATE model_versions SET status = 'retired' "
                "WHERE model_type = ? AND status = 'production'",
                (model_type,),
            )
            # Promote new model
            await self.conn.execute(
                "UPDATE model_versions SET status = 'production', "
                "promoted_date = datetime('now') "
                "WHERE model_type = ? AND version = ?",
                (model_type, version),
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise

    async def get_all_shadow_models(self) -> list[dict[str, Any]]:
        """Get all models currently in shadow status."""
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions WHERE status = 'shadow' "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_retired_models(self) -> list[dict[str, Any]]:
        """Get all retired models (previously production or rejected shadow)."""
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions WHERE status = 'retired' "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def cleanup_retired_models(self, older_than_days: int) -> int:
        """Delete retired models older than N days (DB record + .pkl file)."""
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=older_than_days)).isoformat()
        cursor = await self.conn.execute(
            "SELECT model_type, version, file_path FROM model_versions "
            "WHERE status = 'retired' AND created_at < ?",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        deleted = 0
        for row in rows:
            try:
                await self.delete_model_version(row["model_type"], row["version"])
                deleted += 1
            except Exception:
                logger.warning("Failed to delete retired model %s/%s", row.get("model_type"), row.get("version"), exc_info=True)
        return deleted

    async def reshadow_model(self, model_type: str, version: str) -> bool:
        """Move a retired model back to shadow status for re-evaluation."""
        cursor = await self.conn.execute(
            "UPDATE model_versions SET status = 'shadow', "
            "shadow_start_date = datetime('now') "
            "WHERE model_type = ? AND version = ? AND status = 'retired'",
            (model_type, version),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Training Data
    # ------------------------------------------------------------------

    async def get_training_dataset(
        self, max_days: int | None = None,
    ) -> dict[str, Any]:
        """Load OHLCV data for model training. delivery_pct is included
        as an optional per-bar column; falls back to None for older
        rows imported before migration 038.

        `max_days` caps history to fit RAM-constrained hosts. On a 2 GB
        instance the full ohlcv table (~5 years × universe) OOM-kills
        the feature-matrix builder; the default in
        retraining.max_training_days (730) keeps peak under 1 GB.
        """
        if max_days is not None and max_days > 0:
            cursor = await self.conn.execute(
                "SELECT symbol, timestamp, open, high, low, close, volume, delivery_pct "
                "FROM ohlcv WHERE interval = 'daily' "
                "  AND timestamp >= date('now', ?) "
                "ORDER BY symbol, timestamp",
                (f"-{int(max_days)} day",),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT symbol, timestamp, open, high, low, close, volume, delivery_pct "
                "FROM ohlcv WHERE interval = 'daily' ORDER BY symbol, timestamp"
            )
        rows = await cursor.fetchall()
        return {"bars": [dict[str, Any](row) for row in rows]}

    async def get_bulk_deals_timeline(self) -> list[dict[str, Any]]:
        """Return all bulk/block deals across history, ordered by date.
        Used by model_retrain to build a (symbol, date) → net-count
        index for per-sample feature lookup.
        """
        cursor = await self.read_conn.execute(
            "SELECT symbol, deal_date, buy_sell FROM bulk_deals "
            "ORDER BY deal_date"
        )
        return [
            {"symbol": r[0], "deal_date": r[1], "buy_sell": r[2]}
            for r in await cursor.fetchall()
        ]

    async def get_news_timeline(
        self, date_from: str | None = None,
    ) -> dict[str, list[tuple[str, str]]]:
        """Return all news headlines grouped by symbol since date_from.

        Each entry is (headline, published_at_iso). Used by model_retrain
        to compute per-(symbol, as_of) sentiment features without an
        N+1 query per sample — one scan, fan-out in Python.
        """
        query = (
            "SELECT headline, symbols, published_at FROM news_articles "
            "WHERE published_at IS NOT NULL"
        )
        params: list[Any] = []
        if date_from:
            query += " AND published_at >= ?"
            params.append(date_from)
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        out: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            headline, symbols_raw, published_at = r[0], r[1], r[2]
            if not symbols_raw:
                continue
            try:
                symbols = json.loads(symbols_raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for sym in symbols:
                if not isinstance(sym, str) or not sym:
                    continue
                out.setdefault(sym, []).append((headline, published_at))
        for sym in out:
            out[sym].sort(key=lambda x: x[1])
        return out

    async def upsert_fno_daily(
        self, date_str: str, aggregates: dict[str, dict[str, float]],
    ) -> int:
        """Insert today's F&O aggregates. UPSERT semantics so the skill
        can be re-run safely if the cron fires twice (idempotent).
        Returns count of rows upserted.
        """
        if not aggregates:
            return 0
        rows = [
            (
                date_str,
                symbol,
                agg.get("pcr_oi"),
                agg.get("pcr_volume"),
                agg.get("futures_oi"),
                agg.get("futures_volume"),
                agg.get("futures_close"),
            )
            for symbol, agg in aggregates.items()
        ]
        await self.conn.executemany(
            "INSERT INTO fno_daily "
            "(date, symbol, pcr_oi, pcr_volume, futures_oi, "
            "futures_volume, futures_close) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET "
            "pcr_oi=excluded.pcr_oi, pcr_volume=excluded.pcr_volume, "
            "futures_oi=excluded.futures_oi, "
            "futures_volume=excluded.futures_volume, "
            "futures_close=excluded.futures_close, "
            "created_at=datetime('now')",
            rows,
        )
        await self.conn.commit()
        return len(rows)

    async def get_fno_timeline(
        self, date_from: str | None = None,
    ) -> dict[str, list[tuple[str, dict[str, float]]]]:
        """Return F&O aggregates grouped by symbol, ascending by date.

        Used by model_retrain to bind per-(symbol, date) features without
        an N+1 query per sample — one scan, fan-out in Python. Same
        shape as get_news_timeline.
        """
        query = (
            "SELECT date, symbol, pcr_oi, pcr_volume, futures_oi, "
            "futures_volume, futures_close FROM fno_daily WHERE 1=1"
        )
        params: list[Any] = []
        if date_from:
            query += " AND date >= ?"
            params.append(date_from)
        query += " ORDER BY symbol, date"
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        out: dict[str, list[tuple[str, dict[str, float]]]] = {}
        for r in rows:
            row_dict = {
                "pcr_oi": r[2],
                "pcr_volume": r[3],
                "futures_oi": r[4],
                "futures_volume": r[5],
                "futures_close": r[6],
            }
            out.setdefault(r[1], []).append((r[0], row_dict))
        return out

    async def get_vix_timeline(
        self, date_from: str | None = None,
    ) -> list[tuple[str, float]]:
        """Return India VIX daily close history as (date_str, close), oldest first.

        Reads from ohlcv where symbol='INDIA VIX' and interval='daily'.
        Used by model_retrain to bind per-sample VIX regime features and
        by ingest-vix's cold-start guard to decide whether to backfill.
        """
        query = (
            "SELECT timestamp, close FROM ohlcv "
            "WHERE symbol = 'INDIA VIX' AND interval = 'daily'"
        )
        params: list[Any] = []
        if date_from:
            query += " AND timestamp >= ?"
            params.append(date_from)
        query += " ORDER BY timestamp"
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        out: list[tuple[str, float]] = []
        for r in rows:
            ts_raw = r[0]
            close = r[1]
            if close is None:
                continue
            # ohlcv.timestamp is ISO datetime; the date portion is what
            # we join against in model_retrain. Strip cheaply.
            date_str = ts_raw.split("T")[0] if "T" in ts_raw else ts_raw[:10]
            out.append((date_str, float(close)))
        return out

    async def get_prediction_outcomes(self) -> list[dict[str, Any]]:
        """Load predictions with actual outcomes for retraining analysis."""
        cursor = await self.conn.execute(
            "SELECT p.*, COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            "COALESCE(s.signal_type, t.signal_type) as signal_type, "
            "s.confidence_score "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
            "LEFT JOIN trades t ON p.trade_id = t.trade_id "
            "WHERE p.actual_price IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_model_drift_stats(
        self, days: int = 30, mode: str | None = None,
    ) -> dict[str, Any]:
        """Compare predicted vs realised win rate to detect model drift.

        For every model_version that has scored predictions in the window,
        groups by parent model_type (joined from model_versions) and emits:
          - by_day: predicted_win_rate (mean confidence_score) vs
            realised_win_rate (sum direction_correct / count) for each
            scored_at day
          - calibration_buckets: confidence buckets [0.5-0.6, 0.6-0.7, ...]
            with predicted_mean vs realised_rate + sample size

        A top-level warning string flags a realised win-rate drop of more
        than 15 percentage points in the last 7 days vs the prior 7 days
        (per model_type).
        """
        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        mc = " AND p.mode = ?" if mode else ""
        mp: list[Any] = [mode] if mode else []

        # Pull all scored predictions in the window joined with the model
        # registry to get model_type. predictions.model_version may be on
        # either the predictions row or the signals row depending on age;
        # COALESCE picks whichever is set.
        cursor = await self.read_conn.execute(
            f"SELECT COALESCE(p.model_version, s.model_version) AS version, "
            f"  mv.model_type AS model_type, "
            f"  mv.status AS model_status, "
            f"  substr(p.scored_at, 1, 10) AS day, "
            f"  s.confidence_score AS confidence, "
            f"  p.direction_correct AS correct "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN model_versions mv "
            f"  ON mv.version = COALESCE(p.model_version, s.model_version) "
            f"WHERE p.actual_price IS NOT NULL "
            f"  AND p.scored_at IS NOT NULL "
            f"  AND p.scored_at >= ? "
            f"  AND p.direction_correct IS NOT NULL{mc}",
            [cutoff, *mp],
        )
        rows = await cursor.fetchall()

        # Group by model_type → version → (day_rows, all_rows)
        by_type: dict[str, dict[str, Any]] = {}
        for r in rows:
            mt = r["model_type"]
            if not mt:
                # Unmapped version (e.g. retired model purged from
                # model_versions). Skip — we can't classify it.
                continue
            entry = by_type.setdefault(mt, {
                "model_type": mt,
                "version": r["version"],
                "is_production": (r["model_status"] == "production"),
                "_days": {},
                "_all": [],
            })
            # Always keep the most recent production version as the
            # representative version for the model_type.
            if r["model_status"] == "production":
                entry["version"] = r["version"]
                entry["is_production"] = True
            day = r["day"]
            if not day:
                continue
            day_bucket = entry["_days"].setdefault(
                day, {"conf_sum": 0.0, "conf_n": 0, "correct": 0, "n": 0},
            )
            conf = r["confidence"]
            if conf is not None:
                day_bucket["conf_sum"] += float(conf)
                day_bucket["conf_n"] += 1
            day_bucket["correct"] += int(r["correct"] or 0)
            day_bucket["n"] += 1
            entry["_all"].append({
                "confidence": float(conf) if conf is not None else None,
                "correct": int(r["correct"] or 0),
                "day": day,
            })

        bucket_edges = [
            (0.5, 0.6, "0.50-0.60"),
            (0.6, 0.7, "0.60-0.70"),
            (0.7, 0.8, "0.70-0.80"),
            (0.8, 0.9, "0.80-0.90"),
            (0.9, 1.0001, "0.90-1.00"),
        ]

        warnings: list[str] = []
        model_versions: list[dict[str, Any]] = []
        for mt, entry in by_type.items():
            # Build by_day list sorted ascending.
            by_day = []
            for day in sorted(entry["_days"].keys()):
                d = entry["_days"][day]
                predicted = (
                    d["conf_sum"] / d["conf_n"] if d["conf_n"] > 0 else None
                )
                realised = d["correct"] / d["n"] if d["n"] > 0 else 0.0
                by_day.append({
                    "date": day,
                    "predicted_win_rate": (
                        round(predicted, 4) if predicted is not None else None
                    ),
                    "realised_win_rate": round(realised, 4),
                    "sample_size": d["n"],
                })

            # Calibration buckets over the full window.
            calibration_buckets = []
            for lo, hi, label in bucket_edges:
                items = [
                    a for a in entry["_all"]
                    if a["confidence"] is not None
                    and lo <= a["confidence"] < hi
                ]
                if not items:
                    calibration_buckets.append({
                        "bucket": label,
                        "predicted_mean": None,
                        "realised_rate": None,
                        "samples": 0,
                    })
                    continue
                pred_mean = sum(i["confidence"] for i in items) / len(items)
                real_rate = sum(i["correct"] for i in items) / len(items)
                calibration_buckets.append({
                    "bucket": label,
                    "predicted_mean": round(pred_mean, 4),
                    "realised_rate": round(real_rate, 4),
                    "samples": len(items),
                })

            # Drift detection: realised win-rate last 7d vs prior 7d.
            now_d = now_utc().date()
            recent_correct = recent_n = prior_correct = prior_n = 0
            for a in entry["_all"]:
                try:
                    d = datetime.fromisoformat(a["day"]).date()
                except (TypeError, ValueError):
                    continue
                age = (now_d - d).days
                if 0 <= age < 7:
                    recent_correct += a["correct"]
                    recent_n += 1
                elif 7 <= age < 14:
                    prior_correct += a["correct"]
                    prior_n += 1
            if recent_n >= 5 and prior_n >= 5:
                recent_rate = recent_correct / recent_n
                prior_rate = prior_correct / prior_n
                drop = prior_rate - recent_rate
                if drop > 0.15:
                    warnings.append(
                        f"{mt} model realised win-rate dropped "
                        f"{int(round(drop * 100))}% in last 7 days "
                        f"({int(round(prior_rate * 100))}% -> "
                        f"{int(round(recent_rate * 100))}%)"
                    )

            model_versions.append({
                "model_type": mt,
                "version": entry["version"],
                "is_production": entry["is_production"],
                "by_day": by_day,
                "calibration_buckets": calibration_buckets,
            })

        # Stable ordering: intraday first, then swing, then anything else.
        order = {"intraday": 0, "swing": 1}
        model_versions.sort(key=lambda m: (order.get(m["model_type"], 99), m["model_type"]))

        return {
            "model_versions": model_versions,
            "warning": "; ".join(warnings) if warnings else None,
        }

    async def get_fii_dii_timeline(self, days: int = 30) -> list[dict[str, Any]]:
        """Return the last `days` rows of FII/DII flows ordered by
        date ascending (so the dashboard line chart can plot directly).
        Values are in ₹ crore as published by NSE.
        """
        cursor = await self.read_conn.execute(
            "SELECT date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net "
            "FROM fii_dii_daily "
            "WHERE date >= date('now', ?) "
            "ORDER BY date",
            (f"-{int(days)} day",),
        )
        return [
            {
                "date": r[0],
                "fii_buy": float(r[1] or 0),
                "fii_sell": float(r[2] or 0),
                "fii_net": float(r[3] or 0),
                "dii_buy": float(r[4] or 0),
                "dii_sell": float(r[5] or 0),
                "dii_net": float(r[6] or 0),
            }
            for r in await cursor.fetchall()
        ]

    async def get_bulk_deals_list(
        self, days: int = 30, symbol: str | None = None, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Recent bulk/block deals for the dashboard table. Filtered
        to the last `days` calendar days and optionally to a single
        symbol. Ordered most-recent-first.
        """
        params: list[Any] = [f"-{int(days)} day"]
        sym_clause = ""
        if symbol:
            sym_clause = " AND symbol = ?"
            params.append(symbol)
        params.append(int(limit))
        cursor = await self.read_conn.execute(
            "SELECT deal_date, symbol, deal_type, client_name, buy_sell, "
            "       quantity, trade_price "
            "FROM bulk_deals "
            "WHERE deal_date >= date('now', ?)" + sym_clause +
            " ORDER BY deal_date DESC, symbol "
            "LIMIT ?",
            params,
        )
        return [
            {
                "deal_date": r[0],
                "symbol": r[1],
                "deal_type": r[2],
                "client_name": r[3],
                "buy_sell": r[4],
                "quantity": r[5],
                "trade_price": r[6],
            }
            for r in await cursor.fetchall()
        ]

    async def get_fii_dii_timeline_summary(
        self, days: int = 30,
    ) -> dict[str, Any]:
        """Quick aggregates for the dashboard header pills."""
        timeline = await self.get_fii_dii_timeline(days)
        if not timeline:
            return {
                "days_covered": 0,
                "fii_net_total": 0.0,
                "dii_net_total": 0.0,
                "fii_net_today": None,
                "dii_net_today": None,
            }
        last = timeline[-1]
        return {
            "days_covered": len(timeline),
            "fii_net_total": round(sum(r["fii_net"] for r in timeline), 2),
            "dii_net_total": round(sum(r["dii_net"] for r in timeline), 2),
            "fii_net_today": last["fii_net"],
            "dii_net_today": last["dii_net"],
        }

    async def upsert_bulk_deals(
        self, deals: list[dict[str, Any]], deal_date: str | None = None,
    ) -> int:
        """Persist bulk/block deals. Each row's date comes from its own
        `deal_date` field when present (the consolidated NSE largedeal
        endpoint returns deals from multiple past sessions); the caller-
        supplied `deal_date` arg is a fallback when the payload omits
        it, and that fallback defaults to today (IST).

        Returns the number of new rows inserted (duplicates ignored via
        unique constraint).

        Skips rows where every payload field beyond symbol is empty.
        Without this guard, a schema change at NSE that renames the
        client_name / buy_sell / quantity / trade_price keys would
        produce one symbol-only row per heartbeat, with the unique
        constraint not catching the dupes (SQLite treats NULL ≠ NULL).
        """
        if not deals:
            return 0
        fallback_date = deal_date or now_ist().strftime("%Y-%m-%d")
        before = (await (await self.conn.execute(
            "SELECT COUNT(*) FROM bulk_deals",
        )).fetchone())[0]
        skipped_empty = 0
        for d in deals:
            sym = str(d.get("symbol") or "").strip()
            if not sym:
                continue
            client_raw = d.get("client_name")
            bs_raw = d.get("buy_sell")
            qty_raw = d.get("quantity")
            price_raw = d.get("trade_price")
            client = str(client_raw or "").strip()
            bs = str(bs_raw or "").strip()
            # Always store numeric values — NEVER NULL — so the
            # UNIQUE(deal_date, symbol, client_name, buy_sell,
            # quantity, trade_price) constraint actually dedupes
            # (SQLite treats NULL != NULL in UNIQUE, so rows with
            # a missing trade_price would otherwise accumulate
            # one new copy per heartbeat on NSE responses that
            # omit the price field).
            try:
                qty_val = int(float(str(qty_raw).replace(",", ""))) if qty_raw else 0
            except (TypeError, ValueError):
                qty_val = 0
            try:
                price_val = (
                    float(str(price_raw).replace(",", "")) if price_raw else 0.0
                )
            except (TypeError, ValueError):
                price_val = 0.0
            # Reject rows where everything beyond symbol is empty —
            # a NSE schema mismatch dropping payload keys would
            # otherwise persist one symbol-only stub row per deal.
            if not client and not bs and qty_val == 0 and price_val == 0.0:
                skipped_empty += 1
                continue
            # Per-deal date when NSE gave us one — else fall back.
            row_date = _normalize_iso_date(d.get("deal_date")) or fallback_date
            await self.conn.execute(
                "INSERT OR IGNORE INTO bulk_deals "
                "(deal_date, symbol, deal_type, client_name, buy_sell, "
                " quantity, trade_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row_date, sym,
                    str(d.get("deal_type") or "bulk"),
                    client, bs, qty_val, price_val,
                ),
            )
        await self.conn.commit()
        if skipped_empty:
            logger.warning(
                "upsert_bulk_deals: skipped %d symbol-only rows (likely "
                "NSE schema mismatch — only deal_type/symbol present)",
                skipped_empty,
            )
        after = (await (await self.conn.execute(
            "SELECT COUNT(*) FROM bulk_deals",
        )).fetchone())[0]
        return after - before

    async def upsert_fii_dii(self, data: dict[str, Any]) -> bool:
        """Persist FII/DII net flows for the day. `data` shape matches
        NSEOfficialSource.fetch_fii_dii output: {date, fii: {...}, dii: {...}}.
        Returns True when a row was written.

        The `date` value from NSE arrives in display format (e.g.
        "15-May-2026"). Normalise to ISO `YYYY-MM-DD` before storing so
        `WHERE date >= date('now', '-30 day')` lookups and `ORDER BY
        date DESC` work — string-compared, "15-May-2026" sorts before
        "2026-04-16" and the institutional-flows dashboard reads
        empty. Other date formats NSE has used historically
        ("15-05-2026", "15/05/2026", "2026-05-15") are also accepted.
        """
        if not data or not data.get("date"):
            return False
        raw_date = str(data["date"]).strip()
        iso_date: str | None = None
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y %H:%M"):
            try:
                iso_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if iso_date is None:
            logger.warning(
                "upsert_fii_dii: skipping row with unparseable date %r", raw_date,
            )
            return False
        fii = data.get("fii") or {}
        dii = data.get("dii") or {}
        # Default missing values to 0.0 — INSERT OR REPLACE so the latest
        # snapshot of the day wins (NSE refreshes mid-day).
        await self.conn.execute(
            "INSERT OR REPLACE INTO fii_dii_daily "
            "(date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                iso_date,
                float(fii.get("buy_value") or 0.0),
                float(fii.get("sell_value") or 0.0),
                float(fii.get("net_value") or 0.0),
                float(dii.get("buy_value") or 0.0),
                float(dii.get("sell_value") or 0.0),
                float(dii.get("net_value") or 0.0),
            ),
        )
        await self.conn.commit()
        return True

    async def count_recent_bulk_deals(
        self, symbol: str, lookback_days: int = 5,
    ) -> dict[str, int]:
        """Return {buy_count, sell_count} of bulk/block deal entries on
        `symbol` within the last `lookback_days` calendar days. Used as
        a live risk-check signal and as an ML feature.
        """
        cursor = await self.read_conn.execute(
            "SELECT buy_sell, COUNT(*) FROM bulk_deals "
            "WHERE symbol = ? AND deal_date >= date('now', ?) "
            "GROUP BY buy_sell",
            (symbol, f"-{int(lookback_days)} day"),
        )
        out = {"buy_count": 0, "sell_count": 0}
        for buy_sell, cnt in await cursor.fetchall():
            if str(buy_sell).upper() == "BUY":
                out["buy_count"] = int(cnt)
            elif str(buy_sell).upper() == "SELL":
                out["sell_count"] = int(cnt)
        return out

    async def get_latest_fii_dii(self) -> dict[str, float] | None:
        """Return the most recent FII/DII row, or None if the table is
        empty. Used by risk_check to gate signals when foreigners are
        net sellers.
        """
        cursor = await self.read_conn.execute(
            "SELECT date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net "
            "FROM fii_dii_daily ORDER BY date DESC LIMIT 1",
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "date": row[0],
            "fii_buy": float(row[1] or 0),
            "fii_sell": float(row[2] or 0),
            "fii_net": float(row[3] or 0),
            "dii_buy": float(row[4] or 0),
            "dii_sell": float(row[5] or 0),
            "dii_net": float(row[6] or 0),
        }

    async def get_symbol_sectors_map(
        self, symbols: list[str] | None = None,
    ) -> dict[str, str]:
        """Return a {symbol: sector} mapping. Prefers symbol_sectors
        (canonical NSE Industry) and falls back to watchlist.sector
        for user-added symbols. Symbols with no sector are omitted.
        """
        if symbols is not None:
            if not symbols:
                return {}
            placeholders = ", ".join(["?"] * len(symbols))
            cursor = await self.read_conn.execute(
                f"SELECT symbol, COALESCE(sector, '') FROM symbol_sectors "
                f"WHERE symbol IN ({placeholders})",
                symbols,
            )
        else:
            cursor = await self.read_conn.execute(
                "SELECT symbol, COALESCE(sector, '') FROM symbol_sectors"
            )
        out: dict[str, str] = {}
        for sym, sector in await cursor.fetchall():
            if sector:
                out[sym] = sector
        # Watchlist fallback for any symbols missing from symbol_sectors.
        if symbols is not None:
            missing = [s for s in symbols if s not in out]
            if missing:
                placeholders = ", ".join(["?"] * len(missing))
                cursor = await self.read_conn.execute(
                    f"SELECT symbol, COALESCE(sector, '') FROM watchlist "
                    f"WHERE symbol IN ({placeholders})",
                    missing,
                )
                for sym, sector in await cursor.fetchall():
                    if sector:
                        out[sym] = sector
        else:
            cursor = await self.read_conn.execute(
                "SELECT symbol, COALESCE(sector, '') FROM watchlist"
            )
            for sym, sector in await cursor.fetchall():
                if sector and sym not in out:
                    out[sym] = sector
        return out

    async def compute_live_regime(self) -> dict[str, float]:
        """Cross-sectional regime stats over the latest two daily closes
        of every tracked symbol. Cheap proxy for "is the broad market
        trending or chopping right now". Heartbeats during market
        hours have today's developing daily bar (Kite returns close =
        current LTP), so the comparison is "today vs yesterday".

        Returns: {"breadth": 0..1, "avg_return": float, "sample_size": int}.
        breadth = fraction of symbols up vs yesterday. avg_return =
        mean per-symbol % change. sample_size = number of symbols
        that had two consecutive daily bars available.

        Empty / single-symbol result: returns neutral {0.5, 0.0, 0}.
        """
        cursor = await self.read_conn.execute(
            """
            WITH ranked AS (
                SELECT symbol, close,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY timestamp DESC
                       ) AS rn
                FROM ohlcv
                WHERE interval = 'daily'
                  AND timestamp >= date('now', '-10 day')
            )
            SELECT
                MAX(CASE WHEN rn = 1 THEN close END) AS latest,
                MAX(CASE WHEN rn = 2 THEN close END) AS prev
            FROM ranked
            WHERE rn <= 2
            GROUP BY symbol
            HAVING latest > 0 AND prev > 0
            """
        )
        rows = await cursor.fetchall()
        if not rows:
            return {"breadth": 0.5, "avg_return": 0.0, "sample_size": 0}
        returns = [(float(r[0]) - float(r[1])) / float(r[1]) for r in rows]
        up = sum(1 for x in returns if x > 0)
        return {
            "breadth": up / len(returns),
            "avg_return": sum(returns) / len(returns),
            "sample_size": len(returns),
        }

    async def compute_live_sector_regime(
        self,
    ) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
        """Live per-sector breadth/avg-return plus per-symbol daily return,
        for the inference-time `sector_breadth` / `sector_avg_return` /
        `relative_momentum` features (training computes the same via
        `_compute_sector_index`). Uses the latest two daily closes of every
        tracked symbol, grouped by sector via the symbol_sectors map.

        Returns (sector_stats, symbol_returns) where sector_stats[sector] =
        {"breadth", "avg_return", "n"} and symbol_returns[symbol] = pct
        change. A sector needs >= 3 peers to get stats (mirrors training's
        min-peer guard); thinner sectors are simply absent.
        """
        cursor = await self.read_conn.execute(
            """
            WITH ranked AS (
                SELECT symbol, close,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY timestamp DESC
                       ) AS rn
                FROM ohlcv
                WHERE interval = 'daily'
                  AND timestamp >= date('now', '-10 day')
            )
            SELECT symbol,
                   MAX(CASE WHEN rn = 1 THEN close END) AS latest,
                   MAX(CASE WHEN rn = 2 THEN close END) AS prev
            FROM ranked
            WHERE rn <= 2
            GROUP BY symbol
            HAVING latest > 0 AND prev > 0
            """
        )
        rows = await cursor.fetchall()
        symbol_returns: dict[str, float] = {}
        for sym, latest, prev in rows:
            try:
                symbol_returns[sym] = (float(latest) - float(prev)) / float(prev)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if not symbol_returns:
            return {}, {}
        sector_map = await self.get_symbol_sectors_map(list(symbol_returns.keys()))
        by_sector: dict[str, list[float]] = {}
        for sym, ret in symbol_returns.items():
            sec = sector_map.get(sym)
            if sec:
                by_sector.setdefault(sec, []).append(ret)
        sector_stats: dict[str, dict[str, float]] = {}
        for sec, rets in by_sector.items():
            if len(rets) < 3:
                continue
            up = sum(1 for x in rets if x > 0)
            sector_stats[sec] = {
                "breadth": up / len(rets),
                "avg_return": sum(rets) / len(rets),
                "n": len(rets),
            }
        return sector_stats, symbol_returns

    async def compute_market_trend(self, ma_window: int = 50) -> dict[str, Any]:
        """Equal-weight market-index trend vs its moving average — the
        long-only circuit-breaker signal. Builds an index from the mean
        daily return across all tracked symbols, then reports whether the
        latest index level is at/above its trailing `ma_window`-day MA.

        Returns: {"in_uptrend": bool, "index_level": float, "ma": float,
                  "sample_size": int, "ma_window": int}. Neutral
        (in_uptrend=True, sample_size=0) when there isn't enough history —
        fail-open so a cold cache never blocks trading.
        """
        lookback = ma_window + 10
        cursor = await self.read_conn.execute(
            "SELECT symbol, timestamp, close FROM ohlcv "
            "WHERE interval = 'daily' AND timestamp >= date('now', ?) "
            "ORDER BY symbol, timestamp",
            (f"-{int(lookback)} day",),
        )
        rows = await cursor.fetchall()
        neutral = {
            "in_uptrend": True, "index_level": 1.0, "ma": 1.0,
            "sample_size": 0, "ma_window": ma_window,
        }
        if not rows:
            return neutral
        by_symbol: dict[str, list[tuple[str, float]]] = {}
        for sym, ts, close in rows:
            try:
                c = float(close)
            except (TypeError, ValueError):
                continue
            by_symbol.setdefault(sym, []).append((str(ts)[:10], c))
        ret_sum: dict[str, float] = {}
        ret_cnt: dict[str, int] = {}
        for series in by_symbol.values():
            series.sort()
            for i in range(1, len(series)):
                pc = series[i - 1][1]
                if pc > 0:
                    d = series[i][0]
                    ret_sum[d] = ret_sum.get(d, 0.0) + (series[i][1] / pc - 1)
                    ret_cnt[d] = ret_cnt.get(d, 0) + 1
        dates = sorted(ret_cnt)
        if len(dates) < 2:
            return neutral
        level = 1.0
        levels: list[float] = []
        for d in dates:
            level *= (1 + ret_sum[d] / ret_cnt[d])
            levels.append(level)
        window = levels[-ma_window:] if len(levels) >= ma_window else levels
        ma = sum(window) / len(window)
        latest = levels[-1]
        return {
            "in_uptrend": latest >= ma,
            "index_level": latest,
            "ma": ma,
            "sample_size": ret_cnt[dates[-1]],
            "ma_window": ma_window,
        }

    async def minutes_since_last_loss_for_symbol(
        self, symbol: str, mode: str | None = None,
    ) -> float:
        """Minutes since the most recent losing trade closed for this
        symbol+mode. Returns a large sentinel (999999) when the symbol
        has never recorded a loss in the current mode.
        """
        mode_clause = " AND mode = ?" if mode else ""
        params: tuple[Any, ...] = (
            (symbol, mode) if mode else (symbol,)
        )
        cursor = await self.read_conn.execute(
            f"SELECT closed_at FROM trades "
            f"WHERE symbol = ? AND pnl IS NOT NULL AND pnl < 0{mode_clause} "
            f"ORDER BY closed_at DESC LIMIT 1",
            params,
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return 999999.0
        last_loss_time = datetime.fromisoformat(row[0])
        if last_loss_time.tzinfo is None:
            last_loss_time = last_loss_time.replace(tzinfo=IST)
        now = datetime.now(IST)
        return (now - last_loss_time).total_seconds() / 60

    async def get_feedback_data(self, lookback_days: int = 14) -> dict[str, dict[str, float]]:
        """Get per-symbol feedback stats from recent predictions, dry runs, and trades.

        Returns a dict keyed by symbol with rolling accuracy, PnL, slippage stats.
        Used to augment ML training features and compute sample weights.
        """
        from datetime import timedelta

        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        feedback: dict[str, dict[str, float]] = {}

        def _ensure(sym: str) -> dict[str, float]:
            if sym not in feedback:
                feedback[sym] = {}
            return feedback[sym]

        # 1. Prediction outcomes
        try:
            cursor = await self.read_conn.execute(
                "SELECT COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
                "p.direction_correct, p.target_hit, p.actual_pnl_pct "
                "FROM predictions p "
                "LEFT JOIN signals s ON p.signal_id = s.id "
                "LEFT JOIN trades t ON p.trade_id = t.trade_id "
                "WHERE p.actual_price IS NOT NULL AND p.scored_at >= ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            sym_preds: dict[str, list[dict]] = {}
            for r in rows:
                sym = r[0]
                if not sym:
                    continue
                sym_preds.setdefault(sym, []).append({
                    "correct": r[1], "target_hit": r[2], "pnl_pct": r[3],
                })
            for sym, preds in sym_preds.items():
                fb = _ensure(sym)
                n = len(preds)
                fb["pred_count"] = float(n)
                fb["pred_accuracy"] = sum(1 for p in preds if p["correct"]) / n
                fb["pred_target_hit_rate"] = sum(1 for p in preds if p["target_hit"]) / n
                pnls = [p["pnl_pct"] for p in preds if p["pnl_pct"] is not None]
                fb["pred_avg_pnl_pct"] = sum(pnls) / len(pnls) if pnls else 0.0
        except Exception as e:
            logger.warning("Feedback: prediction query failed: %s", e)

        # 2. Dry run scores
        try:
            cursor = await self.read_conn.execute(
                "SELECT symbol, direction_correct, target_hit, actual_move_pct "
                "FROM dry_run_results "
                "WHERE scored_at IS NOT NULL AND scored_at >= ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            sym_dr: dict[str, list[dict]] = {}
            for r in rows:
                sym = r[0]
                if not sym:
                    continue
                sym_dr.setdefault(sym, []).append({
                    "correct": r[1], "target_hit": r[2], "move_pct": r[3],
                })
            for sym, drs in sym_dr.items():
                fb = _ensure(sym)
                n = len(drs)
                fb["dry_run_count"] = float(n)
                fb["dry_run_accuracy"] = sum(1 for d in drs if d["correct"]) / n
                moves = [d["move_pct"] for d in drs if d["move_pct"] is not None]
                fb["dry_run_avg_move_pct"] = sum(moves) / len(moves) if moves else 0.0
        except Exception as e:
            logger.warning("Feedback: dry run query failed: %s", e)

        # 3. Closed trades
        try:
            cursor = await self.read_conn.execute(
                "SELECT symbol, pnl, slippage, entry_price, fill_price "
                "FROM trades "
                "WHERE closed_at IS NOT NULL AND closed_at >= ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            sym_trades: dict[str, list[dict]] = {}
            for r in rows:
                sym = r[0]
                if not sym:
                    continue
                entry = r[3] or 1
                sym_trades.setdefault(sym, []).append({
                    "pnl": r[1], "slippage_pct": abs(r[2] or 0) / entry * 100,
                })
            for sym, trades in sym_trades.items():
                fb = _ensure(sym)
                n = len(trades)
                fb["trade_count"] = float(n)
                fb["trade_win_rate"] = sum(1 for t in trades if (t["pnl"] or 0) > 0) / n
                fb["trade_loss_count"] = float(
                    sum(1 for t in trades if (t["pnl"] or 0) < 0),
                )
                pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
                fb["trade_avg_pnl"] = sum(pnls) / len(pnls) if pnls else 0.0
                slips = [t["slippage_pct"] for t in trades]
                fb["trade_avg_slippage_pct"] = sum(slips) / len(slips) if slips else 0.0
        except Exception as e:
            logger.warning("Feedback: trades query failed: %s", e)

        return feedback

    # ------------------------------------------------------------------
    # Failure Analysis
    # ------------------------------------------------------------------

    async def store_failure_analysis(self, analysis: object) -> None:
        """Persist LLM failure analysis."""
        patterns = []
        recommendations = []
        summary = ""
        if hasattr(analysis, "patterns_identified"):
            patterns = analysis.patterns_identified
        if hasattr(analysis, "recommendations"):
            recommendations = analysis.recommendations
        if hasattr(analysis, "summary"):
            summary = analysis.summary

        await self.conn.execute(
            "INSERT INTO failure_analyses (patterns, recommendations, summary) "
            "VALUES (?, ?, ?)",
            (json.dumps(patterns), json.dumps(recommendations), summary),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Portfolio State
    # ------------------------------------------------------------------

    async def get_portfolio_state(
        self, weekly_reset_day: str = "monday", mode: str | None = None,
    ) -> dict[str, Any]:
        """Build portfolio state dict[str, Any] for risk checks.

        Computes total capital, exposure, per-stock/sector counts,
        daily/weekly PnL, trades today, and time since last loss.

        Args:
            weekly_reset_day: Day name when weekly PnL resets (e.g. "monday").
            mode: Filter by trading mode ('paper' or 'live'). None = all modes.
        """
        from datetime import timedelta

        now = now_ist()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat()

        mode_clause = " AND mode = ?" if mode else ""
        mode_params: list[Any] = [mode] if mode else []

        # Get initial capital from system_state or fallback
        initial_capital = 100_000.0
        cap_row = await self.get_system_state("initial_capital")
        if cap_row:
            try:
                initial_capital = float(cap_row)
            except (ValueError, TypeError):
                pass

        # Capital breakdown (broker-synced cash/utilised/holdings).
        # Falls back to zeros if no broker sync has happened yet.
        breakdown = {
            "available_cash": 0.0,
            "utilised_margin": 0.0,
            "holdings_invested": 0.0,
            "holdings_current": 0.0,
            "total": 0.0,
        }
        bd_raw = await self.get_system_state("capital_breakdown")
        if bd_raw:
            try:
                import json as _json
                parsed = _json.loads(bd_raw)
                if isinstance(parsed, dict):
                    breakdown.update({k: float(parsed.get(k, 0.0)) for k in breakdown})
            except (ValueError, TypeError):
                pass

        # Open positions
        positions = await self.get_open_positions(mode=mode)
        open_count = len(positions)

        # Stock exposures and sector counts
        stock_exposures: dict[str, float] = {}
        sector_counts: dict[str, int] = {}
        system_position_value = 0.0  # positions created by the trading system
        adopted_position_value = 0.0  # positions imported from broker holdings
        system_position_count = 0
        adopted_position_count = 0

        for pos in positions:
            symbol = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            entry = pos.get("entry_price", 0)
            value = qty * entry

            if pos.get("origin") == "adopted":
                adopted_position_value += value
                adopted_position_count += 1
            else:
                system_position_value += value
                system_position_count += 1

            sector = pos.get("sector") or await self.get_stock_sector(symbol)
            if sector:
                sector_counts[sector] = sector_counts.get(sector, 0) + 1

        total_position_value = system_position_value + adopted_position_value

        # total_capital = initial + all realized PnL
        cursor = await self.conn.execute(
            f"SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE pnl IS NOT NULL{mode_clause}",
            mode_params,
        )
        row = await cursor.fetchone()
        all_time_pnl = row[0] if row else 0
        total_capital = initial_capital + all_time_pnl

        # All-time charges from realized_costs_json — used to display gross
        # PnL alongside net. Rows without the breakdown (legacy or never
        # captured) contribute 0, so gross collapses to net in those cases.
        cursor = await self.conn.execute(
            f"SELECT COALESCE(SUM(CAST(json_extract(realized_costs_json, '$.total') AS REAL)), 0) "
            f"FROM trades WHERE pnl IS NOT NULL "
            f"AND realized_costs_json IS NOT NULL{mode_clause}",
            mode_params,
        )
        row = await cursor.fetchone()
        all_time_charges = row[0] if row else 0

        if total_capital > 0:
            for pos in positions:
                symbol = pos.get("symbol", "")
                qty = pos.get("quantity", 0)
                entry = pos.get("entry_price", 0)
                stock_exposures[symbol] = (qty * entry) / total_capital

        # Available cash: only deduct system-traded positions, not adopted holdings
        # (adopted holdings represent money already invested outside the system)
        # Exposure = system trades / (available capital for system trading)
        system_capital = total_capital - adopted_position_value
        exposure_pct = system_position_value / system_capital if system_capital > 0 else 0
        available_cash = system_capital - system_position_value

        # Today's trades count — overall + per product so risk-check
        # can enforce per-product caps (max_mis_trades_per_day /
        # max_cnc_trades_per_day) independently of the combined cap.
        cursor = await self.conn.execute(
            f"SELECT COUNT(*), "
            f"SUM(CASE WHEN UPPER(product) = 'MIS' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN UPPER(product) = 'CNC' THEN 1 ELSE 0 END) "
            f"FROM trades WHERE created_at >= ?{mode_clause}",
            [today_start, *mode_params],
        )
        row = await cursor.fetchone()
        trades_today = (row[0] or 0) if row else 0
        mis_trades_today = (row[1] or 0) if row else 0
        cnc_trades_today = (row[2] or 0) if row else 0

        # Daily realized PnL
        cursor = await self.conn.execute(
            f"SELECT COALESCE(SUM(pnl), 0) FROM trades "
            f"WHERE closed_at >= ? AND pnl IS NOT NULL{mode_clause}",
            [today_start, *mode_params],
        )
        row = await cursor.fetchone()
        daily_pnl = row[0] if row else 0
        daily_pnl_pct = daily_pnl / total_capital if total_capital > 0 else 0

        # Daily charges (sum of realized_costs_json.total) — lets the dashboard
        # show gross alongside net. Trades closed before this column was added,
        # or with the field unset, contribute 0; in that case net == gross.
        cursor = await self.conn.execute(
            f"SELECT COALESCE(SUM(CAST(json_extract(realized_costs_json, '$.total') AS REAL)), 0) "
            f"FROM trades WHERE closed_at >= ? AND pnl IS NOT NULL "
            f"AND realized_costs_json IS NOT NULL{mode_clause}",
            [today_start, *mode_params],
        )
        row = await cursor.fetchone()
        daily_charges = row[0] if row else 0

        # Weekly realized PnL (since configured reset day at market open)
        _day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        reset_weekday = _day_map.get(weekly_reset_day.lower(), 0)
        days_since_reset = (now.weekday() - reset_weekday) % 7
        week_start = (now - timedelta(days=days_since_reset)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        cursor = await self.conn.execute(
            f"SELECT COALESCE(SUM(pnl), 0) FROM trades "
            f"WHERE closed_at >= ? AND pnl IS NOT NULL{mode_clause}",
            [week_start.isoformat(), *mode_params],
        )
        row = await cursor.fetchone()
        weekly_pnl = row[0] if row else 0
        cursor = await self.conn.execute(
            f"SELECT COALESCE(SUM(CAST(json_extract(realized_costs_json, '$.total') AS REAL)), 0) "
            f"FROM trades WHERE closed_at >= ? AND pnl IS NOT NULL "
            f"AND realized_costs_json IS NOT NULL{mode_clause}",
            [week_start.isoformat(), *mode_params],
        )
        row = await cursor.fetchone()
        weekly_charges = row[0] if row else 0
        weekly_pnl_pct = weekly_pnl / total_capital if total_capital > 0 else 0

        # Pending trade value (app-side block: trades waiting for user approval)
        pending_trade_value = 0.0
        try:
            cursor = await self.read_conn.execute(
                "SELECT entry_price, quantity FROM pending_trades WHERE status = 'pending'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                try:
                    pending_trade_value += float(row[0] or 0) * float(row[1] or 0)
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass

        # Holdings unrealized PnL (only meaningful when broker breakdown is fresh)
        holdings_unrealized = breakdown["holdings_current"] - breakdown["holdings_invested"]
        holdings_unrealized_pct = (
            holdings_unrealized / breakdown["holdings_invested"]
            if breakdown["holdings_invested"] > 0 else 0.0
        )

        # Total PnL (all-time realized + holdings unrealized)
        total_pnl_amount = float(all_time_pnl) + holdings_unrealized

        # Minutes since last loss
        cursor = await self.conn.execute(
            f"SELECT closed_at FROM trades "
            f"WHERE pnl IS NOT NULL AND pnl < 0{mode_clause} "
            f"ORDER BY closed_at DESC LIMIT 1",
            mode_params,
        )
        row = await cursor.fetchone()
        if row and row[0]:
            last_loss_time = datetime.fromisoformat(row[0])
            if last_loss_time.tzinfo is None:
                last_loss_time = last_loss_time.replace(tzinfo=IST)
            minutes_since_last_loss = (now - last_loss_time).total_seconds() / 60
        else:
            minutes_since_last_loss = 999.0  # no losses yet

        # If broker breakdown is available, prefer it as the authoritative
        # total_portfolio_value (cash + utilised + holdings_current).
        total_portfolio_value = breakdown["total"] if breakdown["total"] > 0 else total_capital

        return {
            "total_capital": total_capital,
            "available_cash": available_cash,
            "exposure_pct": exposure_pct,
            "open_positions": open_count,
            "system_positions": system_position_count,
            "adopted_positions": adopted_position_count,
            "system_position_value": round(system_position_value, 2),
            "adopted_position_value": round(adopted_position_value, 2),
            "stock_exposures": stock_exposures,
            "sector_counts": sector_counts,
            "daily_pnl_pct": daily_pnl_pct,
            "weekly_pnl_pct": weekly_pnl_pct,
            "daily_pnl": round(float(daily_pnl), 2),
            "daily_charges": round(float(daily_charges), 2),
            "weekly_pnl": round(float(weekly_pnl), 2),
            "weekly_charges": round(float(weekly_charges), 2),
            "trades_today": trades_today,
            "mis_trades_today": mis_trades_today,
            "cnc_trades_today": cnc_trades_today,
            "minutes_since_last_loss": minutes_since_last_loss,
            # Broker-synced breakdown
            "available_funds": round(breakdown["available_cash"], 2),
            "utilised_margin": round(breakdown["utilised_margin"], 2),
            "pending_trade_value": round(pending_trade_value, 2),
            "locked_total": round(breakdown["utilised_margin"] + pending_trade_value, 2),
            "holdings_invested": round(breakdown["holdings_invested"], 2),
            "holdings_current": round(breakdown["holdings_current"], 2),
            "holdings_unrealized_pnl": round(holdings_unrealized, 2),
            "holdings_unrealized_pnl_pct": round(holdings_unrealized_pct, 4),
            "total_portfolio_value": round(total_portfolio_value, 2),
            "total_pnl": round(total_pnl_amount, 2),
            "all_time_realized_pnl": round(float(all_time_pnl), 2),
            "all_time_charges": round(float(all_time_charges), 2),
        }

    # ------------------------------------------------------------------
    # Stock Sector
    # ------------------------------------------------------------------

    async def get_stock_sector(self, symbol: str) -> str | None:
        """Get sector for a symbol.

        Prefers the canonical `symbol_sectors` lookup (populated from the
        NSE Industry column by ingest-universe). Falls back to watchlist
        when not present — the user can manually set sector via the
        user-watchlist endpoints, and that override should still apply.
        """
        cursor = await self.conn.execute(
            "SELECT sector FROM symbol_sectors WHERE symbol = ?", (symbol.upper(),),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]
        cursor = await self.conn.execute(
            "SELECT sector FROM watchlist WHERE symbol = ?", (symbol,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    # ------------------------------------------------------------------
    # LLM Review Log
    # ------------------------------------------------------------------

    async def log_llm_review(
        self,
        signal: dict[str, Any],
        decision: str,
        reasoning: str,
        adjusted_size: int | None = None,
    ) -> None:
        """Log an LLM trade review for audit trail."""
        await self.conn.execute(
            "INSERT INTO llm_reviews (trade_id, decision, reasoning, adjusted_size) "
            "VALUES (?, ?, ?, ?)",
            (
                signal.get("trade_id") or signal.get("symbol", ""),
                decision,
                reasoning,
                adjusted_size,
            ),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Sector Rotation
    # ------------------------------------------------------------------

    async def get_sector_rotation(self) -> dict[str, Any]:
        """Get sector rotation data from watchlist scores.

        Sector is resolved via COALESCE(symbol_sectors, watchlist) so it
        works even when watchlist rows lack their own sector value (which
        is the common case now that ingest-universe is the source of truth).
        """
        cursor = await self.conn.execute(
            "SELECT COALESCE(ss.sector, w.sector) as sector, "
            "AVG(w.composite_score) as avg_score, COUNT(*) as count "
            "FROM watchlist w "
            "LEFT JOIN symbol_sectors ss ON w.symbol = ss.symbol "
            "WHERE COALESCE(ss.sector, w.sector) IS NOT NULL "
            "GROUP BY COALESCE(ss.sector, w.sector) "
            "ORDER BY avg_score DESC"
        )
        rows = await cursor.fetchall()
        if not rows:
            return {"strong": [], "weak": [], "sectors": {}}

        sectors = {row["sector"]: {"avg_score": row["avg_score"], "count": row["count"]} for row in rows}
        scores = [row["avg_score"] for row in rows if row["avg_score"] is not None]

        if scores:
            p75 = sorted(scores)[int(len(scores) * 0.75)] if len(scores) > 1 else scores[0]
            p25 = sorted(scores)[int(len(scores) * 0.25)] if len(scores) > 1 else scores[0]
            strong = [row["sector"] for row in rows if row["avg_score"] and row["avg_score"] >= p75]
            weak = [row["sector"] for row in rows if row["avg_score"] and row["avg_score"] <= p25]
        else:
            strong, weak = [], []

        return {"strong": strong, "weak": weak, "sectors": sectors}

    # ------------------------------------------------------------------
    # Today's Trades
    # ------------------------------------------------------------------

    async def get_todays_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get all trades created today (IST market day)."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        query = "SELECT * FROM trades WHERE created_at >= ?"
        params: list[Any] = [today_start]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY created_at"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_closed_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get trades that were closed today, regardless of when they were created."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        query = "SELECT * FROM trades WHERE closed_at >= ? AND status = 'closed'"
        params: list[Any] = [today_start]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY closed_at"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_signals_count(self) -> int:
        """Count signals generated today."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at >= ?",
            (today_start,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_signal_class_counts(
        self, days: int = 7, mode: str | None = None,
    ) -> dict[str, Any]:
        """Count signals per type (BUY/SELL/HOLD) in the last `days`.

        Returns:
          {
            "BUY": int, "SELL": int, "HOLD": int, "total": int,
            "by_day": [{"date": "YYYY-MM-DD", "BUY": int, ...}, ...],
          }

        Used by drift-watch (alert on a class going extinct) and the
        Dashboard signal-distribution widget. Mode-scoped when
        `mode` is provided so paper- and live-mode signals don't pool.
        """
        from datetime import timedelta as _td

        cutoff = (now_utc() - _td(days=days)).isoformat()
        params: list[Any] = [cutoff]
        mode_clause = ""
        if mode:
            mode_clause = " AND mode = ?"
            params.append(mode)

        cur = await self.read_conn.execute(
            f"SELECT signal_type, DATE(created_at) AS d, COUNT(*) "
            f"FROM signals WHERE created_at >= ?{mode_clause} "
            f"GROUP BY signal_type, d ORDER BY d ASC",
            tuple(params),
        )
        rows = await cur.fetchall()

        totals = {"BUY": 0, "SELL": 0, "HOLD": 0}
        per_day: dict[str, dict[str, int]] = {}
        for sig_type, d, count in rows:
            key = (sig_type or "").upper()
            if key not in totals:
                continue
            totals[key] += int(count)
            per_day.setdefault(d, {"BUY": 0, "SELL": 0, "HOLD": 0})
            per_day[d][key] = int(count)
        by_day = [
            {"date": d, **counts}
            for d, counts in sorted(per_day.items())
        ]
        return {
            "BUY": totals["BUY"],
            "SELL": totals["SELL"],
            "HOLD": totals["HOLD"],
            "total": sum(totals.values()),
            "by_day": by_day,
        }

    async def get_latest_sentiment(self, symbol: str) -> dict[str, Any] | None:
        """Get latest sentiment for a symbol as dict[str, Any] (for LLM review context)."""
        result = await self.get_sentiment(symbol)
        if result is None:
            return None
        return {
            "symbol": result.symbol,
            "sentiment": result.sentiment,
            "confidence": result.confidence,
            "key_drivers": result.key_drivers,
        }

    # ------------------------------------------------------------------
    # Trade Management
    # ------------------------------------------------------------------

    async def insert_trade(self, trade: dict[str, Any]) -> str:
        """Insert a new trade record atomically with audit log.

        Uses a savepoint so the trade insert + audit entry either both
        succeed or both roll back — no orphaned records on crash.
        """
        import uuid

        trade_id = trade.get("trade_id") or f"T-{uuid.uuid4().hex[:8]}"
        ts_now = now_utc().isoformat()

        trade_columns = await self._get_table_columns("trades")

        # Build column list dynamically based on available columns
        base_cols = [
            "trade_id", "symbol", "signal_type", "entry_price", "fill_price",
            "quantity", "stop_loss_price", "target_price", "order_id", "sl_order_id",
            "product", "mode", "status", "slippage",
        ]
        optional_cols = ["estimated_costs", "expected_holding_days", "signal_id"]
        insert_cols = base_cols + [c for c in optional_cols if c in trade_columns] + ["created_at"]
        placeholders = ", ".join("?" for _ in insert_cols)
        col_names = ", ".join(insert_cols)

        values = tuple(
            trade.get(c, trade.get("entry_price") if c == "fill_price" else None)
            if c not in ("trade_id", "created_at", "product", "mode", "status", "slippage")
            else {
                "trade_id": trade_id,
                "created_at": ts_now,
                "product": trade.get("product", "MIS"),
                "mode": trade.get("mode", "paper"),
                "status": trade.get("status", "open"),
                "slippage": trade.get("slippage", 0.0),
            }[c]
            for c in insert_cols
        )

        # Idempotency pre-check. The trades.signal_id UNIQUE index
        # (migration 042) is the hard guarantee; this SELECT is the
        # graceful-error path so callers get a DuplicateSignalError
        # instead of a raw IntegrityError. trade-execute is the only
        # writer that sets signal_id and it processes signals
        # sequentially per heartbeat, so the TOCTOU window between
        # this SELECT and the INSERT below is effectively zero. If a
        # parallel writer ever appears, the UNIQUE index still catches
        # the duplicate — the caller just sees an IntegrityError
        # bubble up instead of the typed DuplicateSignalError.
        sig_id = trade.get("signal_id")
        if sig_id:
            cur = await self.read_conn.execute(
                "SELECT trade_id FROM trades WHERE signal_id = ?",
                (int(sig_id),),
            )
            row = await cur.fetchone()
            if row:
                raise DuplicateSignalError(
                    signal_id=int(sig_id),
                    existing_trade_id=str(row[0]),
                )

        await self.conn.execute("SAVEPOINT insert_trade")
        try:
            await self.conn.execute(
                f"INSERT INTO trades ({col_names}) VALUES ({placeholders})",
                values,
            )
            await self.conn.execute(
                "INSERT INTO audit_log (timestamp_ist, action_type, skill_name, "
                "input_summary, output_summary) VALUES (?, ?, ?, ?, ?)",
                (
                    ts_now, "trade_inserted", "trade-execute",
                    json.dumps({
                        "trade_id": trade_id, "symbol": trade["symbol"],
                        "signal_type": trade["signal_type"],
                        "mode": trade.get("mode", "paper"),
                    }),
                    json.dumps({
                        "fill_price": trade.get("fill_price"),
                        "quantity": trade["quantity"],
                        "slippage": trade.get("slippage", 0.0),
                    }),
                ),
            )
            await self.conn.execute("RELEASE SAVEPOINT insert_trade")
            await self.conn.commit()
        except Exception:
            await self.conn.execute("ROLLBACK TO SAVEPOINT insert_trade")
            raise
        return trade_id

    async def update_position_sl(self, position_id: int | str, new_sl: float) -> None:
        """Update stop-loss price for an open position."""
        await self.conn.execute(
            "UPDATE trades SET stop_loss_price = ? WHERE trade_id = ?",
            (new_sl, str(position_id)),
        )
        await self.conn.commit()

    async def upsert_funds_snapshot(
        self,
        snapshot_date: str,
        mode: str,
        summary: dict[str, float],
        raw_json: str | None = None,
        holdings_invested: float = 0.0,
        holdings_current: float = 0.0,
    ) -> None:
        """Insert today's funds/margins snapshot (or replace if it
        already exists for the same date+mode).

        Called by the funds-snapshot CRON skill so the user can track
        daily cash movements without logging into Kite.
        """
        from yolovest.timezone import now_utc as _now_utc

        captured_at = _now_utc().isoformat()
        keys_in_order = (
            "available_cash", "live_balance", "opening_balance",
            "utilised_margin", "m2m_unrealised", "m2m_realised",
            "payout", "collateral", "exposure", "span", "delivery", "net",
        )
        params: list[Any] = [
            snapshot_date, captured_at, mode,
        ]
        params.extend(float(summary.get(k, 0.0) or 0.0) for k in keys_in_order)
        params.extend([float(holdings_invested), float(holdings_current), raw_json])

        col_list = (
            "snapshot_date, captured_at, mode, " + ", ".join(keys_in_order)
            + ", holdings_invested, holdings_current, raw_json"
        )
        placeholders = ", ".join(["?"] * (3 + len(keys_in_order) + 3))
        await self.conn.execute(
            f"INSERT INTO funds_snapshots ({col_list}) VALUES ({placeholders}) "
            "ON CONFLICT(snapshot_date, mode) DO UPDATE SET "
            "captured_at=excluded.captured_at, "
            + ", ".join(f"{k}=excluded.{k}" for k in keys_in_order)
            + ", holdings_invested=excluded.holdings_invested"
            + ", holdings_current=excluded.holdings_current"
            + ", raw_json=excluded.raw_json",
            params,
        )
        await self.conn.commit()

    async def get_funds_snapshots(
        self, mode: str | None = None, days: int = 90,
    ) -> list[dict[str, Any]]:
        """Return funds snapshots (newest first) for the last N days.

        Mode-scoped when supplied; paper / live snapshots are stored
        independently so flipping modes doesn't corrupt either history.
        """
        from datetime import date as _date, timedelta as _td
        since = (_date.today() - _td(days=days)).isoformat()
        query = (
            "SELECT id, snapshot_date, captured_at, mode, available_cash, "
            "live_balance, opening_balance, utilised_margin, m2m_unrealised, "
            "m2m_realised, payout, collateral, exposure, span, delivery, net, "
            "holdings_invested, holdings_current "
            "FROM funds_snapshots WHERE snapshot_date >= ?"
        )
        params: list[Any] = [since]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY snapshot_date DESC, captured_at DESC"
        cur = await self.read_conn.execute(query, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def increment_realized_partial_pnl(
        self, position_id: int | str, partial_pnl: float,
    ) -> None:
        """Add a partial-booking PnL to the trade's running total.

        Each partial close is recorded individually in audit_log (with
        exit_qty, exit_price, etc.); the running sum lives on trades
        so reads don't need a JOIN. The final close (full exit of the
        remainder) populates `pnl` separately; UI surfaces
        total = realized_partial_pnl + pnl.
        """
        await self.conn.execute(
            "UPDATE trades "
            "SET realized_partial_pnl = COALESCE(realized_partial_pnl, 0) + ? "
            "WHERE trade_id = ?",
            (float(partial_pnl), str(position_id)),
        )
        await self.conn.commit()

    async def update_position_quantity(
        self, position_id: int | str, new_quantity: int,
    ) -> None:
        """Resize an open position after a partial close.

        Used by the user-initiated partial-close endpoint. The full
        close path goes through `close_position` instead (which sets
        status='closed'); this one keeps status='open' with the
        remaining quantity. Callers are expected to have already
        resized any broker-side SL / target / GTT to match.
        """
        await self.conn.execute(
            "UPDATE trades SET quantity = ? WHERE trade_id = ?",
            (int(new_quantity), str(position_id)),
        )
        await self.conn.commit()

    async def update_unrealized_pnl(self, position_id: int | str, current_price: float) -> None:
        """Update unrealized PnL for an open position based on current price.

        Note: PnL is stored as NULL while position is open; this updates
        a computed field or can be used for tracking in audit_log.
        """
        ts_now = now_utc().isoformat()
        # Log unrealized PnL as audit entry for tracking
        await self.log_audit(
            action_type="unrealized_pnl_update",
            input_summary={"position_id": str(position_id), "current_price": current_price},
        )

    async def close_position(
        self,
        position_id: int | str,
        exit_price: float,
        pnl: float,
        realized_costs: dict[str, Any] | None = None,
    ) -> None:
        """Close a position with exit price, realized PnL, and an optional
        breakdown of the actual charges applied (brokerage/stt/other/total
        plus a `source` of "broker" or "estimate").

        Uses a savepoint to ensure the trade update and audit log are
        committed atomically — no half-closed positions.
        """
        ts_now = now_utc().isoformat()
        pos_id = str(position_id)
        costs_json = json.dumps(realized_costs) if realized_costs else None
        await self.conn.execute("SAVEPOINT close_position")
        try:
            await self.conn.execute(
                "UPDATE trades SET status = 'closed', exit_price = ?, pnl = ?, "
                "closed_at = ?, realized_costs_json = COALESCE(?, realized_costs_json) "
                "WHERE trade_id = ?",
                (exit_price, pnl, ts_now, costs_json, pos_id),
            )
            await self.conn.execute(
                "INSERT INTO audit_log (timestamp_ist, action_type, skill_name, "
                "input_summary, output_summary) VALUES (?, ?, ?, ?, ?)",
                (
                    ts_now, "position_closed", "position-monitor",
                    json.dumps({"trade_id": pos_id, "exit_price": exit_price}),
                    json.dumps({"pnl": pnl}),
                ),
            )
            # Per-trade sentinels in system_state (e.g. partial_booked_{id})
            # outlive the position they describe and have no TTL — clean
            # them up here so system_state doesn't grow monotonically.
            await self.conn.execute(
                "DELETE FROM system_state WHERE key = ?",
                (f"partial_booked_{pos_id}",),
            )
            await self.conn.execute("RELEASE SAVEPOINT close_position")
            await self.conn.commit()
        except Exception:
            await self.conn.execute("ROLLBACK TO SAVEPOINT close_position")
            raise

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    async def insert_prediction(self, prediction: dict[str, Any]) -> str:
        """Insert a new prediction and return its ID."""
        import uuid

        pred_id = prediction.get("prediction_id") or f"P-{uuid.uuid4().hex[:8]}"
        ts_now = now_utc().isoformat()

        # Compute prediction end time from holding period
        from yolovest.models.schemas import _parse_holding_period

        holding = prediction.get("expected_holding_period", "intraday")
        end_time = now_utc() + _parse_holding_period(holding)

        # Try to find the matching signal_id from signals table
        signal_id = prediction.get("signal_id")
        if not signal_id and prediction.get("symbol"):
            cursor = await self.conn.execute(
                "SELECT id FROM signals WHERE symbol = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (prediction["symbol"],),
            )
            row = await cursor.fetchone()
            if row:
                signal_id = row[0]

        await self.conn.execute(
            "INSERT INTO predictions (prediction_id, signal_id, trade_id, symbol, "
            "created_at, prediction_end_time, actual_price, direction_correct, "
            "target_hit, actual_pnl_pct, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
            (
                pred_id, signal_id, prediction.get("trade_id"),
                prediction.get("symbol"), ts_now, end_time.isoformat(),
                prediction.get("mode", "paper"),
            ),
        )

        # Also store prediction details in audit for traceability
        await self.log_audit(
            action_type="prediction_logged",
            skill_name="predict-track",
            input_summary={
                "prediction_id": pred_id,
                "symbol": prediction.get("symbol"),
                "direction": prediction.get("predicted_direction"),
                "confidence": prediction.get("confidence"),
                "target": prediction.get("predicted_target"),
                "model_version": prediction.get("model_version"),
            },
            auto_commit=False,
        )
        await self.conn.commit()
        return pred_id

    async def insert_shadow_prediction(self, prediction: dict[str, Any]) -> str:
        """Insert a shadow model prediction for A/B comparison."""
        import uuid

        pred_id = f"SP-{uuid.uuid4().hex[:8]}"
        ts_now = now_utc().isoformat()
        symbol = prediction.get("symbol")
        mode = prediction.get("mode", "paper")

        from yolovest.models.schemas import _parse_holding_period

        holding = prediction.get("expected_holding_period", "intraday")
        end_time = now_utc() + _parse_holding_period(holding)

        # Check if symbol column exists (migration 012)
        columns = await self._get_table_columns("predictions")
        has_symbol = "symbol" in columns

        if has_symbol:
            await self.conn.execute(
                "INSERT INTO predictions (prediction_id, symbol, signal_id, trade_id, created_at, "
                "prediction_end_time, actual_price, direction_correct, target_hit, "
                "actual_pnl_pct, is_shadow, model_version, mode) "
                "VALUES (?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, 1, ?, ?)",
                (pred_id, symbol, ts_now, end_time.isoformat(),
                 prediction.get("model_version"), mode),
            )
        else:
            await self.conn.execute(
                "INSERT INTO predictions (prediction_id, signal_id, trade_id, created_at, "
                "prediction_end_time, actual_price, direction_correct, target_hit, "
                "actual_pnl_pct, is_shadow, model_version, mode) "
                "VALUES (?, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, 1, ?, ?)",
                (pred_id, ts_now, end_time.isoformat(),
                 prediction.get("model_version"), mode),
            )

        # Also link to the latest signal for this symbol (best-effort)
        if symbol:
            await self.conn.execute(
                "UPDATE predictions SET "
                "signal_id = (SELECT id FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT 1) "
                "WHERE prediction_id = ?",
                (symbol, pred_id),
            )
        await self.conn.commit()
        return pred_id

    async def get_shadow_vs_production_metrics(
        self, model_type: str, since_date: str,
    ) -> dict[str, Any]:
        """Compare shadow vs production prediction accuracy over a period.

        Returns {shadow: {metrics}, production: {metrics}, agreement_rate}.
        """
        # Get the production and shadow model versions for this type
        prod = await self.get_production_model(model_type)
        prod_version = prod["version"] if prod else None

        shadow_models = await self.get_all_shadow_models()
        shadow_versions = [
            s["version"] for s in shadow_models if s["model_type"] == model_type
        ]

        result: dict[str, Any] = {"shadow": {}, "production": {}, "agreement_rate": None}

        # Fetch scored predictions grouped by is_shadow
        cursor = await self.conn.execute(
            "SELECT p.is_shadow, p.model_version, "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN p.direction_correct = 1 THEN 1 ELSE 0 END) as correct, "
            "  SUM(CASE WHEN p.target_hit = 1 THEN 1 ELSE 0 END) as targets_hit, "
            "  AVG(p.actual_pnl_pct) as avg_pnl "
            "FROM predictions p "
            "WHERE p.actual_price IS NOT NULL "
            "AND p.created_at >= ? "
            "GROUP BY p.is_shadow",
            (since_date,),
        )
        rows = await cursor.fetchall()

        for row in rows:
            metrics = {
                "total": row["total"],
                "correct": row["correct"],
                "targets_hit": row["targets_hit"],
                "direction_accuracy": round(row["correct"] / row["total"], 4) if row["total"] > 0 else 0,
                "target_hit_rate": round(row["targets_hit"] / row["total"], 4) if row["total"] > 0 else 0,
                "avg_pnl_pct": round(row["avg_pnl"], 4) if row["avg_pnl"] is not None else 0,
            }
            if row["is_shadow"]:
                result["shadow"] = metrics
            else:
                result["production"] = metrics

        return result

    async def compute_symbol_beta(
        self, symbol: str, lookback_days: int = 60,
    ) -> float | None:
        """Compute a symbol's beta against a cross-sectional market
        proxy. The proxy is the equal-weight mean daily return of every
        symbol with daily bars in the lookback window — the same proxy
        compute_live_regime uses. Returns None when fewer than 20
        overlapping (symbol, market) return pairs are available.

        Formula: beta = cov(symbol_ret, market_ret) / var(market_ret).
        Standard CAPM-style regression slope.

        Used by the risk_check portfolio-beta gate. Cheap enough to
        run on demand inside a heartbeat for the small candidate set,
        but callers should cache per-heartbeat since the inputs are
        the same for every signal in a cycle.
        """
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=lookback_days * 2)).isoformat()
        # Pull all daily bars from the lookback window across the
        # whole universe — same scope as compute_live_regime so the
        # proxy is consistent.
        cursor = await self.read_conn.execute(
            "SELECT symbol, timestamp, close FROM ohlcv "
            "WHERE interval = 'daily' AND timestamp >= ? "
            "ORDER BY symbol, timestamp",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None

        # Build per-symbol close series + the market average per day.
        by_sym: dict[str, list[tuple[str, float]]] = {}
        for r in rows:
            by_sym.setdefault(r[0], []).append((r[1][:10], float(r[2])))

        # Per-day market mean return.
        day_returns: dict[str, list[float]] = {}
        for sym_closes in by_sym.values():
            for i in range(1, len(sym_closes)):
                prev = sym_closes[i - 1][1]
                cur = sym_closes[i][1]
                if prev > 0:
                    ret = (cur - prev) / prev
                    day_returns.setdefault(sym_closes[i][0], []).append(ret)
        market_by_day = {
            d: sum(rs) / len(rs) for d, rs in day_returns.items() if rs
        }

        # Symbol-specific paired series.
        sym_series = by_sym.get(symbol, [])
        if len(sym_series) < 2:
            return None
        sym_returns: list[tuple[float, float]] = []
        for i in range(1, len(sym_series)):
            d = sym_series[i][0]
            prev = sym_series[i - 1][1]
            cur = sym_series[i][1]
            if prev > 0 and d in market_by_day:
                sym_returns.append(((cur - prev) / prev, market_by_day[d]))

        if len(sym_returns) < 20:
            return None

        n = len(sym_returns)
        mean_s = sum(s for s, _ in sym_returns) / n
        mean_m = sum(m for _, m in sym_returns) / n
        cov = sum((s - mean_s) * (m - mean_m) for s, m in sym_returns) / n
        var_m = sum((m - mean_m) ** 2 for _, m in sym_returns) / n
        if var_m <= 0:
            return None
        return cov / var_m

    async def get_live_metrics_for_model(
        self, model_version: str, days: int = 14,
    ) -> dict[str, Any]:
        """Live (i.e. scored-against-actual) metrics for a specific
        model version over the last `days` calendar days. Used by the
        shadow-promotion gate so we can require the shadow to actually
        outperform on real predictions, not just on backtest.

        Returns: {total, scored, direction_accuracy, target_hit_rate,
        avg_pnl_pct} — all zeros when there are no scored predictions
        for the version (caller should treat that as "no live data
        yet, fall back to backtest").
        """
        from datetime import timedelta
        since = (now_utc() - timedelta(days=days)).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN direction_correct IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
            "  SUM(CASE WHEN direction_correct = 1 THEN 1 ELSE 0 END) AS correct, "
            "  SUM(CASE WHEN target_hit = 1 THEN 1 ELSE 0 END) AS targets_hit, "
            "  AVG(actual_pnl_pct) AS avg_pnl "
            "FROM predictions "
            "WHERE model_version = ? AND created_at >= ?",
            (model_version, since),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return {
                "total": 0, "scored": 0,
                "direction_accuracy": 0.0,
                "target_hit_rate": 0.0,
                "avg_pnl_pct": 0.0,
            }
        total = int(row[0] or 0)
        scored = int(row[1] or 0)
        correct = int(row[2] or 0)
        targets_hit = int(row[3] or 0)
        avg_pnl = float(row[4] or 0)
        return {
            "total": total,
            "scored": scored,
            "direction_accuracy": round(correct / scored, 4) if scored > 0 else 0.0,
            "target_hit_rate": round(targets_hit / scored, 4) if scored > 0 else 0.0,
            "avg_pnl_pct": round(avg_pnl, 4),
        }

    async def get_unscored_predictions(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get predictions whose holding period has elapsed but haven't been scored.

        Used by the predict-track skill to know which predictions are ready to score.
        """
        ts_now = now_utc().isoformat()
        mode_clause = " AND p.mode = ?" if mode else ""
        mode_params: list[Any] = [mode] if mode else []
        cursor = await self.conn.execute(
            f"SELECT p.prediction_id as id, p.trade_id, p.created_at, "
            f"p.prediction_end_time, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as predicted_direction, "
            f"COALESCE(s.entry_price, t.entry_price) as entry_price, "
            f"COALESCE(s.target_price, t.target_price) as predicted_target, "
            f"COALESCE(s.stop_loss_price, t.stop_loss_price) as predicted_stop_loss, "
            f"s.confidence_score as confidence, "
            f"s.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"WHERE p.actual_price IS NULL "
            f"AND p.prediction_end_time <= ?{mode_clause} "
            f"ORDER BY p.created_at",
            [ts_now, *mode_params],
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_all_awaiting_predictions(
        self, *, limit: int = 50, offset: int = 0,
        symbol: str | None = None, direction: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Get ALL unscored predictions for UI display (regardless of end time)."""
        where = "WHERE p.actual_price IS NULL"
        params: list[Any] = []
        where, params = self._apply_prediction_filters(
            where, params, symbol=symbol, direction=direction, model=model,
        )
        cursor = await self.read_conn.execute(
            f"SELECT COUNT(*) FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id {where}",
            params,
        )
        total = (await cursor.fetchone())[0]
        cursor = await self.read_conn.execute(
            f"SELECT p.prediction_id, p.prediction_id as id, p.trade_id, p.created_at, "
            f"p.prediction_end_time, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"COALESCE(s.entry_price, t.entry_price) as entry_price, "
            f"COALESCE(s.target_price, t.target_price) as predicted_target, "
            f"COALESCE(s.stop_loss_price, t.stop_loss_price) as predicted_stop_loss, "
            f"s.confidence_score, p.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"{where} ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"items": [dict[str, Any](r) for r in rows], "total": total}

    @staticmethod
    def _apply_prediction_filters(
        where: str, params: list[Any], *,
        symbol: str | None = None, direction: str | None = None,
        direction_correct: int | None = None, target_hit: int | None = None,
        model: str | None = None, min_confidence: float | None = None,
    ) -> tuple[str, list[Any]]:
        """Append filter clauses to a prediction query WHERE string."""
        if symbol:
            where += " AND COALESCE(p.symbol, s.symbol, t.symbol) = ?"
            params.append(symbol)
        if direction:
            where += " AND COALESCE(s.signal_type, t.signal_type) = ?"
            params.append(direction)
        if direction_correct is not None:
            where += " AND p.direction_correct = ?"
            params.append(direction_correct)
        if target_hit is not None:
            where += " AND p.target_hit = ?"
            params.append(target_hit)
        if model:
            where += " AND p.model_version = ?"
            params.append(model)
        if min_confidence is not None:
            where += " AND s.confidence_score >= ?"
            params.append(min_confidence)
        return where, params

    async def get_prediction_outcomes_paginated(
        self, *, limit: int = 50, offset: int = 0,
        symbol: str | None = None, direction: str | None = None,
        direction_correct: int | None = None, target_hit: int | None = None,
        model: str | None = None, min_confidence: float | None = None,
    ) -> dict[str, Any]:
        """Scored predictions with pagination and filters."""
        where = "WHERE p.actual_price IS NOT NULL"
        params: list[Any] = []
        where, params = self._apply_prediction_filters(
            where, params, symbol=symbol, direction=direction,
            direction_correct=direction_correct, target_hit=target_hit,
            model=model, min_confidence=min_confidence,
        )
        cursor = await self.read_conn.execute(
            f"SELECT COUNT(*) FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id {where}",
            params,
        )
        total = (await cursor.fetchone())[0]
        cursor = await self.read_conn.execute(
            f"SELECT p.*, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"s.confidence_score, p.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"{where} ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"items": [dict[str, Any](r) for r in rows], "total": total}

    async def set_trade_gtt(self, trade_id: str, gtt_id: int | None) -> None:
        """Attach (or clear) the broker GTT trigger id on an open trade."""
        await self.conn.execute(
            "UPDATE trades SET gtt_id = ? WHERE trade_id = ?",
            (gtt_id, trade_id),
        )
        await self.conn.commit()

    async def find_trade_by_order_id(
        self, order_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Find an open trade that has this order_id attached to any of
        its order columns (entry, SL, target). Returns (trade, leg)
        where leg is "entry", "sl", or "target". Returns (None, None) if
        nothing matches.

        Used by the postback handler to route broker-side order updates
        to the right business logic.
        """
        for leg, column in (
            ("entry", "order_id"),
            ("sl", "sl_order_id"),
            ("target", "target_order_id"),
        ):
            cursor = await self.read_conn.execute(
                f"SELECT * FROM trades WHERE {column} = ? LIMIT 1",
                (str(order_id),),
            )
            row = await cursor.fetchone()
            if row:
                return dict[str, Any](row), leg
        return None, None

    async def log_gtt_event(
        self,
        *,
        trade_id: str | None,
        gtt_id: int | None,
        symbol: str | None,
        event_type: str,
        status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a row to the GTT audit log. Idempotent / cheap — used
        from every GTT lifecycle site (place, modify, delete, reconcile,
        rejected_placement) so post-mortems have a single source of truth.

        Failure is swallowed; audit gaps shouldn't break the calling
        trading path.
        """
        try:
            await self.conn.execute(
                "INSERT INTO gtt_events "
                "(trade_id, gtt_id, symbol, event_type, status, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    trade_id,
                    int(gtt_id) if gtt_id is not None else None,
                    symbol,
                    event_type,
                    status,
                    json.dumps(details) if details else None,
                ),
            )
            await self.conn.commit()
        except Exception:
            logger.debug("log_gtt_event failed", exc_info=True)

    async def get_gtt_events_for_trade(
        self, trade_id: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return the GTT audit trail for a single trade, newest first."""
        cursor = await self.read_conn.execute(
            "SELECT id, timestamp_utc, trade_id, gtt_id, symbol, "
            "event_type, status, details_json "
            "FROM gtt_events WHERE trade_id = ? "
            "ORDER BY timestamp_utc DESC LIMIT ?",
            (trade_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def set_trade_gtt_status(
        self, trade_id: str, status: str | None,
    ) -> None:
        """Update the cached GTT lifecycle status (active / triggered /
        cancelled / rejected / expired / disabled / deleted).

        Used by position-monitor's reconciler to detect GTTs that no
        longer protect the position so client-side detection can take
        over.
        """
        await self.conn.execute(
            "UPDATE trades SET gtt_status = ? WHERE trade_id = ?",
            (status, trade_id),
        )
        await self.conn.commit()

    async def set_trade_product(
        self, trade_id: str, product: str,
    ) -> None:
        """Update the product (MIS / CNC) on a trade after a broker-side
        position conversion. Used by `/api/positions/{trade_id}/convert`.
        """
        await self.conn.execute(
            "UPDATE trades SET product = ? WHERE trade_id = ?",
            (product, trade_id),
        )
        await self.conn.commit()

    async def set_trade_target_order_id(
        self, trade_id: str, target_order_id: str | None,
    ) -> None:
        """Attach (or clear) the broker target-LIMIT order id on a MIS trade.

        MIS positions can't use GTT, so trade-execute places a LIMIT order
        at target alongside the SL. Position-monitor enforces OCO semantics
        by cancelling whichever side hasn't filled when the other does.
        """
        await self.conn.execute(
            "UPDATE trades SET target_order_id = ? WHERE trade_id = ?",
            (target_order_id, trade_id),
        )
        await self.conn.commit()

    async def set_trade_sl_order_id(
        self, trade_id: str, sl_order_id: str | None,
    ) -> None:
        """Update the SL order id on a trade — used when position-monitor
        cancels and re-places SL (e.g. trailing) or clears it after the
        target LIMIT fills."""
        await self.conn.execute(
            "UPDATE trades SET sl_order_id = ? WHERE trade_id = ?",
            (sl_order_id, trade_id),
        )
        await self.conn.commit()

    async def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        """Fetch a single trade row by id (any status)."""
        cursor = await self.read_conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def score_prediction(
        self,
        prediction_id: str,
        actual_price: float,
        direction_correct: bool,
        target_hit: bool,
        actual_pnl_pct: float,
    ) -> None:
        """Update a prediction with actual outcome."""
        await self.conn.execute(
            "UPDATE predictions SET actual_price = ?, direction_correct = ?, "
            "target_hit = ?, actual_pnl_pct = ?, scored_at = datetime('now') "
            "WHERE prediction_id = ?",
            (
                actual_price,
                1 if direction_correct else 0,
                1 if target_hit else 0,
                actual_pnl_pct,
                prediction_id,
            ),
        )
        await self.conn.commit()

    async def refresh_prediction_scoreboard(self) -> None:
        """Rebuild the prediction scoreboard.

        Aggregates prediction accuracy by symbol, model version, timeframe, and overall.
        """
        scored_predictions = await self._get_all_scored_predictions()
        if not scored_predictions:
            return

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

        for pred in scored_predictions:
            # Overall
            key_overall = ("overall", "overall")
            groups.setdefault(key_overall, []).append(pred)

            # By symbol
            symbol = pred.get("symbol")
            if symbol:
                key_sym = (f"symbol:{symbol}", "symbol")
                groups.setdefault(key_sym, []).append(pred)

            # By model version
            model = pred.get("model_version")
            if model:
                key_model = (f"model:{model}", "model")
                groups.setdefault(key_model, []).append(pred)

        for (group_key, group_type), preds in groups.items():
            total = len(preds)
            correct = sum(1 for p in preds if p.get("direction_correct"))
            accuracy = correct / total if total > 0 else 0
            avg_conf = (
                sum((p.get("confidence") or 0) for p in preds) / total if total > 0 else 0
            )
            target_hits = sum(1 for p in preds if p.get("target_hit"))
            target_rate = target_hits / total if total > 0 else 0
            avg_pnl = (
                sum((p.get("actual_pnl_pct") or 0) for p in preds) / total
                if total > 0
                else 0
            )

            await self.conn.execute(
                "INSERT INTO prediction_scoreboard "
                "(group_key, group_type, total_predictions, correct_predictions, "
                "accuracy, avg_confidence, target_hit_rate, avg_pnl_pct, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(group_key, group_type) DO UPDATE SET "
                "total_predictions=excluded.total_predictions, "
                "correct_predictions=excluded.correct_predictions, "
                "accuracy=excluded.accuracy, avg_confidence=excluded.avg_confidence, "
                "target_hit_rate=excluded.target_hit_rate, avg_pnl_pct=excluded.avg_pnl_pct, "
                "updated_at=excluded.updated_at",
                (group_key, group_type, total, correct, accuracy, avg_conf, target_rate, avg_pnl),
            )
        await self.conn.commit()

    async def _get_all_scored_predictions(self) -> list[dict[str, Any]]:
        """Get all predictions with outcomes for scoreboard computation."""
        cursor = await self.conn.execute(
            "SELECT p.prediction_id, p.direction_correct, p.target_hit, "
            "p.actual_pnl_pct, COALESCE(p.symbol, s.symbol, t.symbol) as symbol, s.confidence_score as confidence, "
            "s.model_version "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
            "LEFT JOIN trades t ON p.trade_id = t.trade_id "
            "WHERE p.actual_price IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_prediction_scoreboard(self, group_type: str | None = None) -> list[dict[str, Any]]:
        """Get prediction scoreboard entries, optionally filtered by group type."""
        if group_type:
            cursor = await self.conn.execute(
                "SELECT * FROM prediction_scoreboard WHERE group_type = ? "
                "ORDER BY total_predictions DESC",
                (group_type,),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM prediction_scoreboard ORDER BY group_type, total_predictions DESC"
            )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_predictions(
        self, *, limit: int = 50, offset: int = 0,
        symbol: str | None = None, direction: str | None = None,
        model: str | None = None, mode: str | None = None,
    ) -> dict[str, Any]:
        """Get predictions created today (IST market day) with pagination and filters."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        where = "WHERE p.created_at >= ?"
        params: list[Any] = [today_start]
        if mode:
            where += " AND p.mode = ?"
            params.append(mode)
        where, params = self._apply_prediction_filters(
            where, params, symbol=symbol, direction=direction, model=model,
        )
        # Total count
        cursor = await self.read_conn.execute(
            f"SELECT COUNT(*) FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id {where}",
            params,
        )
        total = (await cursor.fetchone())[0]
        # Page
        cursor = await self.read_conn.execute(
            f"SELECT p.*, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"s.confidence_score, p.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"{where} ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"items": [dict[str, Any](r) for r in rows], "total": total}

    # ------------------------------------------------------------------
    # Weekly Data
    # ------------------------------------------------------------------

    async def get_weekly_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get trades for the current week (Monday-Friday)."""
        from datetime import timedelta

        now = now_ist()
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        ).astimezone(UTC)
        query = "SELECT * FROM trades WHERE created_at >= ?"
        params: list[Any] = [monday.isoformat()]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY created_at"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_weekly_predictions(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get predictions for the current week."""
        from datetime import timedelta

        now = now_ist()
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        ).astimezone(UTC)
        mode_clause = " AND p.mode = ?" if mode else ""
        mode_params: list[Any] = [mode] if mode else []
        cursor = await self.conn.execute(
            f"SELECT p.*, COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"s.confidence_score "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"WHERE p.created_at >= ?{mode_clause} ORDER BY p.created_at",
            [monday.isoformat(), *mode_params],
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_weekly_llm_reviews(self) -> list[dict[str, Any]]:
        """Get LLM reviews for the current week with linked trade PnL."""
        from datetime import timedelta

        now = now_ist()
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        ).astimezone(UTC)
        cursor = await self.conn.execute(
            "SELECT lr.*, t.pnl as trade_pnl "
            "FROM llm_reviews lr "
            "LEFT JOIN trades t ON lr.trade_id = t.trade_id "
            "WHERE lr.created_at >= ? ORDER BY lr.created_at",
            (monday.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    async def store_report(self, report: dict[str, Any]) -> None:
        """Store a report in the reports archive."""
        from datetime import date

        report_date = report.get("date") or date.today().isoformat()
        await self.conn.execute(
            "INSERT INTO reports (report_type, report_date, content) VALUES (?, ?, ?)",
            (report.get("type", "daily"), report_date, json.dumps(report)),
        )
        await self.conn.commit()

    async def retire_model(self, model_type: str, version: str) -> None:
        """Retire a model version (e.g. shadow that underperformed)."""
        await self.conn.execute(
            "UPDATE model_versions SET status = 'retired' "
            "WHERE model_type = ? AND version = ?",
            (model_type, version),
        )
        await self.conn.commit()

    async def get_shadow_models_ready(self, shadow_mode_days: int) -> list[dict[str, Any]]:
        """Get shadow models that have completed their trial period."""
        from datetime import timedelta

        cutoff = (now_utc() - timedelta(days=shadow_mode_days)).isoformat()
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions "
            "WHERE status = 'shadow' AND shadow_start_date <= ?",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Dashboard Queries
    # ------------------------------------------------------------------

    async def get_trades_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
        mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get trade history with optional filters.

        Dates are YYYY-MM-DD. end_date is inclusive (includes all of that day).
        """
        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []

        if mode:
            query += " AND mode = ?"
            params.append(mode)
        if start_date:
            query += " AND created_at >= ?"
            params.append(start_date)
        if end_date:
            # end_date is inclusive: add one day as exclusive upper bound.
            # This avoids the T23:59:59 hack which misses the last second.
            from datetime import date, timedelta
            next_day = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
            query += " AND created_at < ?"
            params.append(next_day)
        if symbol:
            # Substring match (case-insensitive) so the Trades page search
            # acts like a filter rather than an exact-symbol picker —
            # typing "REL" matches RELIANCE, RELINFRA, etc. SQLite LIKE
            # is already case-insensitive for ASCII; symbol names are
            # ASCII so no need for unicode-aware collation.
            query += " AND symbol LIKE ?"
            params.append(f"%{symbol}%")

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self.read_conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_equity_curve(self, days: int = 30, mode: str | None = None) -> list[dict[str, Any]]:
        """Compute daily equity curve from closed trades.

        Returns a list of {date, cumulative_pnl, trade_count} entries.
        """
        from datetime import timedelta

        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params: list[Any] = [mode] if mode else []
        cursor = await self.conn.execute(
            f"SELECT DATE(closed_at) as trade_date, "
            f"SUM(pnl) as daily_pnl, COUNT(*) as trade_count "
            f"FROM trades "
            f"WHERE closed_at >= ? AND pnl IS NOT NULL{mode_clause} "
            f"GROUP BY DATE(closed_at) "
            f"ORDER BY trade_date",
            [cutoff, *mode_params],
        )
        rows = await cursor.fetchall()

        # Build cumulative curve
        cumulative = 0
        curve = []
        for row in rows:
            cumulative += row["daily_pnl"] or 0
            curve.append({
                "date": row["trade_date"],
                "daily_pnl": row["daily_pnl"],
                "cumulative_pnl": cumulative,
                "trade_count": row["trade_count"],
            })
        return curve

    async def get_daily_pnl_calendar(self, days: int = 90, mode: str | None = None) -> list[dict[str, Any]]:
        """Daily PnL breakdown for calendar heatmap.

        Returns one entry per day that had trades, with PnL, trade count,
        wins, and losses.
        """
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        mode_clause = " AND mode = ?" if mode else ""
        mode_params: list[Any] = [mode] if mode else []
        cursor = await self.read_conn.execute(
            f"SELECT DATE(closed_at) as trade_date, "
            f"SUM(pnl) as pnl, "
            f"COUNT(*) as trade_count, "
            f"SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses "
            f"FROM trades "
            f"WHERE closed_at >= ? AND pnl IS NOT NULL{mode_clause} "
            f"GROUP BY DATE(closed_at) "
            f"ORDER BY trade_date",
            [cutoff, *mode_params],
        )
        rows = await cursor.fetchall()
        return [
            {
                "date": row["trade_date"],
                "pnl": round(row["pnl"] or 0, 2),
                "trade_count": row["trade_count"],
                "wins": row["wins"],
                "losses": row["losses"],
            }
            for row in rows
        ]

    async def get_trade_detail(self, trade_id: str) -> dict[str, Any] | None:
        """Get full trade detail with reasoning chain.

        Returns trade + linked signal, LLM review, prediction, and audit entries.
        """
        # Trade record
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
        )
        trade_row = await cursor.fetchone()
        if not trade_row:
            return None

        trade = dict[str, Any](trade_row)

        # Linked LLM review
        cursor = await self.conn.execute(
            "SELECT * FROM llm_reviews WHERE trade_id = ? ORDER BY created_at DESC LIMIT 1",
            (trade_id,),
        )
        review_row = await cursor.fetchone()
        trade["llm_review"] = dict[str, Any](review_row) if review_row else None

        # Linked prediction
        cursor = await self.conn.execute(
            "SELECT * FROM predictions WHERE trade_id = ? ORDER BY created_at DESC LIMIT 1",
            (trade_id,),
        )
        pred_row = await cursor.fetchone()
        trade["prediction"] = dict[str, Any](pred_row) if pred_row else None

        # Linked signal (via prediction → signal_id, or by matching symbol+time)
        if pred_row and pred_row["signal_id"]:
            cursor = await self.conn.execute(
                "SELECT * FROM signals WHERE id = ?", (pred_row["signal_id"],)
            )
            sig_row = await cursor.fetchone()
            trade["signal"] = dict[str, Any](sig_row) if sig_row else None
        else:
            trade["signal"] = None

        # Relevant audit entries
        cursor = await self.conn.execute(
            "SELECT * FROM audit_log "
            "WHERE input_summary LIKE ? OR output_summary LIKE ? "
            "ORDER BY timestamp_ist DESC LIMIT 20",
            (f"%{trade_id}%", f"%{trade_id}%"),
        )
        audit_rows = await cursor.fetchall()
        trade["audit_trail"] = [dict[str, Any](r) for r in audit_rows]

        return trade

    async def get_reports_history(
        self,
        report_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Get historical reports with optional filters."""
        query = "SELECT * FROM reports WHERE 1=1"
        params: list[Any] = []

        if report_type:
            query += " AND report_type = ?"
            params.append(report_type)
        if start_date:
            query += " AND report_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND report_date <= ?"
            params.append(end_date)

        query += " ORDER BY report_date DESC LIMIT ?"
        params.append(limit)

        cursor = await self.read_conn.execute(query, params)
        rows = await cursor.fetchall()

        result = []
        for row in rows:
            entry = dict[str, Any](row)
            # Parse JSON content back to dict[str, Any]
            if entry.get("content"):
                try:
                    entry["content"] = json.loads(entry["content"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(entry)
        return result

    # ------------------------------------------------------------------
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
    ) -> dict[str, Any]:
        """Delete data older than retention periods.

        Daily and intraday OHLCV are trimmed on SEPARATE windows.
        Daily must cover the training history (`ohlcv_days`); intraday
        (5-minute etc.) is heavy and only used operationally, so it
        gets the shorter `intraday_ohlcv_days` (defaults to ohlcv_days
        for backwards-compat when the caller doesn't pass it).
        """
        from datetime import timedelta

        now = now_utc()
        deleted = {}

        # Daily OHLCV retention (the training-history window).
        cutoff = (now - timedelta(days=ohlcv_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM ohlcv WHERE interval = 'daily' AND timestamp < ?",
            (cutoff,),
        )
        deleted["ohlcv"] = cursor.rowcount

        # Intraday OHLCV retention (decoupled — 5-min bars are ~75×
        # heavier per day and not used for training). When the caller
        # doesn't supply intraday_ohlcv_days, fall back to ohlcv_days
        # so existing behaviour (single retention) is preserved.
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

        await self.conn.commit()
        logger.info("Retention cleanup: %s", deleted)
        return deleted

    # ------------------------------------------------------------------
    # Pending Trades (manual approval queue)
    # ------------------------------------------------------------------

    async def insert_pending_trade(self, signal: dict[str, Any]) -> int:
        """Queue a trade signal for manual approval. Returns the pending trade ID.

        Caller should set `mode` on the signal dict so bulk-delete and
        per-mode listings can scope correctly.
        """
        import json
        ts_now = now_utc().isoformat()
        cursor = await self.conn.execute(
            "INSERT INTO pending_trades "
            "(symbol, signal_type, entry_price, target_price, stop_loss_price, "
            "position_size, confidence_score, model_version, product, signal_data, "
            "mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal.get("symbol"),
                signal.get("signal_type"),
                signal.get("entry_price"),
                signal.get("target_price"),
                signal.get("stop_loss_price"),
                signal.get("position_size"),
                signal.get("confidence_score", signal.get("confidence")),
                signal.get("model_version"),
                signal.get("product", "MIS"),
                json.dumps(signal),
                signal.get("mode", "paper"),
                ts_now,
            ),
        )
        await self.conn.commit()
        pending_id = cursor.lastrowid or 0
        logger.info(
            "Inserted pending trade #%d: %s %s @ %.2f (created_at=%s)",
            pending_id, signal.get("signal_type"), signal.get("symbol"),
            signal.get("entry_price", 0), ts_now,
        )
        return pending_id

    async def get_pending_trades(self) -> list[dict[str, Any]]:
        """Get all pending trades awaiting approval."""
        cursor = await self.conn.execute(
            "SELECT * FROM pending_trades WHERE status = 'pending' "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def get_pending_trade_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Get a pending trade by symbol (case-insensitive). Returns None if not found."""
        cursor = await self.read_conn.execute(
            "SELECT * FROM pending_trades WHERE status = 'pending' "
            "AND UPPER(symbol) = UPPER(?) ORDER BY created_at DESC LIMIT 1",
            (symbol,),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def was_recently_rejected(self, symbol: str, signal_type: str, hours: int = 4) -> bool:
        """Check if a symbol+signal_type was rejected within the last N hours.

        Used to prevent re-queuing the same trade right after user rejects it.
        """
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT 1 FROM pending_trades "
            "WHERE status = 'rejected' AND UPPER(symbol) = UPPER(?) "
            "AND signal_type = ? AND decided_at >= ? "
            "LIMIT 1",
            (symbol, signal_type, cutoff),
        )
        return await cursor.fetchone() is not None

    async def decide_pending_trade(
        self, trade_id: int, decision: str, decided_by: str,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Approve or reject a pending trade. Returns the signal data if approved."""
        import json
        cursor = await self.conn.execute(
            "SELECT * FROM pending_trades WHERE id = ? AND status = 'pending'",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        if decision == "approved" and overrides:
            # Store user overrides in dedicated columns and flag as override
            override_cols = {
                "signal_type": "user_signal_type",
                "entry_price": "user_entry_price",
                "target_price": "user_target_price",
                "stop_loss_price": "user_stop_loss_price",
                "product": "user_product",
                "notes": "user_notes",
            }
            set_parts = [
                "status = ?", "decided_by = ?", "decided_at = datetime('now')",
                "is_override = 1",
            ]
            params: list[Any] = [decision, decided_by]
            for key, col in override_cols.items():
                if key in overrides:
                    set_parts.append(f"{col} = ?")
                    params.append(overrides[key])
            params.append(trade_id)
            await self.conn.execute(
                f"UPDATE pending_trades SET {', '.join(set_parts)} WHERE id = ?",
                tuple(params),
            )
        else:
            await self.conn.execute(
                "UPDATE pending_trades SET status = ?, decided_by = ?, "
                "decided_at = datetime('now') WHERE id = ?",
                (decision, decided_by, trade_id),
            )
        await self.conn.commit()

        if decision == "rejected":
            # Reflect the rejection on the originating signal row so the
            # Today's Recommendations panel updates.
            try:
                await self.update_signal_disposition(
                    row["symbol"], "rejected", f"rejected by {decided_by}",
                )
            except Exception:
                pass

        if decision == "approved":
            signal_data = row["signal_data"]
            signal = json.loads(signal_data) if signal_data else dict[str, Any](row)
            # Apply overrides to the returned signal dict
            if overrides:
                for key in ("signal_type", "entry_price", "target_price",
                            "stop_loss_price", "product", "position_size", "notes"):
                    if key in overrides:
                        signal[key] = overrides[key]
                signal["is_override"] = True
            return signal
        return None

    async def insert_manual_trade(self, trade_data: dict[str, Any]) -> int:
        """Insert a manually initiated trade (not from ML prediction)."""
        import json
        cursor = await self.conn.execute(
            "INSERT INTO pending_trades "
            "(symbol, signal_type, entry_price, target_price, stop_loss_price, "
            "position_size, product, signal_data, status, decided_by, decided_at, is_manual) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, datetime('now'), 1)",
            (
                trade_data["symbol"],
                trade_data["signal_type"],
                trade_data["entry_price"],
                trade_data["target_price"],
                trade_data["stop_loss_price"],
                trade_data.get("position_size", 1),
                trade_data.get("product", "MIS"),
                json.dumps(trade_data),
                trade_data.get("decided_by", "manual"),
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def expire_pending_trades(self, max_age_minutes: int = 30) -> int:
        """Expire pending trades older than max_age_minutes.

        Uses both ISO format (2026-04-09T06:00:00+00:00) and SQLite format
        (2026-04-09 06:00:00) for comparison to handle legacy rows. Also
        flips the originating signals' disposition to 'expired' so the
        Today's Recommendations panel doesn't show them as still pending.
        """
        from datetime import timedelta
        cutoff_dt = now_utc() - timedelta(minutes=max_age_minutes)
        # Compare against both formats to handle legacy rows with SQLite datetime('now')
        cutoff_iso = cutoff_dt.isoformat()
        cutoff_sql = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Collect symbols about to be expired before we UPDATE — needed so
        # we can update the corresponding signal disposition rows.
        cur = await self.conn.execute(
            "SELECT symbol FROM pending_trades "
            "WHERE status = 'pending' AND (created_at < ? OR created_at < ?)",
            (cutoff_iso, cutoff_sql),
        )
        expiring_symbols = [r[0] for r in await cur.fetchall()]

        cursor = await self.conn.execute(
            "UPDATE pending_trades SET status = 'expired' "
            "WHERE status = 'pending' AND ("
            "  created_at < ? OR created_at < ?"
            ")",
            (cutoff_iso, cutoff_sql),
        )
        await self.conn.commit()

        for sym in expiring_symbols:
            try:
                await self.update_signal_disposition(
                    sym, "expired", "pending trade auto-expired",
                )
            except Exception:
                pass

        return cursor.rowcount

    async def update_pending_trade_levels(
        self, trade_id: int, *,
        entry_price: float, target_price: float, stop_loss_price: float,
    ) -> bool:
        """Re-anchor a pending trade's price levels in place.

        Used by the per-heartbeat repricer when the underlying LTP
        has drifted but stayed inside the drift band. The row's
        `created_at` is intentionally NOT touched — the pending-age
        expiry timer keeps ticking against the original queue time.
        Returns True when the row was found and still pending.
        """
        cur = await self.conn.execute(
            "UPDATE pending_trades SET "
            "  entry_price = ?, target_price = ?, stop_loss_price = ? "
            "WHERE id = ? AND status = 'pending'",
            (
                round(float(entry_price), 2),
                round(float(target_price), 2),
                round(float(stop_loss_price), 2),
                int(trade_id),
            ),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def expire_pending_trade(
        self, trade_id: int, reason: str,
    ) -> bool:
        """Flip a single pending trade to status='expired' with a
        reason logged via signal disposition. Used by the per-heartbeat
        repricer when the LTP has already moved past target / SL / the
        drift band so the queued levels no longer make sense.
        """
        cur = await self.conn.execute(
            "SELECT symbol FROM pending_trades "
            "WHERE id = ? AND status = 'pending'",
            (int(trade_id),),
        )
        row = await cur.fetchone()
        if not row:
            return False
        await self.conn.execute(
            "UPDATE pending_trades SET status = 'expired', "
            "  decided_by = 'system', decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now_utc().isoformat(), int(trade_id)),
        )
        await self.conn.commit()
        try:
            await self.update_signal_disposition(
                row[0], "expired", f"pending repriced out: {reason}",
            )
        except Exception:
            pass
        return True

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
                cursor = await self.conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                row = await cursor.fetchone()
                count = row[0] if row else 0

                oldest = newest = None
                if count > 0:
                    cursor = await self.conn.execute(
                        f"SELECT MIN({ts_col}), MAX({ts_col}) FROM {table}"  # noqa: S608
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
        from datetime import timedelta

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
            f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)  # noqa: S608
        )
        await self.conn.commit()
        deleted = cursor.rowcount
        logger.info("Manual cleanup: deleted %d rows from %s (older than %d days)", deleted, table, older_than_days)
        return deleted

    # ------------------------------------------------------------------
    # Symbol Quarantine (auto-block after repeated fetch failures)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_transient_fetch_error(error: str) -> bool:
        """Recognise temporary infrastructure failures that shouldn't
        count toward the 3-strike quarantine threshold. A 30-min Kite
        outage with a heartbeat retrying every 15min was previously
        enough to mass-quarantine the entire universe even though no
        symbol was actually broken.
        """
        if not error:
            return False
        msg = error.lower()
        transient_markers = (
            "too many requests", "rate limit", "429",
            "timeout", "timed out",
            "connection reset", "connection refused", "connection aborted",
            "temporarily unavailable", "service unavailable",
            "unreachable", "network is",
            "ssl", "tls",
            "http 5",  # 500, 502, 503, 504 — server-side, retry-safe
            " 502", " 503", " 504",
            # Broker / data-provider auth failures. A logged-out Kite
            # session would otherwise mass-quarantine every symbol of
            # the universe over three consecutive heartbeats — every
            # historical_data() call returns "Incorrect api_key or
            # access_token" but none of those symbols are actually
            # broken.
            "incorrect `api_key`", "incorrect `access_token`",
            "api_key or access_token", "access token", "token expired",
            "tokenexception", "skipping kite call",
            "token previously rejected",
        )
        return any(marker in msg for marker in transient_markers)

    async def record_fetch_failure(self, symbol: str, error: str) -> bool:
        """Record a data fetch failure. Returns True if symbol is now quarantined.

        Transient errors (rate limit, timeouts, 5xx, SSL/network) are
        logged but don't bump the counter — quarantining a symbol
        because Zerodha had a 5-minute outage is exactly the kind of
        silent-fragility this counter is meant to avoid.
        """
        if self._is_transient_fetch_error(error):
            logger.info(
                "Skipping quarantine counter for %s — transient error: %s",
                symbol, error,
            )
            return False
        row = await self.conn.execute(
            "SELECT consecutive_failures FROM quarantined_symbols WHERE symbol = ?",
            (symbol,),
        )
        existing = await row.fetchone()

        threshold = 3
        if existing:
            new_count = existing[0] + 1
            quarantined_at = (
                "datetime('now')" if new_count >= threshold else None
            )
            if new_count >= threshold:
                await self.conn.execute(
                    "UPDATE quarantined_symbols SET "
                    "consecutive_failures = ?, last_error = ?, "
                    "quarantined_at = datetime('now'), updated_at = datetime('now') "
                    "WHERE symbol = ?",
                    (new_count, error, symbol),
                )
            else:
                await self.conn.execute(
                    "UPDATE quarantined_symbols SET "
                    "consecutive_failures = ?, last_error = ?, "
                    "updated_at = datetime('now') "
                    "WHERE symbol = ?",
                    (new_count, error, symbol),
                )
            await self.conn.commit()
            return new_count >= threshold
        else:
            await self.conn.execute(
                "INSERT INTO quarantined_symbols (symbol, consecutive_failures, last_error) "
                "VALUES (?, 1, ?)",
                (symbol, error),
            )
            await self.conn.commit()
            return False

    async def record_fetch_success(self, symbol: str) -> None:
        """Reset failure counter on successful fetch."""
        await self.conn.execute(
            "DELETE FROM quarantined_symbols WHERE symbol = ?",
            (symbol,),
        )
        await self.conn.commit()

    async def is_quarantined(self, symbol: str) -> bool:
        """Check if a symbol is quarantined."""
        cursor = await self.conn.execute(
            "SELECT 1 FROM quarantined_symbols "
            "WHERE symbol = ? AND quarantined_at IS NOT NULL",
            (symbol,),
        )
        return await cursor.fetchone() is not None

    async def get_quarantined_symbols(self) -> list[dict[str, Any]]:
        """Get all quarantined symbols."""
        cursor = await self.conn.execute(
            "SELECT symbol, consecutive_failures, last_error, "
            "quarantined_at, updated_at, replacement_symbol "
            "FROM quarantined_symbols WHERE quarantined_at IS NOT NULL "
            "ORDER BY quarantined_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def unquarantine_symbol(self, symbol: str) -> bool:
        """Remove a symbol from quarantine."""
        cursor = await self.conn.execute(
            "DELETE FROM quarantined_symbols WHERE symbol = ?",
            (symbol,),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_all_quarantined_symbol_set(self) -> set[str]:
        """Get set of quarantined symbols for fast lookup."""
        cursor = await self.conn.execute(
            "SELECT symbol FROM quarantined_symbols WHERE quarantined_at IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def set_replacement_symbol(
        self, quarantined: str, replacement: str | None,
    ) -> bool:
        """Set (or clear) a replacement symbol for a quarantined symbol."""
        cursor = await self.conn.execute(
            "UPDATE quarantined_symbols SET replacement_symbol = ? "
            "WHERE symbol = ? AND quarantined_at IS NOT NULL",
            (replacement.upper() if replacement else None, quarantined.upper()),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_quarantine_replacements(self) -> dict[str, str]:
        """Get mapping of quarantined symbol -> replacement symbol.

        Only includes entries where a replacement is set.
        """
        cursor = await self.conn.execute(
            "SELECT symbol, replacement_symbol FROM quarantined_symbols "
            "WHERE quarantined_at IS NOT NULL AND replacement_symbol IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return {r[0]: r[1] for r in rows}

    async def resolve_symbols_with_replacements(
        self, symbols: list[str],
    ) -> list[str]:
        """Apply quarantine policy to a raw symbol list.

        Used by ingest skills so user-configured swaps actually take effect
        and quarantined symbols don't leak into the pipeline.

        Policy:
          - Symbol is not quarantined → keep as-is.
          - Symbol is quarantined AND has a replacement → use the
            replacement (e.g. ZOMATO -> ETERNAL after the corporate rename).
          - Symbol is quarantined WITHOUT a replacement → drop entirely.
            (Quarantine means data fetch failed 3+ times. Without a
            user-supplied replacement, the symbol shouldn't appear in any
            downstream operation.)

        Output is deduplicated while preserving input order.
        """
        repl = await self.get_quarantine_replacements()
        quarantined = await self.get_all_quarantined_symbol_set()
        seen: set[str] = set()
        out: list[str] = []
        for s in symbols:
            if s in quarantined:
                target = repl.get(s)
                if not target:
                    # Quarantined and no replacement → drop
                    continue
                # Quarantined with replacement → swap
                if target in seen:
                    continue
                seen.add(target)
                out.append(target)
            else:
                if s in seen:
                    continue
                seen.add(s)
                out.append(s)
        return out

    # ------------------------------------------------------------------
    # Dry-Run Signal Preview
    # ------------------------------------------------------------------

    async def _get_table_columns(self, table: str) -> set[str]:
        """Return the set of column names for a table."""
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in await cursor.fetchall()}

    async def insert_dry_run_results(
        self, run_id: str, signals: list[dict[str, Any]]
    ) -> int:
        """Save dry-run signal results for next-day comparison."""
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
        # Optional columns (from migration 013+, 016+)
        optional_cols = [
            "holding_period", "product", "volatility_score",
            "estimated_costs", "strategy_mode", "expected_holding_days",
        ]
        insert_cols = base_cols + [c for c in optional_cols if c in columns]
        placeholders = ", ".join("?" if c != "created_at" else "datetime('now')" for c in insert_cols)
        col_names = ", ".join(insert_cols)
        value_cols = [c for c in insert_cols if c != "created_at"]

        for s in signals:
            values = tuple(
                run_id if c == "run_id"
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
                "MAX(strategy_mode) as strategy_mode "
                "FROM dry_run_results "
                "GROUP BY run_id ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        except Exception:
            # Fallback if strategy_mode column doesn't exist (pre-migration 013)
            cursor = await self.conn.execute(
                "SELECT run_id, COUNT(*) as signal_count, "
                "MIN(created_at) as created_at, "
                "SUM(CASE WHEN direction_correct = 1 THEN 1 ELSE 0 END) as correct, "
                "SUM(CASE WHEN scored_at IS NOT NULL THEN 1 ELSE 0 END) as scored "
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

    async def score_dry_run(self, run_id: str) -> dict[str, Any]:
        """Score a dry-run against actual next-day OHLCV data.

        For each signal, fetch the next trading day's OHLCV and compare.
        Uses date-only comparison to avoid timestamp format mismatches
        (dry-run created_at has time, OHLCV timestamp may not).

        Scoring waits for the next *trading day's* daily bar to exist.
        Three outcomes possible per signal:
          - scored: found and compared
          - same_day: the dry-run was created today (IST) — too early
          - not_found: previous-day dry-run but next-day OHLCV missing
            (most often: today's daily bar hasn't been ingested yet)
        """
        signals = await self.get_dry_run_signals(run_id)
        if not signals:
            return {"scored": 0, "not_found": 0}

        # Compare in IST so a late-evening-IST dry-run (which is the next
        # UTC day) is still recognised as "same trading day" and treated
        # as too-recent-to-score.
        today_ist = now_ist().strftime("%Y-%m-%d")
        already_scored = 0
        scored = 0
        not_found = 0
        same_day = 0
        unfound: list[dict[str, Any]] = []

        for sig in signals:
            if sig.get("scored_at"):
                already_scored += 1
                continue

            # created_at is stored as SQLite datetime('now') (UTC) e.g.
            # "2026-05-15 05:11:30". Convert to IST trading day before
            # comparing.
            raw_created = str(sig["created_at"])
            try:
                # Parse with assumed UTC if no tzinfo present.
                ts = datetime.fromisoformat(raw_created.replace(" ", "T"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                created_date = ts.astimezone(IST).strftime("%Y-%m-%d")
            except Exception:
                created_date = raw_created[:10]

            if created_date >= today_ist:
                same_day += 1
                continue

            # Get the next day's OHLCV after the dry-run date.
            # Use SUBSTR to compare date portions only, avoiding time format issues.
            cursor = await self.read_conn.execute(
                "SELECT open, high, low, close, SUBSTR(timestamp, 1, 10) AS d "
                "FROM ohlcv WHERE symbol = ? AND interval = 'daily' "
                "AND SUBSTR(timestamp, 1, 10) > ? "
                "ORDER BY timestamp ASC LIMIT 1",
                (sig["symbol"], created_date),
            )
            row = await cursor.fetchone()
            if not row:
                not_found += 1
                unfound.append({
                    "symbol": sig["symbol"],
                    "created_date": created_date,
                })
                continue

            actual_open = row[0]
            actual_high = row[1]
            actual_low = row[2]
            actual_close = row[3]
            entry = sig["entry_price"]

            if sig["signal_type"] == "BUY":
                direction_correct = 1 if actual_close > entry else 0
                target_hit = 1 if actual_high >= sig["target_price"] else 0
                actual_move_pct = (actual_close - entry) / entry * 100 if entry else 0
            else:  # SELL
                direction_correct = 1 if actual_close < entry else 0
                target_hit = 1 if actual_low <= sig["target_price"] else 0
                actual_move_pct = (entry - actual_close) / entry * 100 if entry else 0

            await self.conn.execute(
                "UPDATE dry_run_results SET "
                "actual_open = ?, actual_close = ?, actual_high = ?, actual_low = ?, "
                "direction_correct = ?, target_hit = ?, actual_move_pct = ?, "
                "scored_at = datetime('now') "
                "WHERE id = ?",
                (actual_open, actual_close, actual_high, actual_low,
                 direction_correct, target_hit, round(actual_move_pct, 4),
                 sig["id"]),
            )
            scored += 1

        if scored > 0:
            await self.conn.commit()

        result: dict[str, Any] = {
            "scored": scored,
            "not_found": not_found,
            "already_scored": already_scored,
        }
        if same_day > 0:
            result["same_day"] = same_day
            result["message"] = (
                "Signals generated today cannot be scored yet — "
                "next trading day's data is needed. Try again tomorrow."
            )
        if not_found > 0 and not same_day:
            # Tell the user exactly what's missing. Most common cause:
            # today's daily bar hasn't landed in OHLCV yet — daily bars
            # from jugaad / yfinance arrive after market close.
            sample = ", ".join(
                f"{u['symbol']} (created {u['created_date']})"
                for u in unfound[:5]
            )
            if len(unfound) > 5:
                sample += f", +{len(unfound) - 5} more"
            result["unfound"] = unfound
            result["message"] = (
                "Next-day OHLCV not yet in DB for these symbols: "
                + sample
                + ". Daily bars are usually ingested after market close "
                "(~3:30 PM IST); try again later today or tomorrow."
            )
            logger.info(
                "score_dry_run %s: %d signals could not be scored — %s",
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

        async def _delete(table: str, where: str = "", params: tuple = ()) -> int:
            try:
                if where:
                    cursor = await self.conn.execute(
                        f"DELETE FROM {table} WHERE {where}", params,  # noqa: S608
                    )
                else:
                    cursor = await self.conn.execute(f"DELETE FROM {table}")  # noqa: S608
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
                cursor = await self.conn.execute(f"DELETE FROM {table}")  # noqa: S608
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

        # Close current connection before overwriting
        await self.conn.close()

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

        # Reopen connection
        self.conn = await aiosqlite.connect(self._db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")

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

        # Remove .pkl file from disk
        file_deleted = False
        if model_dir:
            pkl_path = Path(model_dir) / f"{version}.pkl"
            if pkl_path.exists():
                pkl_path.unlink()
                file_deleted = True
                logger.info("Deleted model artifact: %s", pkl_path)

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
    # Slippage Stats
    # ------------------------------------------------------------------

    async def get_slippage_stats(
        self, symbol: str | None = None, days: int = 30, mode: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate slippage statistics for feedback into signal generation.

        Returns avg/max/total slippage, per-symbol breakdown, and slippage trend.
        """
        from datetime import timedelta

        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        mc = " AND mode = ?" if mode else ""
        mp: list[Any] = [mode] if mode else []

        if symbol:
            cursor = await self.conn.execute(
                f"SELECT symbol, slippage, entry_price, fill_price, signal_type, created_at "
                f"FROM trades WHERE symbol = ? AND created_at >= ? AND slippage IS NOT NULL{mc}",
                [symbol, cutoff, *mp],
            )
        else:
            cursor = await self.conn.execute(
                f"SELECT symbol, slippage, entry_price, fill_price, signal_type, created_at "
                f"FROM trades WHERE created_at >= ? AND slippage IS NOT NULL{mc}",
                [cutoff, *mp],
            )
        rows = await cursor.fetchall()
        trades = [dict[str, Any](row) for row in rows]

        if not trades:
            return {
                "total_trades": 0,
                "avg_slippage": 0,
                "max_slippage": 0,
                "avg_slippage_pct": 0,
                "by_symbol": {},
            }

        slippages = [t["slippage"] for t in trades]
        slippage_pcts = [
            t["slippage"] / t["entry_price"] if t["entry_price"] > 0 else 0
            for t in trades
        ]

        # Per-symbol breakdown
        by_symbol: dict[str, dict[str, Any]] = {}
        for t in trades:
            sym = t["symbol"]
            if sym not in by_symbol:
                by_symbol[sym] = {"slippages": [], "count": 0}
            by_symbol[sym]["slippages"].append(t["slippage"])
            by_symbol[sym]["count"] += 1

        symbol_stats = {}
        for sym, data in by_symbol.items():
            s_list = data["slippages"]
            symbol_stats[sym] = {
                "count": data["count"],
                "avg_slippage": sum(s_list) / len(s_list),
                "max_slippage": max(s_list),
            }

        return {
            "total_trades": len(trades),
            "avg_slippage": sum(slippages) / len(slippages),
            "max_slippage": max(slippages),
            "avg_slippage_pct": sum(slippage_pcts) / len(slippage_pcts),
            "by_symbol": symbol_stats,
        }

    # ------------------------------------------------------------------
    # LLM Review Accuracy
    # ------------------------------------------------------------------

    async def get_llm_review_accuracy(
        self, days: int = 30, mode: str | None = None,
    ) -> dict[str, Any]:
        """Compare LLM APPROVE/REJECT decisions vs actual trade outcomes.

        Joins llm_reviews with trades to compute:
        - Approval accuracy: % of approved trades that were profitable
        - Rejection value: avg PnL of trades that were rejected (counterfactual)
        - Decision breakdown: approve/reject counts and outcomes
        """
        from datetime import timedelta

        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        mc = " AND t.mode = ?" if mode else ""
        mp: list[Any] = [mode] if mode else []

        # Get reviews with matching trade outcomes
        cursor = await self.conn.execute(
            f"SELECT r.decision, r.reasoning, r.trade_id, r.created_at, "
            f"t.pnl, t.slippage, t.symbol, t.status "
            f"FROM llm_reviews r "
            f"LEFT JOIN trades t ON r.trade_id = t.symbol "
            f"WHERE r.created_at >= ?{mc}",
            [cutoff, *mp],
        )
        rows = await cursor.fetchall()
        reviews = [dict[str, Any](row) for row in rows]

        approved = [r for r in reviews if r.get("decision") == "APPROVE"]
        rejected = [r for r in reviews if r.get("decision") == "REJECT"]

        # Approved trades with PnL data
        approved_with_pnl = [r for r in approved if r.get("pnl") is not None]
        profitable_approvals = [r for r in approved_with_pnl if (r.get("pnl") or 0) > 0]
        losing_approvals = [r for r in approved_with_pnl if (r.get("pnl") or 0) <= 0]

        approval_accuracy = (
            len(profitable_approvals) / len(approved_with_pnl)
            if approved_with_pnl
            else None
        )
        approved_total_pnl = sum(r.get("pnl", 0) for r in approved_with_pnl)
        approved_avg_pnl = (
            approved_total_pnl / len(approved_with_pnl) if approved_with_pnl else 0
        )

        return {
            "total_reviews": len(reviews),
            "approved_count": len(approved),
            "rejected_count": len(rejected),
            "approved_with_outcomes": len(approved_with_pnl),
            "profitable_approvals": len(profitable_approvals),
            "losing_approvals": len(losing_approvals),
            "approval_accuracy": approval_accuracy,
            "approved_total_pnl": approved_total_pnl,
            "approved_avg_pnl": approved_avg_pnl,
        }

    async def get_audit_log(
        self, limit: int = 50, action_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get recent audit log entries."""
        if action_type:
            cursor = await self.conn.execute(
                "SELECT * FROM audit_log WHERE action_type LIKE ? "
                "ORDER BY timestamp_ist DESC LIMIT ?",
                (f"%{action_type}%", limit),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM audit_log ORDER BY timestamp_ist DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

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
    # Symbol Deep-Dive (Feature #3)
    # ------------------------------------------------------------------

    async def get_symbol_trades(self, symbol: str, limit: int = 50, mode: str | None = None) -> list[dict[str, Any]]:
        """All trades for a specific symbol."""
        return await self.get_trades_history(symbol=symbol, limit=limit, mode=mode)

    async def get_symbol_predictions(self, symbol: str, mode: str | None = None) -> list[dict[str, Any]]:
        """Predictions linked to a specific symbol via signals."""
        mc = " AND p.mode = ?" if mode else ""
        mp: list[Any] = [mode] if mode else []
        cursor = await self.read_conn.execute(
            f"SELECT p.*, COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"s.signal_type, s.confidence_score "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"WHERE COALESCE(p.symbol, s.symbol, t.symbol) = ?{mc} "
            f"ORDER BY p.created_at DESC LIMIT 50",
            [symbol, *mp],
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Strategy Performance (Feature #5)
    # ------------------------------------------------------------------

    async def get_strategy_performance(self, mode: str | None = None) -> dict[str, Any]:
        """Aggregate trade performance by signal type, product, sector, time-of-day, holding period."""
        mc = " AND mode = ?" if mode else ""
        mct = " AND t.mode = ?" if mode else ""
        mp: list[Any] = [mode] if mode else []
        result: dict[str, Any] = {}

        # By signal type (BUY vs SELL)
        cursor = await self.conn.execute(
            f"SELECT signal_type, COUNT(*) as cnt, "
            f"SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
            f"COALESCE(SUM(pnl), 0) as total_pnl, "
            f"COALESCE(AVG(pnl), 0) as avg_pnl "
            f"FROM trades WHERE pnl IS NOT NULL{mc} "
            f"GROUP BY signal_type", mp,
        )
        rows = await cursor.fetchall()
        result["by_signal_type"] = [dict[str, Any](r) for r in rows]

        # By product (MIS vs CNC)
        cursor = await self.conn.execute(
            f"SELECT product, COUNT(*) as cnt, "
            f"SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
            f"COALESCE(SUM(pnl), 0) as total_pnl, "
            f"COALESCE(AVG(pnl), 0) as avg_pnl "
            f"FROM trades WHERE pnl IS NOT NULL{mc} "
            f"GROUP BY product", mp,
        )
        rows = await cursor.fetchall()
        result["by_product"] = [dict[str, Any](r) for r in rows]

        # By hour of entry
        cursor = await self.conn.execute(
            f"SELECT CAST(strftime('%H', created_at) AS INTEGER) as hour, "
            f"COUNT(*) as cnt, "
            f"SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
            f"COALESCE(SUM(pnl), 0) as total_pnl, "
            f"COALESCE(AVG(pnl), 0) as avg_pnl "
            f"FROM trades WHERE pnl IS NOT NULL{mc} "
            f"GROUP BY hour ORDER BY hour", mp,
        )
        rows = await cursor.fetchall()
        result["by_hour"] = [dict[str, Any](r) for r in rows]

        # By sector
        cursor = await self.conn.execute(
            f"SELECT COALESCE(w.sector, 'Unknown') as sector, COUNT(*) as cnt, "
            f"SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN t.pnl < 0 THEN 1 ELSE 0 END) as losses, "
            f"COALESCE(SUM(t.pnl), 0) as total_pnl, "
            f"COALESCE(AVG(t.pnl), 0) as avg_pnl "
            f"FROM trades t LEFT JOIN watchlist w ON t.symbol = w.symbol "
            f"WHERE t.pnl IS NOT NULL{mct} "
            f"GROUP BY sector ORDER BY total_pnl DESC", mp,
        )
        rows = await cursor.fetchall()
        result["by_sector"] = [dict[str, Any](r) for r in rows]

        # By holding period bucket
        cursor = await self.conn.execute(
            f"SELECT "
            f"CASE "
            f"  WHEN (julianday(closed_at) - julianday(created_at)) * 24 < 1 THEN '<1h' "
            f"  WHEN (julianday(closed_at) - julianday(created_at)) * 24 < 4 THEN '1-4h' "
            f"  WHEN (julianday(closed_at) - julianday(created_at)) < 1 THEN '4h-1d' "
            f"  ELSE '>1d' "
            f"END as holding_period, "
            f"COUNT(*) as cnt, "
            f"SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
            f"COALESCE(SUM(pnl), 0) as total_pnl, "
            f"COALESCE(AVG(pnl), 0) as avg_pnl "
            f"FROM trades WHERE pnl IS NOT NULL AND closed_at IS NOT NULL{mc} "
            f"GROUP BY holding_period", mp,
        )
        rows = await cursor.fetchall()
        result["by_holding_period"] = [dict[str, Any](r) for r in rows]

        return result

    # ------------------------------------------------------------------
    # Execution Quality (Feature #8)
    # ------------------------------------------------------------------

    async def get_execution_quality(self, days: int = 30, mode: str | None = None) -> dict[str, Any]:
        """Detailed execution quality metrics."""
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        mc = " AND mode = ?" if mode else ""
        mct = " AND t.mode = ?" if mode else ""
        mp: list[Any] = [mode] if mode else []

        # Slippage by hour
        cursor = await self.conn.execute(
            f"SELECT CAST(strftime('%H', created_at) AS INTEGER) as hour, "
            f"COUNT(*) as cnt, "
            f"AVG(ABS(slippage)) as avg_slippage, "
            f"MAX(ABS(slippage)) as max_slippage "
            f"FROM trades WHERE created_at >= ? AND slippage IS NOT NULL{mc} "
            f"GROUP BY hour ORDER BY hour",
            [cutoff, *mp],
        )
        rows = await cursor.fetchall()
        slippage_by_hour = [dict[str, Any](r) for r in rows]

        # Slippage by order size bucket
        cursor = await self.conn.execute(
            f"SELECT "
            f"CASE "
            f"  WHEN quantity * entry_price < 10000 THEN '<10K' "
            f"  WHEN quantity * entry_price < 50000 THEN '10K-50K' "
            f"  WHEN quantity * entry_price < 100000 THEN '50K-1L' "
            f"  ELSE '>1L' "
            f"END as size_bucket, "
            f"COUNT(*) as cnt, "
            f"AVG(ABS(slippage)) as avg_slippage, "
            f"MAX(ABS(slippage)) as max_slippage "
            f"FROM trades WHERE created_at >= ? AND slippage IS NOT NULL{mc} "
            f"GROUP BY size_bucket",
            [cutoff, *mp],
        )
        rows = await cursor.fetchall()
        slippage_by_size = [dict[str, Any](r) for r in rows]

        # Fill rate (% with non-null fill)
        cursor = await self.conn.execute(
            f"SELECT COUNT(*) as total, "
            f"SUM(CASE WHEN fill_price IS NOT NULL AND fill_price > 0 THEN 1 ELSE 0 END) as filled "
            f"FROM trades WHERE created_at >= ?{mc}",
            [cutoff, *mp],
        )
        row = await cursor.fetchone()
        total = row[0] if row else 0
        filled = row[1] if row else 0

        # Order-to-fill latency
        cursor = await self.conn.execute(
            f"SELECT AVG(t.slippage) as avg_slip, "
            f"COUNT(*) as cnt, "
            f"SUM(CASE WHEN ABS(t.slippage) < 0.1 THEN 1 ELSE 0 END) as zero_slip_cnt "
            f"FROM trades t WHERE t.created_at >= ? AND t.slippage IS NOT NULL{mct}",
            [cutoff, *mp],
        )
        row = await cursor.fetchone()

        # Overall stats
        cursor = await self.conn.execute(
            f"SELECT AVG(ABS(slippage)) as avg_abs_slippage, "
            f"MAX(ABS(slippage)) as max_abs_slippage, "
            f"AVG(slippage) as avg_signed_slippage "
            f"FROM trades WHERE created_at >= ? AND slippage IS NOT NULL{mc}",
            [cutoff, *mp],
        )
        overall_row = await cursor.fetchone()

        return {
            "total_orders": total,
            "filled_orders": filled,
            "fill_rate_pct": round(filled / total * 100, 2) if total > 0 else 0,
            "avg_abs_slippage": overall_row[0] if overall_row else 0,
            "max_abs_slippage": overall_row[1] if overall_row else 0,
            "avg_signed_slippage": overall_row[2] if overall_row else 0,
            "zero_slippage_pct": round((row[2] or 0) / (row[1] or 1) * 100, 2) if row else 0,
            "slippage_by_hour": slippage_by_hour,
            "slippage_by_size": slippage_by_size,
        }

    # ------------------------------------------------------------------
    # Correlation Data (Feature #7)
    # ------------------------------------------------------------------

    async def get_ohlcv_multi(self, symbols: list[str], days: int = 60) -> dict[str, list[dict[str, Any]]]:
        """Fetch close prices for multiple symbols for correlation computation."""
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=days)).isoformat()

        result: dict[str, list[dict[str, Any]]] = {}
        for symbol in symbols:
            cursor = await self.conn.execute(
                "SELECT timestamp, close FROM ohlcv "
                "WHERE symbol = ? AND interval = 'daily' AND timestamp >= ? "
                "ORDER BY timestamp ASC",
                (symbol, cutoff),
            )
            rows = await cursor.fetchall()
            result[symbol] = [{"timestamp": r[0], "close": r[1]} for r in rows]
        return result

    # ------------------------------------------------------------------
    # Price Alerts (Feature #4)
    # ------------------------------------------------------------------

    async def create_price_alert(
        self, symbol: str, target_price: float, direction: str, note: str | None = None
    ) -> int:
        """Create a new price alert. Returns the alert ID."""
        cursor = await self.conn.execute(
            "INSERT INTO price_alerts (symbol, target_price, direction, note) "
            "VALUES (?, ?, ?, ?)",
            (symbol.upper(), target_price, direction, note),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def get_price_alerts(self, active_only: bool = True) -> list[dict[str, Any]]:
        """Get price alerts."""
        if active_only:
            cursor = await self.conn.execute(
                "SELECT * FROM price_alerts WHERE active = 1 ORDER BY created_at DESC"
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM price_alerts ORDER BY created_at DESC LIMIT 100"
            )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def delete_price_alert(self, alert_id: int) -> bool:
        """Delete a price alert."""
        cursor = await self.conn.execute(
            "DELETE FROM price_alerts WHERE id = ?", (alert_id,)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def trigger_price_alert(self, alert_id: int) -> None:
        """Mark an alert as triggered."""
        now = now_utc().isoformat()
        await self.conn.execute(
            "UPDATE price_alerts SET active = 0, triggered_at = ? WHERE id = ?",
            (now, alert_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Risk Simulator (Feature #6)
    # ------------------------------------------------------------------

    async def get_historical_signals(
        self,
        limit: int = 200,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch historical signals with their trade outcomes for simulation.

        Args:
            limit: Max signals to return.
            date_from: Optional start date (YYYY-MM-DD, inclusive).
            date_to: Optional end date (YYYY-MM-DD, exclusive).
        """
        query = (
            "SELECT s.*, "
            "COALESCE(t.pnl, CASE "
            "  WHEN t.status = 'closed' AND t.exit_price IS NOT NULL THEN "
            "    CASE WHEN s.signal_type = 'BUY' "
            "      THEN (t.exit_price - t.fill_price) * t.quantity "
            "      ELSE (t.fill_price - t.exit_price) * t.quantity "
            "    END "
            "END, "
            "  (SELECT p2.actual_pnl_pct * s.entry_price * s.position_size "
            "   FROM predictions p2 "
            "   WHERE p2.signal_id = s.id AND p2.actual_pnl_pct IS NOT NULL "
            "   LIMIT 1)"
            ") as pnl, "
            "t.quantity, t.fill_price, t.slippage, t.status as trade_status, "
            "COALESCE(w.sector, 'Unknown') as sector "
            "FROM signals s "
            "LEFT JOIN trades t ON t.trade_id = COALESCE("
            "  (SELECT t2.trade_id FROM trades t2 "
            "   JOIN predictions p ON p.trade_id = t2.trade_id "
            "   WHERE p.signal_id = s.id LIMIT 1),"
            "  (SELECT t3.trade_id FROM trades t3 "
            "   WHERE t3.symbol = s.symbol "
            "   AND t3.signal_type = s.signal_type "
            "   AND t3.created_at >= s.created_at "
            "   AND t3.created_at < datetime(s.created_at, '+1 day') "
            "   ORDER BY t3.created_at ASC LIMIT 1)"
            ") "
            "LEFT JOIN watchlist w ON s.symbol = w.symbol "
            "WHERE 1=1"
        )
        params: list[Any] = []
        if date_from:
            query += " AND s.created_at >= ?"
            params.append(date_from)
        if date_to:
            query += " AND s.created_at < ?"
            params.append(date_to)
        query += " ORDER BY s.created_at ASC LIMIT ?"
        params.append(limit)
        cursor = await self.read_conn.execute(query, tuple(params))
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    # ------------------------------------------------------------------
    # Locked Holdings
    # ------------------------------------------------------------------

    async def _table_exists(self, table: str) -> bool:
        """Check if a table exists in the database."""
        cursor = await self.read_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return await cursor.fetchone() is not None

    async def get_locked_symbols(self) -> set[str]:
        """Return the set of symbols that are locked (should not be sold)."""
        if not await self._table_exists("locked_holdings"):
            return set()
        cursor = await self.read_conn.execute("SELECT symbol FROM locked_holdings")
        rows = await cursor.fetchall()
        return {row[0] for row in rows}

    async def get_locked_holdings(self) -> list[dict[str, Any]]:
        """Return all locked holdings with metadata."""
        if not await self._table_exists("locked_holdings"):
            return []
        cursor = await self.read_conn.execute(
            "SELECT symbol, locked_at, notes FROM locked_holdings ORDER BY locked_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def lock_symbol(self, symbol: str, notes: str | None = None) -> bool:
        """Lock a symbol to prevent YoloVest from selling it."""
        if not await self._table_exists("locked_holdings"):
            # Auto-create if migration hasn't run
            await self.conn.execute(
                "CREATE TABLE IF NOT EXISTS locked_holdings ("
                "symbol TEXT PRIMARY KEY, locked_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "notes TEXT)"
            )
            await self.conn.commit()
        await self.conn.execute(
            "INSERT OR REPLACE INTO locked_holdings (symbol, locked_at, notes) "
            "VALUES (?, datetime('now'), ?)",
            (symbol.upper(), notes),
        )
        await self.conn.commit()
        return True

    async def unlock_symbol(self, symbol: str) -> bool:
        """Unlock a symbol, allowing YoloVest to sell it again."""
        if not await self._table_exists("locked_holdings"):
            return False
        cursor = await self.conn.execute(
            "DELETE FROM locked_holdings WHERE symbol = ?",
            (symbol.upper(),),
        )
        await self.conn.commit()
        return cursor.rowcount > 0
