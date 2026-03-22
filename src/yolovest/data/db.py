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

from typing import Any

from yolovest.models.schemas import EconomicEvent, NewsArticle, OHLCVBar, SentimentResult

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

    async def upsert_watchlist(self, stocks: list[dict[str, Any]]) -> None:
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

    async def get_watchlist(self) -> list[dict[str, Any]]:
        """Get current watchlist ordered by composite score."""
        cursor = await self.conn.execute(
            "SELECT symbol, composite_score, technical_score, volume_momentum_score, "
            "news_sentiment_score, fundamental_score, sector, updated_at "
            "FROM watchlist ORDER BY composite_score DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Positions (read from trades table)
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """Get trades with status 'open' or 'partially_filled'."""
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE status IN ('open', 'partially_filled')"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Audit Log (NFR-5)
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
    # Economic Calendar (FR-2.6)
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
                pass  # Skip duplicates silently
        await self.conn.commit()
        return inserted

    async def get_upcoming_economic_events(
        self, days: int = 7, country: str | None = None, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get economic events within the next N days, optionally filtered."""
        from datetime import timedelta

        today = datetime.now(IST).date().isoformat()
        end = (datetime.now(IST).date() + timedelta(days=days)).isoformat()

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

        today = datetime.now(IST).date().isoformat()
        end = (datetime.now(IST).date() + timedelta(days=days)).isoformat()

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
    # Signals (Phase 2, FR-4.5)
    # ------------------------------------------------------------------

    async def insert_signal(self, signal: dict[str, Any]) -> None:
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

    # ------------------------------------------------------------------
    # NSE Universe (Phase 2, FR-3.1)
    # ------------------------------------------------------------------

    async def get_nse_universe(self) -> list[dict[str, Any]]:
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
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Model Versions (Phase 2, FR-7.7)
    # ------------------------------------------------------------------

    async def save_model_version(
        self, model_type: str, version: str, file_path: str, metrics: dict[str, Any]
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

    async def get_training_dataset(self) -> dict[str, Any]:
        """Load OHLCV data for model training."""
        cursor = await self.conn.execute(
            "SELECT symbol, timestamp, open, high, low, close, volume "
            "FROM ohlcv WHERE interval = 'daily' ORDER BY symbol, timestamp"
        )
        rows = await cursor.fetchall()
        return {"bars": [dict[str, Any](row) for row in rows]}

    async def get_prediction_outcomes(self) -> list[dict[str, Any]]:
        """Load predictions with actual outcomes for retraining analysis."""
        cursor = await self.conn.execute(
            "SELECT * FROM predictions WHERE actual_price IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

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

    # ------------------------------------------------------------------
    # Portfolio State (Phase 3, FR-5.1)
    # ------------------------------------------------------------------

    async def get_portfolio_state(self) -> dict[str, Any]:
        """Build portfolio state dict[str, Any] for risk checks.

        Computes total capital, exposure, per-stock/sector counts,
        daily/weekly PnL, trades today, and time since last loss.
        """
        from datetime import timedelta

        now = datetime.now(IST)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        # Get initial capital from system_state or fallback
        initial_capital = 100_000.0
        cap_row = await self.get_system_state("initial_capital")
        if cap_row:
            try:
                initial_capital = float(cap_row)
            except (ValueError, TypeError):
                pass

        # Open positions
        positions = await self.get_open_positions()
        open_count = len(positions)

        # Stock exposures and sector counts
        stock_exposures: dict[str, float] = {}
        sector_counts: dict[str, int] = {}
        total_position_value = 0.0

        for pos in positions:
            symbol = pos.get("symbol", "")
            qty = pos.get("quantity", 0)
            entry = pos.get("entry_price", 0)
            value = qty * entry
            total_position_value += value

            sector = pos.get("sector") or await self.get_stock_sector(symbol)
            if sector:
                sector_counts[sector] = sector_counts.get(sector, 0) + 1

        # FR-9.1: total_capital = initial + all realized PnL
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE pnl IS NOT NULL"
        )
        row = await cursor.fetchone()
        all_time_pnl = row[0] if row else 0
        total_capital = initial_capital + all_time_pnl

        if total_capital > 0:
            for pos in positions:
                symbol = pos.get("symbol", "")
                qty = pos.get("quantity", 0)
                entry = pos.get("entry_price", 0)
                stock_exposures[symbol] = (qty * entry) / total_capital

        exposure_pct = total_position_value / total_capital if total_capital > 0 else 0
        available_cash = total_capital - total_position_value

        # Today's trades count
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE created_at >= ?",
            (today_start,),
        )
        row = await cursor.fetchone()
        trades_today = row[0] if row else 0

        # Daily realized PnL
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE closed_at >= ? AND pnl IS NOT NULL",
            (today_start,),
        )
        row = await cursor.fetchone()
        daily_pnl = row[0] if row else 0
        daily_pnl_pct = daily_pnl / total_capital if total_capital > 0 else 0

        # Weekly realized PnL (since Monday 9:15 AM)
        days_since_monday = now.weekday()  # 0=Monday
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE closed_at >= ? AND pnl IS NOT NULL",
            (monday.isoformat(),),
        )
        row = await cursor.fetchone()
        weekly_pnl = row[0] if row else 0
        weekly_pnl_pct = weekly_pnl / total_capital if total_capital > 0 else 0

        # Minutes since last loss
        cursor = await self.conn.execute(
            "SELECT closed_at FROM trades "
            "WHERE pnl IS NOT NULL AND pnl < 0 "
            "ORDER BY closed_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row and row[0]:
            last_loss_time = datetime.fromisoformat(row[0])
            if last_loss_time.tzinfo is None:
                last_loss_time = last_loss_time.replace(tzinfo=IST)
            minutes_since_last_loss = (now - last_loss_time).total_seconds() / 60
        else:
            minutes_since_last_loss = 999.0  # no losses yet

        return {
            "total_capital": total_capital,
            "available_cash": available_cash,
            "exposure_pct": exposure_pct,
            "open_positions": open_count,
            "stock_exposures": stock_exposures,
            "sector_counts": sector_counts,
            "daily_pnl_pct": daily_pnl_pct,
            "weekly_pnl_pct": weekly_pnl_pct,
            "trades_today": trades_today,
            "minutes_since_last_loss": minutes_since_last_loss,
        }

    # ------------------------------------------------------------------
    # Stock Sector (Phase 3, FR-5.13)
    # ------------------------------------------------------------------

    async def get_stock_sector(self, symbol: str) -> str | None:
        """Get sector for a symbol from watchlist."""
        cursor = await self.conn.execute(
            "SELECT sector FROM watchlist WHERE symbol = ?", (symbol,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    # ------------------------------------------------------------------
    # LLM Review Log (Phase 3, FR-5.11)
    # ------------------------------------------------------------------

    async def log_llm_review(
        self,
        signal: dict[str, Any],
        decision: str,
        reasoning: str,
        adjusted_size: int | None = None,
    ) -> None:
        """Log an LLM trade review for audit trail (FR-8.8)."""
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
    # Sector Rotation (Phase 3, FR-3.5)
    # ------------------------------------------------------------------

    async def get_sector_rotation(self) -> dict[str, Any]:
        """Get sector rotation data from watchlist scores."""
        cursor = await self.conn.execute(
            "SELECT sector, AVG(composite_score) as avg_score, COUNT(*) as count "
            "FROM watchlist WHERE sector IS NOT NULL "
            "GROUP BY sector ORDER BY avg_score DESC"
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
    # Today's Trades (Phase 3, FR-5)
    # ------------------------------------------------------------------

    async def get_todays_trades(self) -> list[dict[str, Any]]:
        """Get all trades created today."""
        today_start = datetime.now(IST).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE created_at >= ? ORDER BY created_at",
            (today_start,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

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
    # Trade Management (Phase 3, FR-6)
    # ------------------------------------------------------------------

    async def insert_trade(self, trade: dict[str, Any]) -> None:
        """Insert a new trade record."""
        import uuid

        trade_id = trade.get("trade_id") or f"T-{uuid.uuid4().hex[:8]}"
        now_ist = datetime.now(IST).isoformat()

        await self.conn.execute(
            "INSERT INTO trades (trade_id, symbol, signal_type, entry_price, fill_price, "
            "quantity, stop_loss_price, target_price, order_id, sl_order_id, product, "
            "mode, status, slippage, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id,
                trade["symbol"],
                trade["signal_type"],
                trade["entry_price"],
                trade.get("fill_price", trade["entry_price"]),
                trade["quantity"],
                trade["stop_loss_price"],
                trade["target_price"],
                trade.get("order_id"),
                trade.get("sl_order_id"),
                trade.get("product", "MIS"),
                trade.get("mode", "paper"),
                trade.get("status", "open"),
                trade.get("slippage", 0.0),
                now_ist,
            ),
        )
        await self.conn.commit()

    async def update_position_sl(self, position_id: int | str, new_sl: float) -> None:
        """Update stop-loss price for an open position."""
        await self.conn.execute(
            "UPDATE trades SET stop_loss_price = ? WHERE trade_id = ?",
            (new_sl, str(position_id)),
        )
        await self.conn.commit()

    async def update_unrealized_pnl(self, position_id: int | str, current_price: float) -> None:
        """Update unrealized PnL for an open position based on current price.

        Note: PnL is stored as NULL while position is open; this updates
        a computed field or can be used for tracking in audit_log.
        """
        now_ist = datetime.now(IST).isoformat()
        # Log unrealized PnL as audit entry for tracking
        await self.log_audit(
            action_type="unrealized_pnl_update",
            input_summary={"position_id": str(position_id), "current_price": current_price},
        )

    async def close_position(
        self, position_id: int | str, exit_price: float, pnl: float
    ) -> None:
        """Close a position with exit price and realized PnL."""
        now_ist = datetime.now(IST).isoformat()
        await self.conn.execute(
            "UPDATE trades SET status = 'closed', exit_price = ?, pnl = ?, closed_at = ? "
            "WHERE trade_id = ?",
            (exit_price, pnl, now_ist, str(position_id)),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Predictions (Phase 4, FR-7.1)
    # ------------------------------------------------------------------

    async def insert_prediction(self, prediction: dict[str, Any]) -> str:
        """Insert a new prediction and return its ID."""
        import uuid

        pred_id = prediction.get("prediction_id") or f"P-{uuid.uuid4().hex[:8]}"
        now_ist = datetime.now(IST).isoformat()

        # Compute prediction end time from holding period
        from yolovest.models.schemas import _parse_holding_period

        holding = prediction.get("expected_holding_period", "intraday")
        end_time = datetime.now(IST) + _parse_holding_period(holding)

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
            "INSERT INTO predictions (prediction_id, signal_id, trade_id, created_at, "
            "prediction_end_time, actual_price, direction_correct, target_hit, "
            "actual_pnl_pct) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            (pred_id, signal_id, prediction.get("trade_id"), now_ist, end_time.isoformat()),
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

    async def get_unscored_predictions(self) -> list[dict[str, Any]]:
        """Get predictions whose holding period has elapsed but haven't been scored."""
        now_ist = datetime.now(IST).isoformat()
        cursor = await self.conn.execute(
            "SELECT p.prediction_id as id, p.trade_id, p.created_at, "
            "p.prediction_end_time, "
            "s.symbol, s.signal_type as predicted_direction, "
            "s.entry_price, s.target_price as predicted_target, "
            "s.stop_loss_price as predicted_stop_loss, "
            "s.confidence_score as confidence, "
            "s.model_version "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
            "WHERE p.actual_price IS NULL "
            "AND p.prediction_end_time <= ? "
            "ORDER BY p.created_at",
            (now_ist,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

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
            "target_hit = ?, actual_pnl_pct = ? WHERE prediction_id = ?",
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
        """Rebuild the prediction scoreboard (FR-7.3).

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
                sum(p.get("confidence", 0) for p in preds) / total if total > 0 else 0
            )
            target_hits = sum(1 for p in preds if p.get("target_hit"))
            target_rate = target_hits / total if total > 0 else 0
            avg_pnl = (
                sum(p.get("actual_pnl_pct", 0) for p in preds) / total
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
            "p.actual_pnl_pct, s.symbol, s.confidence_score as confidence, "
            "s.model_version "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
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

    async def get_todays_predictions(self) -> list[dict[str, Any]]:
        """Get predictions created today."""
        today_start = datetime.now(IST).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        cursor = await self.conn.execute(
            "SELECT p.*, s.symbol, s.signal_type, s.confidence_score "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
            "WHERE p.created_at >= ? ORDER BY p.created_at",
            (today_start,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Weekly Data (Phase 4, FR-8.5)
    # ------------------------------------------------------------------

    async def get_weekly_trades(self) -> list[dict[str, Any]]:
        """Get trades for the current week (Monday-Friday)."""
        from datetime import timedelta

        now = datetime.now(IST)
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        cursor = await self.conn.execute(
            "SELECT * FROM trades WHERE created_at >= ? ORDER BY created_at",
            (monday.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_weekly_predictions(self) -> list[dict[str, Any]]:
        """Get predictions for the current week."""
        from datetime import timedelta

        now = datetime.now(IST)
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        cursor = await self.conn.execute(
            "SELECT p.*, s.symbol, s.signal_type, s.confidence_score "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
            "WHERE p.created_at >= ? ORDER BY p.created_at",
            (monday.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_weekly_llm_reviews(self) -> list[dict[str, Any]]:
        """Get LLM reviews for the current week with linked trade PnL."""
        from datetime import timedelta

        now = datetime.now(IST)
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
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
    # Reports (Phase 4, FR-8.4/8.5)
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

        cutoff = (datetime.now(IST) - timedelta(days=shadow_mode_days)).isoformat()
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions "
            "WHERE status = 'shadow' AND shadow_start_date <= ?",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Dashboard Queries (Phase 5, FR-8)
    # ------------------------------------------------------------------

    async def get_trades_history(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get trade history with optional filters (FR-8.7)."""
        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []

        if start_date:
            query += " AND created_at >= ?"
            params.append(start_date)
        if end_date:
            query += " AND created_at <= ?"
            params.append(end_date + "T23:59:59")
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_equity_curve(self, days: int = 30) -> list[dict[str, Any]]:
        """Compute daily equity curve from closed trades (FR-8.1).

        Returns a list of {date, cumulative_pnl, trade_count} entries.
        """
        from datetime import timedelta

        cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()
        cursor = await self.conn.execute(
            "SELECT DATE(closed_at) as trade_date, "
            "SUM(pnl) as daily_pnl, COUNT(*) as trade_count "
            "FROM trades "
            "WHERE closed_at >= ? AND pnl IS NOT NULL "
            "GROUP BY DATE(closed_at) "
            "ORDER BY trade_date",
            (cutoff,),
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

    async def get_trade_detail(self, trade_id: str) -> dict[str, Any] | None:
        """Get full trade detail with reasoning chain (FR-8.3).

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
        """Get historical reports with optional filters (FR-8.7)."""
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

        cursor = await self.conn.execute(query, params)
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
    # Backup & Retention (FR-10.2, FR-10.3)
    # ------------------------------------------------------------------

    async def backup(self, backup_dir: str) -> str:
        """Create a timestamped backup of the database (FR-10.2)."""
        import shutil

        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        backup_path = str(Path(backup_dir) / f"yolovest_{timestamp}.db")
        # Use SQLite backup API via a checkpoint first
        await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(self._db_path, backup_path)
        logger.info("Database backup created: %s", backup_path)
        return backup_path

    async def run_retention_cleanup(
        self, ohlcv_days: int = 730, audit_days: int = 365, predictions_days: int = 365
    ) -> dict[str, Any]:
        """Delete data older than retention periods (FR-10.3)."""
        from datetime import timedelta

        now = datetime.now(IST)
        deleted = {}

        # OHLCV retention
        cutoff = (now - timedelta(days=ohlcv_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM ohlcv WHERE timestamp < ?", (cutoff,)
        )
        deleted["ohlcv"] = cursor.rowcount

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

        await self.conn.commit()
        logger.info("Retention cleanup: %s", deleted)
        return deleted

    # ------------------------------------------------------------------
    # Slippage Stats (FR-6.7)
    # ------------------------------------------------------------------

    async def get_slippage_stats(
        self, symbol: str | None = None, days: int = 30
    ) -> dict[str, Any]:
        """Aggregate slippage statistics for feedback into signal generation.

        Returns avg/max/total slippage, per-symbol breakdown, and slippage trend.
        """
        from datetime import timedelta

        cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()

        if symbol:
            cursor = await self.conn.execute(
                "SELECT symbol, slippage, entry_price, fill_price, signal_type, created_at "
                "FROM trades WHERE symbol = ? AND created_at >= ? AND slippage IS NOT NULL",
                (symbol, cutoff),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT symbol, slippage, entry_price, fill_price, signal_type, created_at "
                "FROM trades WHERE created_at >= ? AND slippage IS NOT NULL",
                (cutoff,),
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
    # LLM Review Accuracy (FR-7.8)
    # ------------------------------------------------------------------

    async def get_llm_review_accuracy(
        self, days: int = 30
    ) -> dict[str, Any]:
        """Compare LLM APPROVE/REJECT decisions vs actual trade outcomes.

        Joins llm_reviews with trades to compute:
        - Approval accuracy: % of approved trades that were profitable
        - Rejection value: avg PnL of trades that were rejected (counterfactual)
        - Decision breakdown: approve/reject counts and outcomes
        """
        from datetime import timedelta

        cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()

        # Get reviews with matching trade outcomes
        cursor = await self.conn.execute(
            "SELECT r.decision, r.reasoning, r.trade_id, r.created_at, "
            "t.pnl, t.slippage, t.symbol, t.status "
            "FROM llm_reviews r "
            "LEFT JOIN trades t ON r.trade_id = t.symbol "
            "WHERE r.created_at >= ?",
            (cutoff,),
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
        """Get recent audit log entries (FR-8.8)."""
        if action_type:
            cursor = await self.conn.execute(
                "SELECT * FROM audit_log WHERE action_type = ? "
                "ORDER BY timestamp_ist DESC LIMIT ?",
                (action_type, limit),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM audit_log ORDER BY timestamp_ist DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]
