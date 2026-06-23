"""Open positions, today's trades, trade lifecycle, weekly data.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import json
import logging
from datetime import UTC, timedelta
from typing import Any

from yolovest.data.db.core import (
    DuplicateSignalError,
)
from yolovest.timezone import now_ist, now_utc

logger = logging.getLogger(__name__)


class TradesMixin:
    # Positions (read from trades table)
    # ------------------------------------------------------------------

    async def get_open_positions(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get trades with status 'open' or 'partially_filled'.

        Args:
            mode: Filter by trading mode ('paper' or 'live'). None = all modes.
        """
        query = "SELECT * FROM trades WHERE status IN ('open', 'partially_filled')"
        params: list[Any] = []
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        # Newest position first — without this the table renders in rowid
        # (oldest-opened) order, inconsistent with the rest of the app.
        # All 23 callers iterate/aggregate, so the order is display-only.
        query += " ORDER BY created_at DESC"
        cursor = await self.read_conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    # ------------------------------------------------------------------
    # Today's Trades
    # ------------------------------------------------------------------

    async def get_todays_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get all trades created today (IST market day)."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        query = "SELECT * FROM trades WHERE created_at >= ?"
        params: list[Any] = [today_start]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY created_at"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_closed_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get trades that were closed today, regardless of when they were created."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        query = "SELECT * FROM trades WHERE closed_at >= ? AND status = 'closed'"
        params: list[Any] = [today_start]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY closed_at"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_signals_count(self) -> int:
        """Count signals generated today."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at >= ?",
            (today_start,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_signal_class_counts(
        self, days: int = 7, mode: str | None = None,
    ) -> dict[str, Any]:
        """Count signals per type (BUY/SELL/HOLD) in the last `days`.

        Returns:
          {
            "BUY": int, "SELL": int, "HOLD": int, "total": int,
            "by_day": [{"date": "YYYY-MM-DD", "BUY": int, ...}, ...],
          }

        Used by drift-watch (alert on a class going extinct) and the
        Dashboard signal-distribution widget. Mode-scoped when
        `mode` is provided so paper- and live-mode signals don't pool.
        """
        from datetime import timedelta as _td

        cutoff = (now_utc() - _td(days=days)).isoformat()
        params: list[Any] = [cutoff]
        mode_clause = ""
        if mode:
            mode_clause = " AND mode = ?"
            params.append(mode)

        cur = await self.read_conn.execute(
            f"SELECT signal_type, DATE(created_at) AS d, COUNT(*) "
            f"FROM signals WHERE created_at >= ?{mode_clause} "
            f"GROUP BY signal_type, d ORDER BY d ASC",
            tuple(params),
        )
        rows = await cur.fetchall()

        totals = {"BUY": 0, "SELL": 0, "HOLD": 0}
        per_day: dict[str, dict[str, int]] = {}
        for sig_type, d, count in rows:
            key = (sig_type or "").upper()
            if key not in totals:
                continue
            totals[key] += int(count)
            per_day.setdefault(d, {"BUY": 0, "SELL": 0, "HOLD": 0})
            per_day[d][key] = int(count)
        by_day = [
            {"date": d, **counts}
            for d, counts in sorted(per_day.items())
        ]
        return {
            "BUY": totals["BUY"],
            "SELL": totals["SELL"],
            "HOLD": totals["HOLD"],
            "total": sum(totals.values()),
            "by_day": by_day,
        }

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
    # Trade Management
    # ------------------------------------------------------------------

    async def insert_trade(self, trade: dict[str, Any]) -> str:
        """Insert a new trade record atomically with audit log.

        Uses a savepoint so the trade insert + audit entry either both
        succeed or both roll back — no orphaned records on crash.
        """
        import uuid

        trade_id = trade.get("trade_id") or f"T-{uuid.uuid4().hex[:8]}"
        ts_now = now_utc().isoformat()

        trade_columns = await self._get_table_columns("trades")

        # Build column list dynamically based on available columns
        base_cols = [
            "trade_id", "symbol", "signal_type", "entry_price", "fill_price",
            "quantity", "stop_loss_price", "target_price", "order_id", "sl_order_id",
            "product", "mode", "status", "slippage",
        ]
        optional_cols = [
            "estimated_costs", "expected_holding_days", "signal_id",
            "model_version",
        ]
        insert_cols = base_cols + [c for c in optional_cols if c in trade_columns] + ["created_at"]
        placeholders = ", ".join("?" for _ in insert_cols)
        col_names = ", ".join(insert_cols)

        values = tuple(
            trade.get(c, trade.get("entry_price") if c == "fill_price" else None)
            if c not in ("trade_id", "created_at", "product", "mode", "status", "slippage")
            else {
                "trade_id": trade_id,
                "created_at": ts_now,
                "product": trade.get("product", "MIS"),
                "mode": trade.get("mode", "paper"),
                "status": trade.get("status", "open"),
                "slippage": trade.get("slippage", 0.0),
            }[c]
            for c in insert_cols
        )

        # Idempotency pre-check. The trades.signal_id UNIQUE index
        # (migration 042) is the hard guarantee; this SELECT is the
        # graceful-error path so callers get a DuplicateSignalError
        # instead of a raw IntegrityError. trade-execute is the only
        # writer that sets signal_id and it processes signals
        # sequentially per heartbeat, so the TOCTOU window between
        # this SELECT and the INSERT below is effectively zero. If a
        # parallel writer ever appears, the UNIQUE index still catches
        # the duplicate — the caller just sees an IntegrityError
        # bubble up instead of the typed DuplicateSignalError.
        sig_id = trade.get("signal_id")
        if sig_id:
            cur = await self.read_conn.execute(
                "SELECT trade_id FROM trades WHERE signal_id = ?",
                (int(sig_id),),
            )
            row = await cur.fetchone()
            if row:
                raise DuplicateSignalError(
                    signal_id=int(sig_id),
                    existing_trade_id=str(row[0]),
                )

        await self.conn.execute("SAVEPOINT insert_trade")
        try:
            await self.conn.execute(
                f"INSERT INTO trades ({col_names}) VALUES ({placeholders})",
                values,
            )
            await self.conn.execute(
                "INSERT INTO audit_log (timestamp_ist, action_type, skill_name, "
                "input_summary, output_summary) VALUES (?, ?, ?, ?, ?)",
                (
                    ts_now, "trade_inserted", "trade-execute",
                    json.dumps({
                        "trade_id": trade_id, "symbol": trade["symbol"],
                        "signal_type": trade["signal_type"],
                        "mode": trade.get("mode", "paper"),
                    }),
                    json.dumps({
                        "fill_price": trade.get("fill_price"),
                        "quantity": trade["quantity"],
                        "slippage": trade.get("slippage", 0.0),
                    }),
                ),
            )
            await self.conn.execute("RELEASE SAVEPOINT insert_trade")
            await self.conn.commit()
        except Exception:
            await self.conn.execute("ROLLBACK TO SAVEPOINT insert_trade")
            raise
        return trade_id

    async def update_position_sl(self, position_id: int | str, new_sl: float) -> None:
        """Update stop-loss price for an open position."""
        await self.conn.execute(
            "UPDATE trades SET stop_loss_price = ? WHERE trade_id = ?",
            (new_sl, str(position_id)),
        )
        await self.conn.commit()

    async def upsert_funds_snapshot(
        self,
        snapshot_date: str,
        mode: str,
        summary: dict[str, float],
        raw_json: str | None = None,
        holdings_invested: float = 0.0,
        holdings_current: float = 0.0,
    ) -> None:
        """Insert today's funds/margins snapshot (or replace if it
        already exists for the same date+mode).

        Called by the funds-snapshot CRON skill so the user can track
        daily cash movements without logging into Kite.
        """
        from yolovest.timezone import now_utc as _now_utc

        captured_at = _now_utc().isoformat()
        keys_in_order = (
            "available_cash", "live_balance", "opening_balance",
            "utilised_margin", "m2m_unrealised", "m2m_realised",
            "payout", "collateral", "exposure", "span", "delivery", "net",
        )
        params: list[Any] = [
            snapshot_date, captured_at, mode,
        ]
        params.extend(float(summary.get(k, 0.0) or 0.0) for k in keys_in_order)
        params.extend([float(holdings_invested), float(holdings_current), raw_json])

        col_list = (
            "snapshot_date, captured_at, mode, " + ", ".join(keys_in_order)
            + ", holdings_invested, holdings_current, raw_json"
        )
        placeholders = ", ".join(["?"] * (3 + len(keys_in_order) + 3))
        await self.conn.execute(
            f"INSERT INTO funds_snapshots ({col_list}) VALUES ({placeholders}) "
            "ON CONFLICT(snapshot_date, mode) DO UPDATE SET "
            "captured_at=excluded.captured_at, "
            + ", ".join(f"{k}=excluded.{k}" for k in keys_in_order)
            + ", holdings_invested=excluded.holdings_invested"
            + ", holdings_current=excluded.holdings_current"
            + ", raw_json=excluded.raw_json",
            params,
        )
        await self.conn.commit()

    async def get_funds_snapshots(
        self, mode: str | None = None, days: int = 90,
    ) -> list[dict[str, Any]]:
        """Return funds snapshots (newest first) for the last N days.

        Mode-scoped when supplied; paper / live snapshots are stored
        independently so flipping modes doesn't corrupt either history.
        """
        from datetime import date as _date
        from datetime import timedelta as _td
        since = (_date.today() - _td(days=days)).isoformat()
        query = (
            "SELECT id, snapshot_date, captured_at, mode, available_cash, "
            "live_balance, opening_balance, utilised_margin, m2m_unrealised, "
            "m2m_realised, payout, collateral, exposure, span, delivery, net, "
            "holdings_invested, holdings_current "
            "FROM funds_snapshots WHERE snapshot_date >= ?"
        )
        params: list[Any] = [since]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY snapshot_date DESC, captured_at DESC"
        cur = await self.read_conn.execute(query, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def increment_realized_partial_pnl(
        self, position_id: int | str, partial_pnl: float,
    ) -> None:
        """Add a partial-booking PnL to the trade's running total.

        Each partial close is recorded individually in audit_log (with
        exit_qty, exit_price, etc.); the running sum lives on trades
        so reads don't need a JOIN. The final close (full exit of the
        remainder) populates `pnl` separately; UI surfaces
        total = realized_partial_pnl + pnl.
        """
        await self.conn.execute(
            "UPDATE trades "
            "SET realized_partial_pnl = COALESCE(realized_partial_pnl, 0) + ? "
            "WHERE trade_id = ?",
            (float(partial_pnl), str(position_id)),
        )
        await self.conn.commit()

    async def update_position_quantity(
        self, position_id: int | str, new_quantity: int,
    ) -> None:
        """Resize an open position after a partial close.

        Used by the user-initiated partial-close endpoint. The full
        close path goes through `close_position` instead (which sets
        status='closed'); this one keeps status='open' with the
        remaining quantity. Callers are expected to have already
        resized any broker-side SL / target / GTT to match.
        """
        await self.conn.execute(
            "UPDATE trades SET quantity = ? WHERE trade_id = ?",
            (int(new_quantity), str(position_id)),
        )
        await self.conn.commit()

    async def update_unrealized_pnl(self, position_id: int | str, current_price: float) -> None:
        """Update unrealized PnL for an open position based on current price.

        Note: PnL is stored as NULL while position is open; this updates
        a computed field or can be used for tracking in audit_log.
        """
        # Log unrealized PnL as audit entry for tracking
        await self.log_audit(
            action_type="unrealized_pnl_update",
            input_summary={"position_id": str(position_id), "current_price": current_price},
        )

    async def close_position(
        self,
        position_id: int | str,
        exit_price: float,
        pnl: float,
        realized_costs: dict[str, Any] | None = None,
    ) -> bool:
        """Close a position with exit price, realized PnL, and an optional
        breakdown of the actual charges applied (brokerage/stt/other/total
        plus a `source` of "broker" or "estimate").

        Uses a savepoint to ensure the trade update and audit log are
        committed atomically — no half-closed positions.

        Idempotent: the UPDATE is guarded by `status != 'closed'`, so a
        second close (e.g. a manual close racing position-monitor's exit,
        or a duplicated postback) is a no-op that returns ``False`` instead
        of overwriting the recorded exit price / PnL with a second value
        and writing a duplicate audit row. Returns ``True`` only when this
        call is the one that actually closed the row.
        """
        ts_now = now_utc().isoformat()
        pos_id = str(position_id)
        costs_json = json.dumps(realized_costs) if realized_costs else None
        await self.conn.execute("SAVEPOINT close_position")
        try:
            cursor = await self.conn.execute(
                "UPDATE trades SET status = 'closed', exit_price = ?, pnl = ?, "
                "closed_at = ?, realized_costs_json = COALESCE(?, realized_costs_json) "
                "WHERE trade_id = ? AND status != 'closed'",
                (exit_price, pnl, ts_now, costs_json, pos_id),
            )
            if cursor.rowcount == 0:
                # Already closed (or unknown id) — idempotent no-op. Skip the
                # audit row + sentinel cleanup so a duplicate close leaves no
                # second trail, then release the savepoint cleanly.
                await self.conn.execute("RELEASE SAVEPOINT close_position")
                await self.conn.commit()
                return False
            await self.conn.execute(
                "INSERT INTO audit_log (timestamp_ist, action_type, skill_name, "
                "input_summary, output_summary) VALUES (?, ?, ?, ?, ?)",
                (
                    ts_now, "position_closed", "position-monitor",
                    json.dumps({"trade_id": pos_id, "exit_price": exit_price}),
                    json.dumps({"pnl": pnl}),
                ),
            )
            # Per-trade sentinels in system_state (e.g. partial_booked_{id})
            # outlive the position they describe and have no TTL — clean
            # them up here so system_state doesn't grow monotonically.
            await self.conn.execute(
                "DELETE FROM system_state WHERE key = ?",
                (f"partial_booked_{pos_id}",),
            )
            await self.conn.execute("RELEASE SAVEPOINT close_position")
            await self.conn.commit()
            return True
        except Exception:
            await self.conn.execute("ROLLBACK TO SAVEPOINT close_position")
            raise

    # ------------------------------------------------------------------
    # Weekly Data
    # ------------------------------------------------------------------

    async def get_weekly_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get trades for the current week (Monday-Friday)."""

        now = now_ist()
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        ).astimezone(UTC)
        query = "SELECT * FROM trades WHERE created_at >= ?"
        params: list[Any] = [monday.isoformat()]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY created_at"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_weekly_predictions(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get predictions for the current week."""

        now = now_ist()
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        ).astimezone(UTC)
        mode_clause = " AND p.mode = ?" if mode else ""
        mode_params: list[Any] = [mode] if mode else []
        cursor = await self.conn.execute(
            f"SELECT p.*, COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"s.confidence_score "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"WHERE p.created_at >= ?{mode_clause} ORDER BY p.created_at",
            [monday.isoformat(), *mode_params],
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_weekly_llm_reviews(self) -> list[dict[str, Any]]:
        """Get LLM reviews for the current week with linked trade PnL."""

        now = now_ist()
        days_since_monday = now.weekday()
        monday = (now - timedelta(days=days_since_monday)).replace(
            hour=9, minute=15, second=0, microsecond=0
        ).astimezone(UTC)
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
