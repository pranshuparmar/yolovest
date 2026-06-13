"""Reports + dashboard aggregate queries.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import json
import logging
from datetime import timedelta
from typing import Any

from yolovest.timezone import now_utc

logger = logging.getLogger(__name__)


class ReportsDashboardMixin:
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
        # model_version: stamped on the trade row at execution (migration
        # 050). The signal's version rides along under a UNIQUE alias —
        # selecting it as "model_version" would duplicate the t.* column
        # name and dict(row) would resolve to the raw (NULL-for-legacy)
        # one — and is coalesced in Python below so legacy rows still
        # resolve while their signal exists.
        query = (
            "SELECT t.*, s.model_version AS signal_model_version "
            "FROM trades t "
            "LEFT JOIN signals s ON s.id = t.signal_id WHERE 1=1"
        )
        params: list[Any] = []

        if mode:
            query += " AND t.mode = ?"
            params.append(mode)
        if start_date:
            query += " AND t.created_at >= ?"
            params.append(start_date)
        if end_date:
            # end_date is inclusive: add one day as exclusive upper bound.
            # This avoids the T23:59:59 hack which misses the last second.
            from datetime import date, timedelta
            next_day = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
            query += " AND t.created_at < ?"
            params.append(next_day)
        if symbol:
            # Substring match (case-insensitive) so the Trades page search
            # acts like a filter rather than an exact-symbol picker —
            # typing "REL" matches RELIANCE, RELINFRA, etc. SQLite LIKE
            # is already case-insensitive for ASCII; symbol names are
            # ASCII so no need for unicode-aware collation.
            query += " AND t.symbol LIKE ?"
            params.append(f"%{symbol}%")

        query += " ORDER BY t.created_at DESC LIMIT ?"
        params.append(limit)

        cursor = await self.read_conn.execute(query, params)
        rows = await cursor.fetchall()
        out = []
        for row in rows:
            d = dict[str, Any](row)
            d["model_version"] = (
                d.get("model_version") or d.pop("signal_model_version", None)
            )
            d.pop("signal_model_version", None)
            out.append(d)
        return out

    async def get_equity_curve(self, days: int = 30, mode: str | None = None) -> list[dict[str, Any]]:
        """Compute daily equity curve from closed trades.

        Returns a list of {date, cumulative_pnl, trade_count} entries.
        """

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
        # Trade record. The producing signal's model_version rides along
        # under a unique alias and is coalesced in Python so legacy rows
        # (pre-migration-050) resolve while their signal exists.
        cursor = await self.conn.execute(
            "SELECT t.*, s.model_version AS signal_model_version "
            "FROM trades t "
            "LEFT JOIN signals s ON s.id = t.signal_id "
            "WHERE t.trade_id = ?",
            (trade_id,),
        )
        trade_row = await cursor.fetchone()
        if not trade_row:
            return None

        trade = dict[str, Any](trade_row)
        trade["model_version"] = (
            trade.get("model_version") or trade.pop("signal_model_version", None)
        )
        trade.pop("signal_model_version", None)

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
