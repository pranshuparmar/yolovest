"""Slippage stats, LLM accuracy, symbol deep-dive, strategy performance, execution quality, correlations, price alerts, risk simulator.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import logging
from datetime import timedelta
from typing import Any

from yolovest.timezone import now_utc

logger = logging.getLogger(__name__)


class FeatureQueriesMixin:
    # Slippage Stats
    # ------------------------------------------------------------------

    async def get_slippage_stats(
        self, symbol: str | None = None, days: int = 30, mode: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate slippage statistics for feedback into signal generation.

        Returns avg/max/total slippage, per-symbol breakdown, and slippage trend.
        """

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
