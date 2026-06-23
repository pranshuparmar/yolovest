"""Predictions, shadow predictions, scoring, drift stats.

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


class PredictionsMixin:
    # Predictions
    # ------------------------------------------------------------------

    async def insert_prediction(self, prediction: dict[str, Any]) -> str:
        """Insert a new prediction and return its ID."""
        import uuid

        pred_id = prediction.get("prediction_id") or f"P-{uuid.uuid4().hex[:8]}"
        ts_now = now_utc().isoformat()

        # Compute prediction end time from holding period
        from yolovest.models.schemas import _parse_holding_period

        holding = prediction.get("expected_holding_period", "intraday")
        end_time = now_utc() + _parse_holding_period(holding)

        # Try to find the matching signal_id from signals table
        signal_id = prediction.get("signal_id")
        if not signal_id and prediction.get("symbol"):
            cursor = await self.conn.execute(
                "SELECT id FROM signals WHERE symbol = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (prediction["symbol"],),
            )
            row = await cursor.fetchone()
            if row:
                signal_id = row[0]

        await self.conn.execute(
            "INSERT INTO predictions (prediction_id, signal_id, trade_id, symbol, "
            "created_at, prediction_end_time, actual_price, direction_correct, "
            "target_hit, actual_pnl_pct, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
            (
                pred_id, signal_id, prediction.get("trade_id"),
                prediction.get("symbol"), ts_now, end_time.isoformat(),
                prediction.get("mode", "paper"),
            ),
        )

        # Also store prediction details in audit for traceability
        await self.log_audit(
            action_type="prediction_logged",
            skill_name="predict-track",
            input_summary={
                "prediction_id": pred_id,
                "symbol": prediction.get("symbol"),
                "direction": prediction.get("predicted_direction"),
                "confidence": prediction.get("confidence"),
                "target": prediction.get("predicted_target"),
                "model_version": prediction.get("model_version"),
            },
            auto_commit=False,
        )
        await self.conn.commit()
        return pred_id

    async def insert_shadow_prediction(self, prediction: dict[str, Any]) -> str:
        """Insert a shadow model prediction for A/B comparison."""
        import uuid

        pred_id = f"SP-{uuid.uuid4().hex[:8]}"
        ts_now = now_utc().isoformat()
        symbol = prediction.get("symbol")
        mode = prediction.get("mode", "paper")

        from yolovest.models.schemas import _parse_holding_period

        holding = prediction.get("expected_holding_period", "intraday")
        end_time = now_utc() + _parse_holding_period(holding)

        # Check if symbol column exists (migration 012)
        columns = await self._get_table_columns("predictions")
        has_symbol = "symbol" in columns

        if has_symbol:
            await self.conn.execute(
                "INSERT INTO predictions (prediction_id, symbol, signal_id, trade_id, created_at, "
                "prediction_end_time, actual_price, direction_correct, target_hit, "
                "actual_pnl_pct, is_shadow, model_version, mode) "
                "VALUES (?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, 1, ?, ?)",
                (pred_id, symbol, ts_now, end_time.isoformat(),
                 prediction.get("model_version"), mode),
            )
        else:
            await self.conn.execute(
                "INSERT INTO predictions (prediction_id, signal_id, trade_id, created_at, "
                "prediction_end_time, actual_price, direction_correct, target_hit, "
                "actual_pnl_pct, is_shadow, model_version, mode) "
                "VALUES (?, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, 1, ?, ?)",
                (pred_id, ts_now, end_time.isoformat(),
                 prediction.get("model_version"), mode),
            )

        # Also link to the latest signal for this symbol (best-effort)
        if symbol:
            await self.conn.execute(
                "UPDATE predictions SET "
                "signal_id = (SELECT id FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT 1) "
                "WHERE prediction_id = ?",
                (symbol, pred_id),
            )
        await self.conn.commit()
        return pred_id

    async def get_shadow_vs_production_metrics(
        self, model_type: str, since_date: str,
    ) -> dict[str, Any]:
        """Compare shadow vs production prediction accuracy over a period.

        Returns {shadow: {metrics}, production: {metrics}, agreement_rate}.
        """
        result: dict[str, Any] = {"shadow": {}, "production": {}, "agreement_rate": None}

        # Fetch scored predictions grouped by is_shadow
        cursor = await self.conn.execute(
            "SELECT p.is_shadow, p.model_version, "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN p.direction_correct = 1 THEN 1 ELSE 0 END) as correct, "
            "  SUM(CASE WHEN p.target_hit = 1 THEN 1 ELSE 0 END) as targets_hit, "
            "  AVG(p.actual_pnl_pct) as avg_pnl "
            "FROM predictions p "
            "WHERE p.actual_price IS NOT NULL "
            "AND p.created_at >= ? "
            "GROUP BY p.is_shadow",
            (since_date,),
        )
        rows = await cursor.fetchall()

        for row in rows:
            metrics = {
                "total": row["total"],
                "correct": row["correct"],
                "targets_hit": row["targets_hit"],
                "direction_accuracy": round(row["correct"] / row["total"], 4) if row["total"] > 0 else 0,
                "target_hit_rate": round(row["targets_hit"] / row["total"], 4) if row["total"] > 0 else 0,
                "avg_pnl_pct": round(row["avg_pnl"], 4) if row["avg_pnl"] is not None else 0,
            }
            if row["is_shadow"]:
                result["shadow"] = metrics
            else:
                result["production"] = metrics

        return result

    async def compute_symbol_beta(
        self, symbol: str, lookback_days: int = 60,
    ) -> float | None:
        """Compute a symbol's beta against a cross-sectional market
        proxy. The proxy is the equal-weight mean daily return of every
        symbol with daily bars in the lookback window — the same proxy
        compute_live_regime uses. Returns None when fewer than 20
        overlapping (symbol, market) return pairs are available.

        Formula: beta = cov(symbol_ret, market_ret) / var(market_ret).
        Standard CAPM-style regression slope.

        Used by the risk_check portfolio-beta gate. Cheap enough to
        run on demand inside a heartbeat for the small candidate set,
        but callers should cache per-heartbeat since the inputs are
        the same for every signal in a cycle.
        """
        cutoff = (now_utc() - timedelta(days=lookback_days * 2)).isoformat()
        # Pull all daily bars from the lookback window across the
        # whole universe — same scope as compute_live_regime so the
        # proxy is consistent.
        cursor = await self.read_conn.execute(
            "SELECT symbol, timestamp, close FROM ohlcv "
            "WHERE interval = 'daily' AND timestamp >= ? "
            "ORDER BY symbol, timestamp",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None

        # Build per-symbol close series + the market average per day.
        by_sym: dict[str, list[tuple[str, float]]] = {}
        for r in rows:
            by_sym.setdefault(r[0], []).append((r[1][:10], float(r[2])))

        # Per-day market mean return.
        day_returns: dict[str, list[float]] = {}
        for sym_closes in by_sym.values():
            for i in range(1, len(sym_closes)):
                prev = sym_closes[i - 1][1]
                cur = sym_closes[i][1]
                if prev > 0:
                    ret = (cur - prev) / prev
                    day_returns.setdefault(sym_closes[i][0], []).append(ret)
        market_by_day = {
            d: sum(rs) / len(rs) for d, rs in day_returns.items() if rs
        }

        # Symbol-specific paired series.
        sym_series = by_sym.get(symbol, [])
        if len(sym_series) < 2:
            return None
        sym_returns: list[tuple[float, float]] = []
        for i in range(1, len(sym_series)):
            d = sym_series[i][0]
            prev = sym_series[i - 1][1]
            cur = sym_series[i][1]
            if prev > 0 and d in market_by_day:
                sym_returns.append(((cur - prev) / prev, market_by_day[d]))

        if len(sym_returns) < 20:
            return None

        n = len(sym_returns)
        mean_s = sum(s for s, _ in sym_returns) / n
        mean_m = sum(m for _, m in sym_returns) / n
        cov = sum((s - mean_s) * (m - mean_m) for s, m in sym_returns) / n
        var_m = sum((m - mean_m) ** 2 for _, m in sym_returns) / n
        if var_m <= 0:
            return None
        return cov / var_m

    async def get_live_metrics_for_model(
        self, model_version: str, days: int = 14,
    ) -> dict[str, Any]:
        """Live (i.e. scored-against-actual) metrics for a specific
        model version over the last `days` calendar days. Used by the
        shadow-promotion gate so we can require the shadow to actually
        outperform on real predictions, not just on backtest.

        Returns: {total, scored, direction_accuracy, target_hit_rate,
        avg_pnl_pct} — all zeros when there are no scored predictions
        for the version (caller should treat that as "no live data
        yet, fall back to backtest").
        """
        since = (now_utc() - timedelta(days=days)).isoformat()
        cursor = await self.read_conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN direction_correct IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
            "  SUM(CASE WHEN direction_correct = 1 THEN 1 ELSE 0 END) AS correct, "
            "  SUM(CASE WHEN target_hit = 1 THEN 1 ELSE 0 END) AS targets_hit, "
            "  AVG(actual_pnl_pct) AS avg_pnl "
            "FROM predictions "
            "WHERE model_version = ? AND created_at >= ?",
            (model_version, since),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return {
                "total": 0, "scored": 0,
                "direction_accuracy": 0.0,
                "target_hit_rate": 0.0,
                "avg_pnl_pct": 0.0,
            }
        total = int(row[0] or 0)
        scored = int(row[1] or 0)
        correct = int(row[2] or 0)
        targets_hit = int(row[3] or 0)
        avg_pnl = float(row[4] or 0)
        return {
            "total": total,
            "scored": scored,
            "direction_accuracy": round(correct / scored, 4) if scored > 0 else 0.0,
            "target_hit_rate": round(targets_hit / scored, 4) if scored > 0 else 0.0,
            "avg_pnl_pct": round(avg_pnl, 4),
        }

    async def get_unscored_predictions(self, mode: str | None = None) -> list[dict[str, Any]]:
        """Get predictions whose holding period has elapsed but haven't been scored.

        Used by the predict-track skill to know which predictions are ready to score.
        """
        ts_now = now_utc().isoformat()
        mode_clause = " AND p.mode = ?" if mode else ""
        mode_params: list[Any] = [mode] if mode else []
        cursor = await self.conn.execute(
            f"SELECT p.prediction_id as id, p.trade_id, p.created_at, "
            f"p.prediction_end_time, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as predicted_direction, "
            f"COALESCE(s.entry_price, t.entry_price) as entry_price, "
            f"COALESCE(s.target_price, t.target_price) as predicted_target, "
            f"COALESCE(s.stop_loss_price, t.stop_loss_price) as predicted_stop_loss, "
            f"s.confidence_score as confidence, "
            f"s.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"WHERE p.actual_price IS NULL "
            f"AND p.prediction_end_time <= ?{mode_clause} "
            f"ORDER BY p.created_at",
            [ts_now, *mode_params],
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_all_awaiting_predictions(
        self, *, limit: int = 50, offset: int = 0,
        symbol: str | None = None, direction: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Get ALL unscored predictions for UI display (regardless of end time)."""
        where = "WHERE p.actual_price IS NULL"
        params: list[Any] = []
        where, params = self._apply_prediction_filters(
            where, params, symbol=symbol, direction=direction, model=model,
        )
        cursor = await self.read_conn.execute(
            f"SELECT COUNT(*) FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id {where}",
            params,
        )
        total = (await cursor.fetchone())[0]
        cursor = await self.read_conn.execute(
            f"SELECT p.prediction_id, p.prediction_id as id, p.trade_id, p.created_at, "
            f"p.prediction_end_time, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"COALESCE(s.entry_price, t.entry_price) as entry_price, "
            f"COALESCE(s.target_price, t.target_price) as target_price, "
            f"COALESCE(s.stop_loss_price, t.stop_loss_price) as stop_loss_price, "
            f"COALESCE(s.product, t.product) as product, "
            f"s.holding_period, "
            f"COALESCE(s.expected_holding_days, t.expected_holding_days) as expected_holding_days, "
            f"s.confidence_score, p.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"{where} ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"items": [dict[str, Any](r) for r in rows], "total": total}

    @staticmethod
    def _apply_prediction_filters(
        where: str, params: list[Any], *,
        symbol: str | None = None, direction: str | None = None,
        direction_correct: int | None = None, target_hit: int | None = None,
        model: str | None = None, min_confidence: float | None = None,
    ) -> tuple[str, list[Any]]:
        """Append filter clauses to a prediction query WHERE string."""
        if symbol:
            where += " AND COALESCE(p.symbol, s.symbol, t.symbol) = ?"
            params.append(symbol)
        if direction:
            where += " AND COALESCE(s.signal_type, t.signal_type) = ?"
            params.append(direction)
        if direction_correct is not None:
            where += " AND p.direction_correct = ?"
            params.append(direction_correct)
        if target_hit is not None:
            where += " AND p.target_hit = ?"
            params.append(target_hit)
        if model:
            where += " AND p.model_version = ?"
            params.append(model)
        if min_confidence is not None:
            where += " AND s.confidence_score >= ?"
            params.append(min_confidence)
        return where, params

    async def get_prediction_outcomes_paginated(
        self, *, limit: int = 50, offset: int = 0,
        symbol: str | None = None, direction: str | None = None,
        direction_correct: int | None = None, target_hit: int | None = None,
        model: str | None = None, min_confidence: float | None = None,
    ) -> dict[str, Any]:
        """Scored predictions with pagination and filters."""
        where = "WHERE p.actual_price IS NOT NULL"
        params: list[Any] = []
        where, params = self._apply_prediction_filters(
            where, params, symbol=symbol, direction=direction,
            direction_correct=direction_correct, target_hit=target_hit,
            model=model, min_confidence=min_confidence,
        )
        cursor = await self.read_conn.execute(
            f"SELECT COUNT(*) FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id {where}",
            params,
        )
        total = (await cursor.fetchone())[0]
        cursor = await self.read_conn.execute(
            f"SELECT p.*, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"COALESCE(s.entry_price, t.entry_price) as entry_price, "
            f"COALESCE(s.target_price, t.target_price) as target_price, "
            f"COALESCE(s.stop_loss_price, t.stop_loss_price) as stop_loss_price, "
            f"COALESCE(s.product, t.product) as product, "
            f"s.holding_period, "
            f"COALESCE(s.expected_holding_days, t.expected_holding_days) as expected_holding_days, "
            f"s.confidence_score, p.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"{where} ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"items": [dict[str, Any](r) for r in rows], "total": total}

    async def set_trade_gtt(self, trade_id: str, gtt_id: int | None) -> None:
        """Attach (or clear) the broker GTT trigger id on an open trade."""
        await self.conn.execute(
            "UPDATE trades SET gtt_id = ? WHERE trade_id = ?",
            (gtt_id, trade_id),
        )
        await self.conn.commit()

    async def find_trade_by_order_id(
        self, order_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Find an open trade that has this order_id attached to any of
        its order columns (entry, SL, target). Returns (trade, leg)
        where leg is "entry", "sl", or "target". Returns (None, None) if
        nothing matches.

        Used by the postback handler to route broker-side order updates
        to the right business logic.
        """
        for leg, column in (
            ("entry", "order_id"),
            ("sl", "sl_order_id"),
            ("target", "target_order_id"),
        ):
            cursor = await self.read_conn.execute(
                f"SELECT * FROM trades WHERE {column} = ? LIMIT 1",
                (str(order_id),),
            )
            row = await cursor.fetchone()
            if row:
                return dict[str, Any](row), leg
        return None, None

    async def log_gtt_event(
        self,
        *,
        trade_id: str | None,
        gtt_id: int | None,
        symbol: str | None,
        event_type: str,
        status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a row to the GTT audit log. Idempotent / cheap — used
        from every GTT lifecycle site (place, modify, delete, reconcile,
        rejected_placement) so post-mortems have a single source of truth.

        Failure is swallowed; audit gaps shouldn't break the calling
        trading path.
        """
        try:
            await self.conn.execute(
                "INSERT INTO gtt_events "
                "(trade_id, gtt_id, symbol, event_type, status, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    trade_id,
                    int(gtt_id) if gtt_id is not None else None,
                    symbol,
                    event_type,
                    status,
                    json.dumps(details) if details else None,
                ),
            )
            await self.conn.commit()
        except Exception:
            logger.debug("log_gtt_event failed", exc_info=True)

    async def get_gtt_events_for_trade(
        self, trade_id: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return the GTT audit trail for a single trade, newest first."""
        cursor = await self.read_conn.execute(
            "SELECT id, timestamp_utc, trade_id, gtt_id, symbol, "
            "event_type, status, details_json "
            "FROM gtt_events WHERE trade_id = ? "
            "ORDER BY timestamp_utc DESC LIMIT ?",
            (trade_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](r) for r in rows]

    async def set_trade_gtt_status(
        self, trade_id: str, status: str | None,
    ) -> None:
        """Update the cached GTT lifecycle status (active / triggered /
        cancelled / rejected / expired / disabled / deleted).

        Used by position-monitor's reconciler to detect GTTs that no
        longer protect the position so client-side detection can take
        over.
        """
        await self.conn.execute(
            "UPDATE trades SET gtt_status = ? WHERE trade_id = ?",
            (status, trade_id),
        )
        await self.conn.commit()

    async def set_trade_product(
        self, trade_id: str, product: str,
    ) -> None:
        """Update the product (MIS / CNC) on a trade after a broker-side
        position conversion. Used by `/api/positions/{trade_id}/convert`.
        """
        await self.conn.execute(
            "UPDATE trades SET product = ? WHERE trade_id = ?",
            (product, trade_id),
        )
        await self.conn.commit()

    async def set_trade_target_order_id(
        self, trade_id: int | str, target_order_id: str | None,
    ) -> None:
        """Attach (or clear) the broker target-LIMIT order id on a MIS trade.

        MIS positions can't use GTT, so trade-execute places a LIMIT order
        at target alongside the SL. Position-monitor enforces OCO semantics
        by cancelling whichever side hasn't filled when the other does.
        """
        await self.conn.execute(
            "UPDATE trades SET target_order_id = ? WHERE trade_id = ?",
            (target_order_id, trade_id),
        )
        await self.conn.commit()

    async def set_trade_sl_order_id(
        self, trade_id: int | str, sl_order_id: str | None,
    ) -> None:
        """Update the SL order id on a trade — used when position-monitor
        cancels and re-places SL (e.g. trailing) or clears it after the
        target LIMIT fills."""
        await self.conn.execute(
            "UPDATE trades SET sl_order_id = ? WHERE trade_id = ?",
            (sl_order_id, trade_id),
        )
        await self.conn.commit()

    async def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        """Fetch a single trade row by id (any status)."""
        cursor = await self.read_conn.execute(
            "SELECT * FROM trades WHERE trade_id = ?", (trade_id,),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def score_prediction(
        self,
        prediction_id: str,
        actual_price: float,
        direction_correct: bool,
        target_hit: bool,
        actual_pnl_pct: float,
    ) -> None:
        """Update a prediction with actual outcome."""
        await self.conn.execute(
            "UPDATE predictions SET actual_price = ?, direction_correct = ?, "
            "target_hit = ?, actual_pnl_pct = ?, scored_at = datetime('now') "
            "WHERE prediction_id = ?",
            (
                actual_price,
                1 if direction_correct else 0,
                1 if target_hit else 0,
                actual_pnl_pct,
                prediction_id,
            ),
        )
        await self.conn.commit()

    async def refresh_prediction_scoreboard(self) -> None:
        """Rebuild the prediction scoreboard.

        Aggregates prediction accuracy by symbol, model version, timeframe, and overall.
        """
        scored_predictions = await self._get_all_scored_predictions()
        if not scored_predictions:
            return

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

        for pred in scored_predictions:
            # Overall
            key_overall = ("overall", "overall")
            groups.setdefault(key_overall, []).append(pred)

            # By symbol
            symbol = pred.get("symbol")
            if symbol:
                key_sym = (f"symbol:{symbol}", "symbol")
                groups.setdefault(key_sym, []).append(pred)

            # By model version
            model = pred.get("model_version")
            if model:
                key_model = (f"model:{model}", "model")
                groups.setdefault(key_model, []).append(pred)

        for (group_key, group_type), preds in groups.items():
            total = len(preds)
            correct = sum(1 for p in preds if p.get("direction_correct"))
            accuracy = correct / total if total > 0 else 0
            avg_conf = (
                sum((p.get("confidence") or 0) for p in preds) / total if total > 0 else 0
            )
            target_hits = sum(1 for p in preds if p.get("target_hit"))
            target_rate = target_hits / total if total > 0 else 0
            avg_pnl = (
                sum((p.get("actual_pnl_pct") or 0) for p in preds) / total
                if total > 0
                else 0
            )

            await self.conn.execute(
                "INSERT INTO prediction_scoreboard "
                "(group_key, group_type, total_predictions, correct_predictions, "
                "accuracy, avg_confidence, target_hit_rate, avg_pnl_pct, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(group_key, group_type) DO UPDATE SET "
                "total_predictions=excluded.total_predictions, "
                "correct_predictions=excluded.correct_predictions, "
                "accuracy=excluded.accuracy, avg_confidence=excluded.avg_confidence, "
                "target_hit_rate=excluded.target_hit_rate, avg_pnl_pct=excluded.avg_pnl_pct, "
                "updated_at=excluded.updated_at",
                (group_key, group_type, total, correct, accuracy, avg_conf, target_rate, avg_pnl),
            )
        await self.conn.commit()

    async def _get_all_scored_predictions(self) -> list[dict[str, Any]]:
        """Get all predictions with outcomes for scoreboard computation."""
        cursor = await self.conn.execute(
            "SELECT p.prediction_id, p.direction_correct, p.target_hit, "
            "p.actual_pnl_pct, COALESCE(p.symbol, s.symbol, t.symbol) as symbol, s.confidence_score as confidence, "
            "s.model_version "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
            "LEFT JOIN trades t ON p.trade_id = t.trade_id "
            "WHERE p.actual_price IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_prediction_scoreboard(self, group_type: str | None = None) -> list[dict[str, Any]]:
        """Get prediction scoreboard entries, optionally filtered by group type."""
        if group_type:
            cursor = await self.conn.execute(
                "SELECT * FROM prediction_scoreboard WHERE group_type = ? "
                "ORDER BY total_predictions DESC",
                (group_type,),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM prediction_scoreboard ORDER BY group_type, total_predictions DESC"
            )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_todays_predictions(
        self, *, limit: int = 50, offset: int = 0,
        symbol: str | None = None, direction: str | None = None,
        model: str | None = None, mode: str | None = None,
    ) -> dict[str, Any]:
        """Get predictions created today (IST market day) with pagination and filters."""
        today_start = now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC).isoformat()
        where = "WHERE p.created_at >= ?"
        params: list[Any] = [today_start]
        if mode:
            where += " AND p.mode = ?"
            params.append(mode)
        where, params = self._apply_prediction_filters(
            where, params, symbol=symbol, direction=direction, model=model,
        )
        # Total count
        cursor = await self.read_conn.execute(
            f"SELECT COUNT(*) FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id {where}",
            params,
        )
        total = (await cursor.fetchone())[0]
        # Page
        cursor = await self.read_conn.execute(
            f"SELECT p.*, "
            f"COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            f"COALESCE(s.signal_type, t.signal_type) as signal_type, "
            f"COALESCE(s.entry_price, t.entry_price) as entry_price, "
            f"COALESCE(s.target_price, t.target_price) as target_price, "
            f"COALESCE(s.stop_loss_price, t.stop_loss_price) as stop_loss_price, "
            f"COALESCE(s.product, t.product) as product, "
            f"s.holding_period, "
            f"COALESCE(s.expected_holding_days, t.expected_holding_days) as expected_holding_days, "
            f"s.confidence_score, p.model_version "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN trades t ON p.trade_id = t.trade_id "
            f"{where} ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return {"items": [dict[str, Any](r) for r in rows], "total": total}

    # ------------------------------------------------------------------
