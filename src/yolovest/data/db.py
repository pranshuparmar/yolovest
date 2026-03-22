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

from yolovest.models.schemas import NewsArticle, OHLCVBar, SentimentResult

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
            # Use explicit transaction for atomicity (M1 fix).
            # executescript runs implicit commits which break atomicity.
            await self.conn.execute("BEGIN")
            try:
                for stmt in self._split_sql(sql):
                    await self.conn.execute(stmt)
                await self.conn.execute(
                    "INSERT INTO schema_version (version, filename) VALUES (?, ?)",
                    (version, mf.name),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
            logger.info("Migration %s applied", mf.name)

    @staticmethod
    def _split_sql(sql: str) -> list[str]:
        """Split SQL text into individual statements, skipping empty ones."""
        return [s.strip() for s in sql.split(";") if s.strip()]

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
        from datetime import timedelta

        cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()
        cursor = await self.conn.execute(
            "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
            "WHERE symbol = ? AND interval = ? "
            "AND timestamp >= ? "
            "ORDER BY timestamp ASC",
            (symbol, interval, cutoff),
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
        """Replace watchlist with new scored stocks (atomic)."""
        await self.conn.execute("BEGIN")
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
        *,
        auto_commit: bool = True,
    ) -> None:
        """Log an audit entry for decision traceability.

        Set auto_commit=False when batching multiple audit entries,
        then call flush_audit() to commit them all at once.
        """
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
        if auto_commit:
            await self.conn.commit()

    async def flush_audit(self) -> None:
        """Commit any pending audit log entries."""
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Pre-market Data (Phase 2, FR-2.10)
    # ------------------------------------------------------------------

    async def upsert_premarket(self, data: dict) -> None:
        """Insert or update today's pre-market context."""
        from datetime import date

        today = date.today().isoformat()
        gift = data.get("gift_nifty", {})
        us = data.get("us_markets", {})
        llm = data.get("llm_summary")
        # Extract bias from LLM summary if it's a WebGroundingResult-like object
        bias = None
        summary_text = None
        if hasattr(llm, "summary"):
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

    async def get_latest_premarket(self) -> dict:
        """Get the most recent pre-market context."""
        cursor = await self.conn.execute(
            "SELECT * FROM premarket ORDER BY date DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}

    # ------------------------------------------------------------------
    # Sentiment (Phase 2, FR-2.7)
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

    async def get_sentiment(self, symbol: str) -> SentimentResult | None:
        """Get latest sentiment for a symbol."""
        cursor = await self.conn.execute(
            "SELECT symbol, sentiment, confidence, key_drivers FROM sentiment WHERE symbol = ?",
            (symbol,),
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
    # News Articles (Phase 2, FR-2.13)
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
                pass  # Skip duplicates silently
        await self.conn.commit()
        return inserted

    # ------------------------------------------------------------------
    # Signals (Phase 2, FR-4.5)
    # ------------------------------------------------------------------

    async def insert_signal(self, signal: dict) -> None:
        """Persist a generated signal."""
        await self.conn.execute(
            "INSERT INTO signals (symbol, signal_type, entry_price, target_price, "
            "stop_loss_price, position_size, confidence_score, model_version, "
            "features_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
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
            ),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Fundamentals (Phase 2, FR-2.4)
    # ------------------------------------------------------------------

    async def upsert_fundamentals(self, symbol: str, data: dict) -> None:
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

    # ------------------------------------------------------------------
    # NSE Universe (Phase 2, FR-3.1)
    # ------------------------------------------------------------------

    async def get_nse_universe(self) -> list[dict]:
        """Get all symbols with OHLCV data, enriched with sentiment and fundamentals.

        Returns dicts with sub-scores for market-scan scoring.
        """
        cursor = await self.conn.execute(
            "SELECT o.symbol, "
            "  AVG(o.volume) as avg_daily_volume, "
            "  s.sentiment, s.confidence as sentiment_confidence, "
            "  f.pe_ratio, f.debt_to_equity, f.promoter_holding_pct, "
            "  w.sector "
            "FROM ohlcv o "
            "LEFT JOIN sentiment s ON o.symbol = s.symbol "
            "LEFT JOIN fundamentals f ON o.symbol = f.symbol "
            "LEFT JOIN watchlist w ON o.symbol = w.symbol "
            "WHERE o.interval = 'daily' "
            "GROUP BY o.symbol"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Model Versions (Phase 2, FR-7.7)
    # ------------------------------------------------------------------

    async def save_model_version(
        self, model_type: str, version: str, file_path: str, metrics: dict
    ) -> None:
        """Save a new model version record."""
        await self.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, "
            "sharpe_ratio, max_drawdown_pct, win_rate, profit_factor, "
            "status, shadow_start_date) VALUES (?, ?, ?, ?, ?, ?, ?, 'shadow', datetime('now'))",
            (
                model_type,
                version,
                file_path,
                metrics.get("sharpe_ratio"),
                metrics.get("max_drawdown_pct"),
                metrics.get("win_rate"),
                metrics.get("profit_factor"),
            ),
        )
        await self.conn.commit()

    async def get_production_model(self, model_type: str) -> dict | None:
        """Get the current production model for a model type."""
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions "
            "WHERE model_type = ? AND status = 'production' "
            "ORDER BY created_at DESC LIMIT 1",
            (model_type,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def promote_model(self, model_type: str, version: str) -> None:
        """Promote a shadow model to production, retire the current production."""
        await self.conn.execute("BEGIN")
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

    # ------------------------------------------------------------------
    # Training Data (Phase 2, FR-7.4)
    # ------------------------------------------------------------------

    async def get_training_dataset(self) -> dict:
        """Load OHLCV data for model training."""
        cursor = await self.conn.execute(
            "SELECT symbol, timestamp, open, high, low, close, volume "
            "FROM ohlcv WHERE interval = 'daily' ORDER BY symbol, timestamp"
        )
        rows = await cursor.fetchall()
        return {"bars": [dict(row) for row in rows]}

    async def get_prediction_outcomes(self) -> list[dict]:
        """Load predictions with actual outcomes for retraining analysis."""
        cursor = await self.conn.execute(
            "SELECT * FROM predictions WHERE actual_price IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Failure Analysis (Phase 2, FR-7.6)
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
