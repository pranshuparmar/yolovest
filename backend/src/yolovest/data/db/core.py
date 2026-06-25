"""SQLite database layer with WAL mode and migration support.

The concrete DB layer injected into AppContext.db. Uses aiosqlite for async access.
Schema versioned via numbered SQL migration files in migrations/ directory.
"""

import asyncio
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

UTC = UTC
from pathlib import Path
from typing import Any, cast

import aiosqlite

from yolovest.timezone import UTC

logger = logging.getLogger(__name__)

# Default migrations directory (relative to project root)
# NOTE: parents[4] (not [3]) — this file moved one level deeper when the
# monolithic db.py became the db/ package.
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "migrations"

# When True for the current async context, read queries are routed to a
# dedicated dashboard read connection instead of the shared engine read
# connection. The dashboard's HTTP middleware sets this per-request so the
# UI's (trivial) reads never queue behind a heartbeat skill that is draining
# hundreds of serialized OHLCV reads through the engine connection.
DASHBOARD_READ_CONN: ContextVar[bool] = ContextVar(
    "yolovest_dashboard_read_conn", default=False,
)


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


# Provider trust ranking for OHLCV upserts. On a key conflict (same bar from
# a different provider), the higher-priority source wins REGARDLESS of
# ingestion order — so yfinance can never clobber a kite bar, and the day's
# bar doesn't flip-flop with whichever provider ran last. kite (paid,
# authoritative broker data) is the source of truth; yfinance (questionable
# NSE adjustment) is lowest. Unknown sources fall to 0 and won't overwrite a
# known source's bar.
_SOURCE_PRIORITY: dict[str, int] = {
    "kite": 7,
    "bhavcopy": 6,
    "jugaad": 5,
    "tvdatafeed": 4,
    "backfill": 3,
    "ingester": 3,
    "universe": 2,
    "yfinance": 1,
    "yfinance_vix": 1,
}


def _source_priority_sql(col: str) -> str:
    """Build a CASE expression mapping a source column to its trust rank.
    Source keys are hardcoded constants (no user input), so embedding is
    injection-safe."""
    whens = " ".join(f"WHEN '{s}' THEN {p}" for s, p in _SOURCE_PRIORITY.items())
    return f"(CASE {col} {whens} ELSE 0 END)"


# Statement prefixes that open / continue a write transaction. A statement
# starting with any of these acquires the write-serialization lock; everything
# else (SELECT / PRAGMA / EXPLAIN / VACUUM / WITH…SELECT) is treated as a read
# and runs lock-free. NOTE: there are deliberately NO CTE-prefixed writes
# (`WITH … INSERT`) in this codebase — if one is ever added it must be
# classified here, or its write won't be serialized. Classifying a *read* as a
# write would be worse (a SELECT never commits, so the lock would never
# release), which is why WITH stays on the read side.
_WRITE_PREFIXES = (
    "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
    "SAVEPOINT", "RELEASE", "ROLLBACK", "BEGIN",
)


def _is_write_sql(sql: str) -> bool:
    """True if `sql` begins a write/transaction-control statement."""
    head = sql.lstrip()[:12].upper()
    return any(head.startswith(p) for p in _WRITE_PREFIXES)


class _SerializedWriteConnection:
    """Serializes write transactions on the single shared write connection.

    The app reaches one aiosqlite write connection from many concurrent
    actors (heartbeat skills, FastAPI endpoints, the Telegram bot, the
    order-postback handler, KiteTicker callbacks). Under deferred isolation
    the first write auto-opens ONE connection-global transaction that the
    next ``commit()``/``rollback()`` ends — so without coordination, two
    coroutines that interleave their ``execute``/``commit`` calls can bleed
    one transaction's writes into another's commit, or have a rollback
    discard a third party's in-flight write.

    This wrapper acquires an ``asyncio.Lock`` on the FIRST write statement of
    a transaction and releases it on ``commit()``/``rollback()``. Ownership is
    keyed by the running ``asyncio.Task`` so the lock is re-entrant within a
    single multi-statement write method (and across SAVEPOINT/RELEASE), while
    being mutually exclusive across actors. Pure reads (SELECT/PRAGMA) never
    take the lock, so dashboard reads on this connection aren't blocked.

    A task-done callback is the safety net: if a task acquires the lock but
    dies before commit/rollback (unhandled exception after ``ROLLBACK TO
    SAVEPOINT``, cancellation), the lock is released when the task finishes
    rather than deadlocking every future writer.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None

    async def _begin(self) -> None:
        task = asyncio.current_task()
        if self._owner is task and task is not None:
            return  # already inside this task's transaction — re-entrant
        await self._lock.acquire()
        self._owner = task
        if task is not None:
            task.add_done_callback(self._on_owner_done)

    def _end(self) -> None:
        task = asyncio.current_task()
        if self._owner is not task:
            return  # not this task's transaction to end
        self._owner = None
        if task is not None:
            task.remove_done_callback(self._on_owner_done)
        self._lock.release()

    def _on_owner_done(self, task: "asyncio.Task[Any]") -> None:
        if self._owner is task:
            self._owner = None
            self._lock.release()

    async def execute(self, sql: str, parameters: Any = None) -> Any:
        if _is_write_sql(sql):
            await self._begin()
        if parameters is None:
            return await self._conn.execute(sql)
        return await self._conn.execute(sql, parameters)

    async def executemany(self, sql: str, parameters: Any) -> Any:
        await self._begin()
        return await self._conn.executemany(sql, parameters)

    async def commit(self) -> None:
        try:
            await self._conn.commit()
        finally:
            self._end()

    async def rollback(self) -> None:
        try:
            await self._conn.rollback()
        finally:
            self._end()

    def __getattr__(self, name: str) -> Any:
        # Delegate everything else (cursor(), row_factory, in_transaction, …)
        # to the real connection. _conn is set in __init__ so this never
        # recurses for it.
        return getattr(self._conn, name)


class DatabaseCore:
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
        # Write-serialization wrapper over _conn (set in initialize). All
        # writes funnel through this so transactions on the single shared
        # write connection can't interleave across concurrent actors.
        self._write_proxy: _SerializedWriteConnection | None = None
        self._read_conn: aiosqlite.Connection | None = None
        # Dedicated read connection for the dashboard, so UI reads don't
        # queue behind the engine's heartbeat reads on _read_conn.
        self._dashboard_read_conn: aiosqlite.Connection | None = None
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
        # Wrap the write connection so every write (including the migrations
        # below) is serialized per-transaction. PRAGMAs are issued on the raw
        # connection above/below; they're reads to the proxy anyway.
        self._write_proxy = _SerializedWriteConnection(self._conn)

        # -- Durability & concurrency hardening --
        # WAL mode: concurrent reads during writes, crash-safe journal
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Sync WAL to disk on every commit (FULL = safest, ~2x slower than NORMAL)
        await self._conn.execute("PRAGMA synchronous=FULL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Wait up to 5s for locks instead of failing immediately with SQLITE_BUSY
        await self._conn.execute("PRAGMA busy_timeout=5000")

        # NOTE: the integrity check is intentionally NOT run here. On a
        # multi-GB DB, PRAGMA quick_check scans the whole file from disk
        # (minutes) and the result was advisory only — it never aborted
        # startup — so it was pure boot tax. It now runs off the boot path,
        # nightly inside the database-maintenance CRON (see check_integrity).

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

        # Dashboard read connection — a second read-only connection reserved
        # for the HTTP dashboard. aiosqlite serializes every query on a
        # connection through one worker thread, so a heartbeat skill draining
        # hundreds of OHLCV reads on _read_conn would otherwise head-of-line
        # block the UI's trivial reads. WAL allows any number of concurrent
        # readers, so a dedicated connection keeps the dashboard responsive.
        if self._read_conn is not None:
            try:
                self._dashboard_read_conn = await aiosqlite.connect(
                    f"file:{self._db_path}?mode=ro", uri=True,
                )
                self._dashboard_read_conn.row_factory = aiosqlite.Row
                await self._dashboard_read_conn.execute("PRAGMA busy_timeout=5000")
            except Exception as e:
                logger.warning(
                    "Dashboard read connection failed (%s), sharing engine read connection", e,
                )
                self._dashboard_read_conn = None

        await self._run_migrations()
        logger.info(
            "Database initialized at %s (read_conn=%s, dashboard_read_conn=%s)",
            self._db_path,
            "enabled" if self._read_conn else "disabled",
            "enabled" if self._dashboard_read_conn else "disabled",
        )

    async def check_integrity(self) -> str:
        """Run `PRAGMA quick_check` and return its result ("ok" or the first
        corruption message, "unknown" if no row, "error: ..." if it raised).

        Uses quick_check (B-tree structure, no per-page cross-checks) rather
        than integrity_check, but on a multi-GB DB it still scans the whole
        file from disk — so this is NOT run on the startup path. The
        database-maintenance CRON calls it nightly (off-hours), where a
        minutes-long full read is acceptable. Runs on the read connection so
        it never holds the write connection. Logs critical on failure; the
        caller is responsible for alerting.
        """
        try:
            cursor = await self.read_conn.execute("PRAGMA quick_check")
            row = await cursor.fetchone()
            result = row[0] if row else "unknown"
            if result != "ok":
                logger.critical(
                    "DATABASE INTEGRITY CHECK FAILED: %s — "
                    "data may be corrupted. Take a backup immediately.",
                    result,
                )
            else:
                logger.info("Database integrity check passed")
            return result
        except Exception as e:
            logger.warning("Database integrity check could not run: %s", e)
            return f"error: {e}"

    async def close(self) -> None:
        """Close all database connections."""
        if self._dashboard_read_conn:
            await self._dashboard_read_conn.close()
            self._dashboard_read_conn = None
        if self._read_conn:
            await self._read_conn.close()
            self._read_conn = None
        if self._conn:
            await self._conn.close()
            self._conn = None
        # Drop the write proxy too so `conn` raises after close (the proxy
        # holds a now-closed connection otherwise).
        self._write_proxy = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Write connection — use for INSERT/UPDATE/DELETE.

        Returns the write-serialization proxy (not the raw aiosqlite
        connection) so concurrent writers can't interleave transactions on
        the single shared connection. Quacks like aiosqlite.Connection.
        """
        if self._write_proxy is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return cast("aiosqlite.Connection", self._write_proxy)

    @property
    def read_conn(self) -> aiosqlite.Connection:
        """Read connection — use for SELECT queries.

        When the current async context is a dashboard HTTP request
        (DASHBOARD_READ_CONN set by the dashboard middleware), routes to the
        dedicated dashboard read connection so the UI never queues behind the
        engine's heartbeat reads. Falls back to the engine read connection,
        and finally to the write connection if no read connection is available
        (e.g., in-memory databases or older SQLite without URI support).
        """
        if DASHBOARD_READ_CONN.get() and self._dashboard_read_conn is not None:
            return self._dashboard_read_conn
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
