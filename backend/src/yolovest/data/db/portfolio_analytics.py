"""Failure analysis, portfolio state, LLM review log, sector rotation, regime/beta/trend computations.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)


class PortfolioAnalyticsMixin:
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
                # Accumulate, don't overwrite: a symbol can have more than one
                # open row (e.g. an adopted holding plus a system position in
                # the same name), and the single-stock-exposure gate must see
                # their combined exposure, not just the last row's.
                stock_exposures[symbol] = (
                    stock_exposures.get(symbol, 0.0) + (qty * entry) / total_capital
                )

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
