"""Symbol quarantine + locked holdings.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class QuarantineMixin:
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
