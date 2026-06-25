"""Manual-approval pending-trade queue.

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


class PendingTradesMixin:
    # Pending Trades (manual approval queue)
    # ------------------------------------------------------------------

    async def insert_pending_trade(self, signal: dict[str, Any]) -> int:
        """Queue a trade signal for manual approval. Returns the pending trade ID.

        Caller should set `mode` on the signal dict so bulk-delete and
        per-mode listings can scope correctly.
        """
        ts_now = now_utc().isoformat()
        cursor = await self.conn.execute(
            "INSERT INTO pending_trades "
            "(symbol, signal_type, entry_price, target_price, stop_loss_price, "
            "position_size, confidence_score, model_version, product, signal_data, "
            "mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal.get("symbol"),
                signal.get("signal_type"),
                signal.get("entry_price"),
                signal.get("target_price"),
                signal.get("stop_loss_price"),
                signal.get("position_size"),
                signal.get("confidence_score", signal.get("confidence")),
                signal.get("model_version"),
                signal.get("product", "MIS"),
                json.dumps(signal),
                signal.get("mode", "paper"),
                ts_now,
            ),
        )
        await self.conn.commit()
        pending_id = cursor.lastrowid or 0
        logger.info(
            "Inserted pending trade #%d: %s %s @ %.2f (created_at=%s)",
            pending_id, signal.get("signal_type"), signal.get("symbol"),
            signal.get("entry_price", 0), ts_now,
        )
        return pending_id

    async def get_pending_trades(self) -> list[dict[str, Any]]:
        """Get all pending trades awaiting approval.

        Surfaces the holding-period fields (stored inside `signal_data`) at
        the top level so the UI can show the expected hold / target date
        without re-parsing the signal JSON.
        """
        cursor = await self.conn.execute(
            "SELECT * FROM pending_trades WHERE status = 'pending' "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict[str, Any](r)
            if "expected_holding_days" not in d or d.get("expected_holding_days") is None:
                try:
                    sig = json.loads(d.get("signal_data") or "{}")
                    d["expected_holding_days"] = sig.get("expected_holding_days")
                    d["expected_holding_period"] = sig.get("expected_holding_period")
                except (ValueError, TypeError):
                    d["expected_holding_days"] = None
                    d["expected_holding_period"] = None
            out.append(d)
        return out

    async def get_pending_trade_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Get a pending trade by symbol (case-insensitive). Returns None if not found."""
        cursor = await self.read_conn.execute(
            "SELECT * FROM pending_trades WHERE status = 'pending' "
            "AND UPPER(symbol) = UPPER(?) ORDER BY created_at DESC LIMIT 1",
            (symbol,),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def was_recently_rejected(self, symbol: str, signal_type: str, hours: int = 4) -> bool:
        """Check if a symbol+signal_type was rejected within the last N hours.

        Used to prevent re-queuing the same trade right after user rejects it.
        """
        cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT 1 FROM pending_trades "
            "WHERE status = 'rejected' AND UPPER(symbol) = UPPER(?) "
            "AND signal_type = ? AND decided_at >= ? "
            "LIMIT 1",
            (symbol, signal_type, cutoff),
        )
        return await cursor.fetchone() is not None

    async def decide_pending_trade(
        self, trade_id: int, decision: str, decided_by: str,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Approve or reject a pending trade. Returns the signal data if approved."""
        cursor = await self.conn.execute(
            "SELECT * FROM pending_trades WHERE id = ? AND status = 'pending'",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        if decision == "approved" and overrides:
            # Store user overrides in dedicated columns and flag as override
            override_cols = {
                "signal_type": "user_signal_type",
                "entry_price": "user_entry_price",
                "target_price": "user_target_price",
                "stop_loss_price": "user_stop_loss_price",
                "product": "user_product",
                "notes": "user_notes",
            }
            set_parts = [
                "status = ?", "decided_by = ?", "decided_at = datetime('now')",
                "is_override = 1",
            ]
            params: list[Any] = [decision, decided_by]
            for key, col in override_cols.items():
                if key in overrides:
                    set_parts.append(f"{col} = ?")
                    params.append(overrides[key])
            params.append(trade_id)
            cursor = await self.conn.execute(
                f"UPDATE pending_trades SET {', '.join(set_parts)} "
                "WHERE id = ? AND status = 'pending'",
                tuple(params),
            )
        else:
            cursor = await self.conn.execute(
                "UPDATE pending_trades SET status = ?, decided_by = ?, "
                "decided_at = datetime('now') WHERE id = ? AND status = 'pending'",
                (decision, decided_by, trade_id),
            )
        await self.conn.commit()

        # The guarded UPDATE — not the SELECT above — is the real gate. Two
        # concurrent approvers (dashboard double-click, or dashboard +
        # Telegram /approve) can both pass the SELECT while the row is still
        # 'pending', but only the first UPDATE matches. rowcount == 0 means we
        # lost the race and the trade was already decided — return None so the
        # caller doesn't execute the same trade twice.
        if cursor.rowcount == 0:
            return None

        if decision == "rejected":
            # Reflect the rejection on the originating signal row so the
            # Today's Recommendations panel updates.
            try:
                await self.update_signal_disposition(
                    row["symbol"], "rejected", f"rejected by {decided_by}",
                )
            except Exception:
                pass

        if decision == "approved":
            signal_data = row["signal_data"]
            signal = json.loads(signal_data) if signal_data else dict[str, Any](row)
            # Apply overrides to the returned signal dict
            if overrides:
                for key in ("signal_type", "entry_price", "target_price",
                            "stop_loss_price", "product", "position_size", "notes"):
                    if key in overrides:
                        signal[key] = overrides[key]
                signal["is_override"] = True
            return signal
        return None

    async def insert_manual_trade(self, trade_data: dict[str, Any]) -> int:
        """Insert a manually initiated trade (not from ML prediction)."""
        cursor = await self.conn.execute(
            "INSERT INTO pending_trades "
            "(symbol, signal_type, entry_price, target_price, stop_loss_price, "
            "position_size, product, signal_data, status, decided_by, decided_at, is_manual) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?, datetime('now'), 1)",
            (
                trade_data["symbol"],
                trade_data["signal_type"],
                trade_data["entry_price"],
                trade_data["target_price"],
                trade_data["stop_loss_price"],
                trade_data.get("position_size", 1),
                trade_data.get("product", "MIS"),
                json.dumps(trade_data),
                trade_data.get("decided_by", "manual"),
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def expire_pending_trades(self, max_age_minutes: int = 30) -> int:
        """Expire pending trades older than max_age_minutes.

        Uses both ISO format (2026-04-09T06:00:00+00:00) and SQLite format
        (2026-04-09 06:00:00) for comparison to handle legacy rows. Also
        flips the originating signals' disposition to 'expired' so the
        Today's Recommendations panel doesn't show them as still pending.
        """
        cutoff_dt = now_utc() - timedelta(minutes=max_age_minutes)
        # Compare against both formats to handle legacy rows with SQLite datetime('now')
        cutoff_iso = cutoff_dt.isoformat()
        cutoff_sql = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Collect symbols about to be expired before we UPDATE — needed so
        # we can update the corresponding signal disposition rows.
        cur = await self.conn.execute(
            "SELECT symbol FROM pending_trades "
            "WHERE status = 'pending' AND (created_at < ? OR created_at < ?)",
            (cutoff_iso, cutoff_sql),
        )
        expiring_symbols = [r[0] for r in await cur.fetchall()]

        cursor = await self.conn.execute(
            "UPDATE pending_trades SET status = 'expired' "
            "WHERE status = 'pending' AND ("
            "  created_at < ? OR created_at < ?"
            ")",
            (cutoff_iso, cutoff_sql),
        )
        await self.conn.commit()

        for sym in expiring_symbols:
            try:
                await self.update_signal_disposition(
                    sym, "expired", "pending trade auto-expired",
                )
            except Exception:
                pass

        return cursor.rowcount

    async def update_pending_trade_levels(
        self, trade_id: int, *,
        entry_price: float, target_price: float, stop_loss_price: float,
    ) -> bool:
        """Re-anchor a pending trade's price levels in place.

        Used by the per-heartbeat repricer when the underlying LTP
        has drifted but stayed inside the drift band. The row's
        `created_at` is intentionally NOT touched — the pending-age
        expiry timer keeps ticking against the original queue time.
        Returns True when the row was found and still pending.
        """
        cur = await self.conn.execute(
            "UPDATE pending_trades SET "
            "  entry_price = ?, target_price = ?, stop_loss_price = ? "
            "WHERE id = ? AND status = 'pending'",
            (
                round(float(entry_price), 2),
                round(float(target_price), 2),
                round(float(stop_loss_price), 2),
                int(trade_id),
            ),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def expire_pending_trade(
        self, trade_id: int, reason: str,
    ) -> bool:
        """Flip a single pending trade to status='expired' with a
        reason logged via signal disposition. Used by the per-heartbeat
        repricer when the LTP has already moved past target / SL / the
        drift band so the queued levels no longer make sense.
        """
        cur = await self.conn.execute(
            "SELECT symbol FROM pending_trades "
            "WHERE id = ? AND status = 'pending'",
            (int(trade_id),),
        )
        row = await cur.fetchone()
        if not row:
            return False
        await self.conn.execute(
            "UPDATE pending_trades SET status = 'expired', "
            "  decided_by = 'system', decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now_utc().isoformat(), int(trade_id)),
        )
        await self.conn.commit()
        try:
            await self.update_signal_disposition(
                row[0], "expired", f"pending repriced out: {reason}",
            )
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
