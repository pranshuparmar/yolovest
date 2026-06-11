"""Algo + user watchlists and rotation-cooldown stats.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class WatchlistMixin:
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
