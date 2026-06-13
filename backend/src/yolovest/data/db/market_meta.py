"""Symbol sectors, fundamentals, NSE universe, per-stock sector lookup.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import logging
from datetime import timedelta
from typing import Any

from yolovest.timezone import now_utc

logger = logging.getLogger(__name__)


class MarketMetaMixin:
    # Order-book depth snapshots (intraday order-flow dataset)
    # ------------------------------------------------------------------

    async def insert_depth_snapshots(
        self, ts: str, rows: dict[str, dict[str, Any]],
    ) -> int:
        """Persist one heartbeat's batched depth quotes. `rows` is
        {symbol: payload} from the Kite batch quote. INSERT OR REPLACE
        so a retried heartbeat at the same instant is idempotent."""
        if not rows:
            return 0
        payload = [
            (
                ts, symbol, q.get("ltp"), q.get("bid"), q.get("ask"),
                q.get("total_buy_qty"), q.get("total_sell_qty"),
                q.get("top5_buy_qty"), q.get("top5_sell_qty"),
                q.get("volume"),
            )
            for symbol, q in rows.items()
        ]
        await self.conn.executemany(
            "INSERT OR REPLACE INTO depth_snapshots "
            "(ts, symbol, ltp, bid, ask, total_buy_qty, total_sell_qty, "
            "top5_buy_qty, top5_sell_qty, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def prune_depth_snapshots(self, keep_days: int) -> int:
        """Trim snapshots older than `keep_days` (self-maintaining)."""
        cutoff = (now_utc() - timedelta(days=keep_days)).isoformat()
        cursor = await self.conn.execute(
            "DELETE FROM depth_snapshots WHERE ts < ?", (cutoff,),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0)


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
