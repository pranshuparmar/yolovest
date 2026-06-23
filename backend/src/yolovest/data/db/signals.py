"""Signal rows, dispositions, daily dedup queries.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import json
import logging
from datetime import UTC, timedelta
from typing import Any

from yolovest.timezone import now_ist, now_utc

logger = logging.getLogger(__name__)


class SignalsMixin:
    # Signals
    # ------------------------------------------------------------------

    async def insert_signal(self, signal: dict[str, Any]) -> int:
        """Persist a generated signal. Caller should set `mode` to the
        active trading mode so bulk-delete and analytics can scope by it.
        attribution_json holds the top-N feature contributions surfaced
        on TradeDetailPage; None when the ML layer couldn't compute
        them (e.g. booster unreachable through calibration wrapper).

        Returns the autoincrement id of the inserted row. trade-execute
        carries this id onto the trade row so the UNIQUE index on
        trades.signal_id can enforce one-trade-per-signal at the DB
        layer (defence-in-depth against a missed in-memory dedup).
        """
        attribution = signal.get("attribution")
        attribution_json = json.dumps(attribution) if attribution else None
        # product / holding_period / expected_holding_days come from the
        # holding-period decision (signal_evaluator). Persisting them lets
        # the recommendations view show MIS vs CNC and derive a target date.
        cursor = await self.conn.execute(
            "INSERT INTO signals (symbol, signal_type, entry_price, target_price, "
            "stop_loss_price, position_size, confidence_score, model_version, "
            "features_snapshot, mode, attribution_json, "
            "product, holding_period, expected_holding_days, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
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
                signal.get("mode", "paper"),
                attribution_json,
                signal.get("product"),
                signal.get("expected_holding_period"),
                signal.get("expected_holding_days"),
            ),
        )
        await self.conn.commit()
        return int(cursor.lastrowid or 0)

    async def update_signal_disposition(
        self,
        symbol: str,
        disposition: str,
        reason: str | None = None,
        position_size: int | None = None,
        mode: str | None = None,
    ) -> None:
        """Update disposition for the most recent signal for a symbol today.

        `insert_signal` stamps `created_at` via SQLite `datetime('now')`,
        i.e. UTC in space-separated form (`2026-05-13 04:18:30`). We scope
        to "today's IST trading session" by converting IST-midnight to its
        UTC instant and matching `created_at >= that`. Comparing the
        IST *calendar date* against the UTC date prefix used to silently
        no-op during the 00:00–05:30 IST window (when the IST date is a day
        ahead of UTC), leaving disposition stuck at the seeded value.

        When mode is provided, the update is scoped to rows of that
        mode so a live execution can't accidentally flip a stale paper
        signal's disposition (or vice versa).
        """
        ist_day_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Match the space-separated UTC format that datetime('now') writes
        # so the lexical string comparison is also chronological.
        day_start_utc = ist_day_start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        mode_clause = " AND mode = ?" if mode else ""
        # ml_signal seeds position_size=1 as a placeholder; risk-check
        # determines the real number. Update both columns here so the
        # signals row reflects the actual planned size when this
        # function is called from the dispatch path (which has the
        # post-risk-check value handy). When the caller doesn't pass
        # position_size, leave it untouched via COALESCE.
        if position_size is not None and position_size > 0:
            params: tuple[Any, ...] = (
                disposition, reason, int(position_size), symbol, day_start_utc,
            )
            if mode:
                params = params + (mode,)
            await self.conn.execute(
                "UPDATE signals "
                "SET disposition = ?, disposition_reason = ?, "
                "    position_size = ? "
                "WHERE id = (SELECT id FROM signals WHERE symbol = ? "
                f"AND created_at >= ?{mode_clause} "
                "ORDER BY created_at DESC LIMIT 1)",
                params,
            )
        else:
            params = (disposition, reason, symbol, day_start_utc)
            if mode:
                params = params + (mode,)
            await self.conn.execute(
                "UPDATE signals SET disposition = ?, disposition_reason = ? "
                "WHERE id = (SELECT id FROM signals WHERE symbol = ? "
                f"AND created_at >= ?{mode_clause} "
                "ORDER BY created_at DESC LIMIT 1)",
                params,
            )
        await self.conn.commit()

    async def get_todays_recommendations(self) -> list[dict[str, Any]]:
        """Today's signals with disposition — what the system suggested + outcome."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT id, symbol, signal_type, entry_price, target_price, "
            "stop_loss_price, position_size, confidence_score, model_version, "
            "disposition, disposition_reason, attribution_json, "
            "product, holding_period, expected_holding_days, created_at "
            "FROM signals WHERE created_at >= ? "
            "ORDER BY confidence_score DESC, created_at DESC",
            (today_start,),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_signaled_symbols(
        self, mode: str | None = None,
        risk_rejected_retry_cap: int = 5,
    ) -> set[str]:
        """Get symbols that should be skipped from new signal generation today.

        Includes:
        - Symbols with non-retryable signals today (executed,
          llm_rejected, awaiting_approval, or in-flight NULL).
        - Symbols whose retryable-disposition count (risk_rejected,
          expired) has reached risk_rejected_retry_cap — guard against
          chronically-failing setups generating a fresh row every
          heartbeat.
        - Symbols with open SYSTEM-generated positions in the current
          mode (avoid double-trading).

        Retryable dispositions (re-evaluated when count < cap):
        - risk_rejected: most risk-check reasons (exposure, drift,
          depth, correlation, cooldown) clear within the same day.
        - expired: the user didn't approve in time, but conditions may
          still favour the setup — give it another shot. Hard "no"
          should come via /reject, which routes through the separate
          rejection_cooldown_hours mechanism.
        - trade_execute_failed: the broker rejected the order or the
          skill crashed — usually a transient condition.
        - skill_error: risk-check or llm-review itself threw an
          exception. Treated as retryable so a persistent skill bug
          doesn't burn the cap on the *risk decision* side; the cap
          still protects against churn loops.

        Deferred dispositions (re-evaluated freely, cap-exempt):
        - time_blocked: signal was generated outside the order window
          (e.g. heartbeat fired between market.open and order_start).
          The underlying condition is purely time-based ("wait N
          minutes"), so consuming a retry slot would punish the
          symbol for a scheduler edge case rather than a real signal
          problem. Counts as neither "other" nor "retryable" in the
          dedup math.
        """
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()

        # Symbols where dedup applies — either has at least one
        # non-retryable signal today (count_other > 0) or the retryable
        # budget is exhausted (count_retryable >= cap). Mode-scoped so
        # paper signals don't block live and vice versa.
        retryable = (
            'risk_rejected', 'expired',
            'trade_execute_failed', 'skill_error',
        )
        deferred = ('time_blocked',)
        ignorable = retryable + deferred  # neither blocks nor counts toward cap when "other"-counting
        ignorable_ph = ", ".join(["?"] * len(ignorable))
        retryable_ph = ", ".join(["?"] * len(retryable))
        mode_clause = " AND mode = ?" if mode else ""
        sig_params: tuple[Any, ...] = (today_start,)
        if mode:
            sig_params = sig_params + (mode,)
        # Bound params order: first the ignorable set for the "other"
        # count (deferred rows must NOT count as other), then the
        # retryable set for the cap count (deferred rows must NOT
        # count toward the cap), then the cap value itself.
        sig_params = sig_params + ignorable + retryable + (int(risk_rejected_retry_cap),)
        cursor = await self.read_conn.execute(
            "SELECT symbol FROM signals "
            f"WHERE created_at >= ?{mode_clause} "
            "GROUP BY symbol "
            f"HAVING SUM(CASE WHEN disposition IN ({ignorable_ph}) THEN 0 ELSE 1 END) > 0 "
            f"   OR SUM(CASE WHEN disposition IN ({retryable_ph}) THEN 1 ELSE 0 END) >= ?",
            sig_params,
        )
        signaled = {row[0] for row in await cursor.fetchall()}

        # Symbols with open SYSTEM-generated positions only (skip adopted).
        # Filter by mode so old paper positions don't block live signal generation.
        query = (
            "SELECT DISTINCT symbol FROM trades "
            "WHERE status IN ('open', 'partially_filled') "
            "AND COALESCE(origin, 'system') = 'system'"
        )
        params: list[Any] = []
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        cursor = await self.read_conn.execute(query, params)
        positioned = {row[0] for row in await cursor.fetchall()}
        return signaled | positioned

    async def clear_todays_signals(self) -> dict[str, int]:
        """Clear today's actionable signals so the next heartbeat can
        regenerate fresh ones. Preserves:
          - `executed` rows (real trades happened — keep the audit link)
          - `rejected` rows (user explicitly said no — don't re-suggest)

        Deletes everything else: NULL (never processed), `awaiting_approval`,
        `risk_rejected`, `llm_rejected`, and `expired`. Pending trades in
        `pending` / `expired` state are also dropped so the symbols are
        free for re-evaluation.

        Cascades to predictions.signal_id since the schema has no ON
        DELETE CASCADE — without that, foreign_keys=ON would reject
        the DELETE on signals that already have a linked prediction
        (typical after a full heartbeat ran predict-track).

        Returns counts of deleted rows per table.
        """
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()

        # First null out predictions.signal_id for rows we're about to
        # delete from signals. NULLing instead of cascading because the
        # predictions table is part of the model-drift audit trail —
        # we don't want to lose the scored outcomes just because the
        # source signal was re-evaluated.
        await self.conn.execute(
            "UPDATE predictions SET signal_id = NULL "
            "WHERE signal_id IN ("
            "  SELECT id FROM signals WHERE created_at >= ? "
            "  AND (disposition IS NULL "
            "       OR disposition IN ('awaiting_approval', 'risk_rejected', "
            "                          'llm_rejected', 'expired', 'time_blocked'))"
            ")",
            (today_start,),
        )

        cursor = await self.conn.execute(
            "DELETE FROM signals "
            "WHERE created_at >= ? "
            "AND (disposition IS NULL "
            "     OR disposition IN ('awaiting_approval', 'risk_rejected', "
            "                        'llm_rejected', 'expired', 'time_blocked'))",
            (today_start,),
        )
        signals_deleted = cursor.rowcount

        cursor = await self.conn.execute(
            "DELETE FROM pending_trades WHERE status IN ('pending', 'expired')",
        )
        pending_deleted = cursor.rowcount

        await self.conn.commit()
        logger.info(
            "Cleared %d signals (today, non-terminal) and %d pending/expired trades",
            signals_deleted, pending_deleted,
        )
        return {"signals_deleted": signals_deleted, "pending_deleted": pending_deleted}

    async def get_recently_traded_symbols(
        self, lookback_days: int, mode: str | None = None,
    ) -> dict[str, str]:
        """Get symbols traded in the last N days with their most recent trade date.

        Returns {symbol: last_trade_date_iso} for symbols with trades
        in the lookback window. Filters by mode and excludes adopted holdings.
        """
        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        query = (
            "SELECT symbol, MAX(created_at) as last_trade "
            "FROM trades WHERE created_at >= ? "
            "AND COALESCE(origin, 'system') = 'system'"
        )
        params: list[Any] = [cutoff]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        query += " GROUP BY symbol"
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
