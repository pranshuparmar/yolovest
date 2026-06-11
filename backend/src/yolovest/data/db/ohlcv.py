"""OHLCV bars: upsert, fetch, delivery %.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from yolovest.data.db.core import (
    _canonical_ohlcv_ts,
    _source_priority_sql,
)
from yolovest.models.schemas import OHLCVBar
from yolovest.timezone import now_ist, now_utc

logger = logging.getLogger(__name__)


class OhlcvMixin:
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
            "ingested_at=datetime('now') "
            # Only overwrite when the incoming source is at least as trusted
            # as the stored one — so a later yfinance fetch can't clobber a
            # kite bar, and the day's bar doesn't flip-flop by ingest order.
            f"WHERE {_source_priority_sql('excluded.source')} "
            f">= {_source_priority_sql('ohlcv.source')}",
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

        end_dt = end or now_utc()
        cutoff = (end_dt - timedelta(days=days)).isoformat()
        # For daily bars, inline the interval as a literal rather than a bound
        # parameter. SQLite only uses a `WHERE interval='daily'` partial index
        # when it can prove the predicate at compile time, which a bound `?`
        # defeats. The literal lets the covering index idx_ohlcv_daily_covering
        # serve the query without per-row heap fetches for OHLCV columns.
        if interval == "daily":
            query = (
                "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
                "WHERE symbol = ? AND interval = 'daily' AND timestamp >= ? "
            )
            params: list[Any] = [symbol, cutoff]
        else:
            query = (
                "SELECT timestamp, open, high, low, close, volume FROM ohlcv "
                "WHERE symbol = ? AND interval = ? AND timestamp >= ? "
            )
            params = [symbol, interval, cutoff]
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
