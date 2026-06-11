"""Model version registry + training-dataset queries.

Mixin for the composed Database class (see yolovest/data/db/__init__).
Methods moved verbatim from the original monolithic db.py; they run on
the connections owned by DatabaseCore (self.conn / self.read_conn).
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from yolovest.data.db.core import (
    _normalize_iso_date,
)
from yolovest.timezone import IST, now_ist, now_utc

logger = logging.getLogger(__name__)


class ModelsTrainingMixin:
    # Model Versions
    # ------------------------------------------------------------------

    async def save_model_version(
        self, model_type: str, version: str, file_path: str, metrics: dict[str, Any]
    ) -> None:
        """Save a new model version record."""
        await self.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, "
            "sharpe_ratio, sharpe_lower, argmax_sharpe, max_drawdown_pct, "
            "win_rate, profit_factor, status, shadow_start_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'shadow', datetime('now'))",
            (
                model_type,
                version,
                file_path,
                metrics.get("sharpe") or metrics.get("sharpe_ratio"),
                metrics.get("sharpe_lower"),
                metrics.get("argmax_sharpe"),
                metrics.get("max_drawdown_pct"),
                metrics.get("win_rate"),
                metrics.get("profit_factor"),
            ),
        )
        await self.conn.commit()

    async def get_production_model(self, model_type: str) -> dict[str, Any] | None:
        """Get the current production model for a model type."""
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions "
            "WHERE model_type = ? AND status = 'production' "
            "ORDER BY created_at DESC LIMIT 1",
            (model_type,),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def get_model_version(self, version: str) -> dict[str, Any] | None:
        """Look up a single model_versions row by version string (any status)."""
        cursor = await self.read_conn.execute(
            "SELECT * FROM model_versions WHERE version = ? LIMIT 1",
            (version,),
        )
        row = await cursor.fetchone()
        return dict[str, Any](row) if row else None

    async def promote_model(self, model_type: str, version: str) -> None:
        """Promote a shadow model to production, retire the current production.

        First UPDATE auto-begins the transaction (Python sqlite3 default
        deferred isolation), commit() ends it. Explicit BEGIN omitted —
        it conflicts when other writers are active on the same connection.
        """
        try:
            # Retire current production
            await self.conn.execute(
                "UPDATE model_versions SET status = 'retired' "
                "WHERE model_type = ? AND status = 'production'",
                (model_type,),
            )
            # Promote new model
            await self.conn.execute(
                "UPDATE model_versions SET status = 'production', "
                "promoted_date = datetime('now') "
                "WHERE model_type = ? AND version = ?",
                (model_type, version),
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise

    async def get_all_shadow_models(self) -> list[dict[str, Any]]:
        """Get all models currently in shadow status."""
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions WHERE status = 'shadow' "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_retired_models(self) -> list[dict[str, Any]]:
        """Get all retired models (previously production or rejected shadow)."""
        cursor = await self.conn.execute(
            "SELECT * FROM model_versions WHERE status = 'retired' "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def cleanup_retired_models(self, older_than_days: int) -> int:
        """Delete retired models older than N days (DB record + .pkl file)."""
        from datetime import timedelta
        cutoff = (now_utc() - timedelta(days=older_than_days)).isoformat()
        cursor = await self.conn.execute(
            "SELECT model_type, version, file_path FROM model_versions "
            "WHERE status = 'retired' AND created_at < ?",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        deleted = 0
        for row in rows:
            try:
                await self.delete_model_version(row["model_type"], row["version"])
                deleted += 1
            except Exception:
                logger.warning("Failed to delete retired model %s/%s", row.get("model_type"), row.get("version"), exc_info=True)
        return deleted

    async def reshadow_model(self, model_type: str, version: str) -> bool:
        """Move a retired model back to shadow status for re-evaluation."""
        cursor = await self.conn.execute(
            "UPDATE model_versions SET status = 'shadow', "
            "shadow_start_date = datetime('now') "
            "WHERE model_type = ? AND version = ? AND status = 'retired'",
            (model_type, version),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Training Data
    # ------------------------------------------------------------------

    async def get_training_dataset(
        self, max_days: int | None = None,
    ) -> dict[str, Any]:
        """Load OHLCV data for model training. delivery_pct is included
        as an optional per-bar column; falls back to None for older
        rows imported before migration 038.

        `max_days` caps history so the feature-matrix builder's peak
        memory stays bounded — it scales with days × symbols. The
        default in retraining.max_training_days (730) is the starting
        point; raise it on hosts with memory to spare.
        """
        if max_days is not None and max_days > 0:
            cursor = await self.conn.execute(
                "SELECT symbol, timestamp, open, high, low, close, volume, delivery_pct "
                "FROM ohlcv WHERE interval = 'daily' "
                "  AND timestamp >= date('now', ?) "
                "ORDER BY symbol, timestamp",
                (f"-{int(max_days)} day",),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT symbol, timestamp, open, high, low, close, volume, delivery_pct "
                "FROM ohlcv WHERE interval = 'daily' ORDER BY symbol, timestamp"
            )
        rows = await cursor.fetchall()
        return {"bars": [dict[str, Any](row) for row in rows]}

    async def get_intraday_training_dataset(
        self,
        *,
        max_days: int | None = None,
        interval: str = "5minute",
        minute_interval: str = "1m",
        symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load intraday training data: 5-min *decision* bars + 1-min *path* bars.

        The intraday model computes features and takes entries on the
        ``interval`` (5-min) series, but the triple-barrier label resolves
        target-before-SL ordering on the finer ``minute_interval`` (1-min)
        series (see ``intraday_triple_barrier_label``). Both are returned,
        scoped to the same window, so every decision bar has the 1-min path
        needed to label it.

        ``symbols`` scopes both queries — chunk the F&O universe through it
        to bound memory, since 1-min × the full universe is large. ``max_days``
        caps history (timestamps are ISO, so the lexical date compare is safe
        for intraday timestamps too).

        Returns::

            {
              "decision_bars": [ {symbol, timestamp, open, high, low, close, volume}, ... ],
              "minute_bars": { symbol: [ {timestamp, open, high, low, close, volume}, ... ] },
            }

        ``decision_bars`` is ordered by (symbol, timestamp); each
        ``minute_bars[symbol]`` list is ascending by timestamp.
        """
        def _build(iv: str) -> tuple[str, list[Any]]:
            q = (
                "SELECT symbol, timestamp, open, high, low, close, volume "
                "FROM ohlcv WHERE interval = ?"
            )
            params: list[Any] = [iv]
            if max_days is not None and max_days > 0:
                q += " AND timestamp >= date('now', ?)"
                params.append(f"-{int(max_days)} day")
            if symbols:
                placeholders = ",".join("?" * len(symbols))
                q += f" AND symbol IN ({placeholders})"
                params.extend(symbols)
            q += " ORDER BY symbol, timestamp"
            return q, params

        dec_q, dec_params = _build(interval)
        dec_rows = await self.read_conn.execute_fetchall(dec_q, tuple(dec_params))
        decision_bars = [dict[str, Any](r) for r in dec_rows]

        min_q, min_params = _build(minute_interval)
        min_rows = await self.read_conn.execute_fetchall(min_q, tuple(min_params))
        minute_bars: dict[str, list[dict[str, Any]]] = {}
        for r in min_rows:
            row = dict[str, Any](r)
            minute_bars.setdefault(row["symbol"], []).append(row)

        return {"decision_bars": decision_bars, "minute_bars": minute_bars}

    async def get_bulk_deals_timeline(self) -> list[dict[str, Any]]:
        """Return all bulk/block deals across history, ordered by date.
        Used by model_retrain to build a (symbol, date) → net-count
        index for per-sample feature lookup.
        """
        cursor = await self.read_conn.execute(
            "SELECT symbol, deal_date, buy_sell FROM bulk_deals "
            "ORDER BY deal_date"
        )
        return [
            {"symbol": r[0], "deal_date": r[1], "buy_sell": r[2]}
            for r in await cursor.fetchall()
        ]

    async def get_news_timeline(
        self, date_from: str | None = None,
    ) -> dict[str, list[tuple[str, str]]]:
        """Return all news headlines grouped by symbol since date_from.

        Each entry is (headline, published_at_iso). Used by model_retrain
        to compute per-(symbol, as_of) sentiment features without an
        N+1 query per sample — one scan, fan-out in Python.
        """
        query = (
            "SELECT headline, symbols, published_at FROM news_articles "
            "WHERE published_at IS NOT NULL"
        )
        params: list[Any] = []
        if date_from:
            query += " AND published_at >= ?"
            params.append(date_from)
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        out: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            headline, symbols_raw, published_at = r[0], r[1], r[2]
            if not symbols_raw:
                continue
            try:
                symbols = json.loads(symbols_raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for sym in symbols:
                if not isinstance(sym, str) or not sym:
                    continue
                out.setdefault(sym, []).append((headline, published_at))
        for sym in out:
            out[sym].sort(key=lambda x: x[1])
        return out

    async def upsert_fno_daily(
        self, date_str: str, aggregates: dict[str, dict[str, float]],
    ) -> int:
        """Insert today's F&O aggregates. UPSERT semantics so the skill
        can be re-run safely if the cron fires twice (idempotent).
        Returns count of rows upserted.
        """
        if not aggregates:
            return 0
        rows = [
            (
                date_str,
                symbol,
                agg.get("pcr_oi"),
                agg.get("pcr_volume"),
                agg.get("futures_oi"),
                agg.get("futures_volume"),
                agg.get("futures_close"),
            )
            for symbol, agg in aggregates.items()
        ]
        await self.conn.executemany(
            "INSERT INTO fno_daily "
            "(date, symbol, pcr_oi, pcr_volume, futures_oi, "
            "futures_volume, futures_close) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET "
            "pcr_oi=excluded.pcr_oi, pcr_volume=excluded.pcr_volume, "
            "futures_oi=excluded.futures_oi, "
            "futures_volume=excluded.futures_volume, "
            "futures_close=excluded.futures_close, "
            "created_at=datetime('now')",
            rows,
        )
        await self.conn.commit()
        return len(rows)

    async def get_distinct_ohlcv_symbols(
        self, interval: str, max_days: int | None = None,
    ) -> list[str]:
        """Distinct symbols that have bars at ``interval`` within ``max_days``.

        Cheap symbol-list lookup used to chunk the intraday training fetch:
        loading 1-min bars for the whole universe at once can exhaust
        available memory, so model-retrain walks symbol chunks, and this
        is the index it chunks over. ``max_days`` mirrors
        get_intraday_training_dataset's lexical ISO date compare.
        """
        q = "SELECT DISTINCT symbol FROM ohlcv WHERE interval = ?"
        params: list[Any] = [interval]
        if max_days is not None and max_days > 0:
            q += " AND timestamp >= date('now', ?)"
            params.append(f"-{int(max_days)} day")
        q += " ORDER BY symbol"
        rows = await self.read_conn.execute_fetchall(q, tuple(params))
        return [r[0] for r in rows if r[0]]

    async def get_distinct_fno_underlyings(self) -> list[str]:
        """Distinct F&O underlying symbols seen in fno_daily.

        Offline fallback for resolving the F&O universe when a live NFO
        instrument-master fetch isn't available (broker unauthenticated).
        Only as complete as ingest-fno's accumulated history.
        """
        rows = await self.read_conn.execute_fetchall(
            "SELECT DISTINCT symbol FROM fno_daily ORDER BY symbol"
        )
        return [r[0] for r in rows if r[0]]

    async def get_fno_timeline(
        self, date_from: str | None = None,
    ) -> dict[str, list[tuple[str, dict[str, float]]]]:
        """Return F&O aggregates grouped by symbol, ascending by date.

        Used by model_retrain to bind per-(symbol, date) features without
        an N+1 query per sample — one scan, fan-out in Python. Same
        shape as get_news_timeline.
        """
        query = (
            "SELECT date, symbol, pcr_oi, pcr_volume, futures_oi, "
            "futures_volume, futures_close FROM fno_daily WHERE 1=1"
        )
        params: list[Any] = []
        if date_from:
            query += " AND date >= ?"
            params.append(date_from)
        query += " ORDER BY symbol, date"
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        out: dict[str, list[tuple[str, dict[str, float]]]] = {}
        for r in rows:
            row_dict = {
                "pcr_oi": r[2],
                "pcr_volume": r[3],
                "futures_oi": r[4],
                "futures_volume": r[5],
                "futures_close": r[6],
            }
            out.setdefault(r[1], []).append((r[0], row_dict))
        return out

    async def get_vix_timeline(
        self, date_from: str | None = None,
    ) -> list[tuple[str, float]]:
        """Return India VIX daily close history as (date_str, close), oldest first.

        Reads from ohlcv where symbol='INDIA VIX' and interval='daily'.
        Used by model_retrain to bind per-sample VIX regime features and
        by ingest-vix's cold-start guard to decide whether to backfill.
        """
        query = (
            "SELECT timestamp, close FROM ohlcv "
            "WHERE symbol = 'INDIA VIX' AND interval = 'daily'"
        )
        params: list[Any] = []
        if date_from:
            query += " AND timestamp >= ?"
            params.append(date_from)
        query += " ORDER BY timestamp"
        rows = await self.read_conn.execute_fetchall(query, tuple(params))
        out: list[tuple[str, float]] = []
        for r in rows:
            ts_raw = r[0]
            close = r[1]
            if close is None:
                continue
            # ohlcv.timestamp is ISO datetime; the date portion is what
            # we join against in model_retrain. Strip cheaply.
            date_str = ts_raw.split("T")[0] if "T" in ts_raw else ts_raw[:10]
            out.append((date_str, float(close)))
        return out

    # ------------------------------------------------------------------
    # Feature-drift snapshots
    # ------------------------------------------------------------------

    async def upsert_feature_snapshot(
        self, day: str, symbol: str, mode: str, features: dict[str, Any],
    ) -> None:
        """Persist one symbol's UNCONDITIONED inference feature vector for
        the day (pre-gate — written for every evaluated symbol, not just
        passed signals). Later heartbeats the same day overwrite, so the
        table holds at most one row per (day, symbol, mode)."""
        await self.conn.execute(
            "INSERT OR REPLACE INTO feature_snapshots "
            "(day, symbol, mode, features_json) VALUES (?, ?, ?, ?)",
            (day, symbol, mode, json.dumps(
                {k: v for k, v in features.items()
                 if isinstance(v, (int, float))},
            )),
        )
        await self.conn.commit()

    async def get_feature_snapshots(
        self, days: int, mode: str,
    ) -> list[dict[str, float]]:
        """Parsed feature dicts from the trailing `days` calendar days.
        drift-watch bins these against the production model's training
        deciles (PSI)."""
        date_from = (now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await self.read_conn.execute_fetchall(
            "SELECT features_json FROM feature_snapshots "
            "WHERE day >= ? AND mode = ?",
            (date_from, mode),
        )
        out: list[dict[str, float]] = []
        for r in rows:
            try:
                parsed = json.loads(r[0])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    async def prune_feature_snapshots(self, keep_days: int = 30) -> int:
        """Trim snapshots older than `keep_days`. Called by drift-watch
        after each read so the table is self-maintaining."""
        cutoff = (now_ist() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        cursor = await self.conn.execute(
            "DELETE FROM feature_snapshots WHERE day < ?", (cutoff,),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0)

    async def get_prediction_outcomes(self) -> list[dict[str, Any]]:
        """Load predictions with actual outcomes for retraining analysis."""
        cursor = await self.conn.execute(
            "SELECT p.*, COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
            "COALESCE(s.signal_type, t.signal_type) as signal_type, "
            "s.confidence_score "
            "FROM predictions p "
            "LEFT JOIN signals s ON p.signal_id = s.id "
            "LEFT JOIN trades t ON p.trade_id = t.trade_id "
            "WHERE p.actual_price IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict[str, Any](row) for row in rows]

    async def get_model_drift_stats(
        self, days: int = 30, mode: str | None = None,
    ) -> dict[str, Any]:
        """Compare predicted vs realised win rate to detect model drift.

        For every model_version that has scored predictions in the window,
        groups by parent model_type (joined from model_versions) and emits:
          - by_day: predicted_win_rate (mean confidence_score) vs
            realised_win_rate (sum direction_correct / count) for each
            scored_at day
          - calibration_buckets: confidence buckets [0.5-0.6, 0.6-0.7, ...]
            with predicted_mean vs realised_rate + sample size

        A top-level warning string flags a realised win-rate drop of more
        than 15 percentage points in the last 7 days vs the prior 7 days
        (per model_type).
        """
        cutoff = (now_utc() - timedelta(days=days)).isoformat()
        mc = " AND p.mode = ?" if mode else ""
        mp: list[Any] = [mode] if mode else []

        # Pull all scored predictions in the window joined with the model
        # registry to get model_type. predictions.model_version may be on
        # either the predictions row or the signals row depending on age;
        # COALESCE picks whichever is set.
        cursor = await self.read_conn.execute(
            f"SELECT COALESCE(p.model_version, s.model_version) AS version, "
            f"  mv.model_type AS model_type, "
            f"  mv.status AS model_status, "
            f"  substr(p.scored_at, 1, 10) AS day, "
            f"  s.confidence_score AS confidence, "
            f"  p.direction_correct AS correct "
            f"FROM predictions p "
            f"LEFT JOIN signals s ON p.signal_id = s.id "
            f"LEFT JOIN model_versions mv "
            f"  ON mv.version = COALESCE(p.model_version, s.model_version) "
            f"WHERE p.actual_price IS NOT NULL "
            f"  AND p.scored_at IS NOT NULL "
            f"  AND p.scored_at >= ? "
            f"  AND p.direction_correct IS NOT NULL{mc}",
            [cutoff, *mp],
        )
        rows = await cursor.fetchall()

        # Group by model_type → version → (day_rows, all_rows)
        by_type: dict[str, dict[str, Any]] = {}
        for r in rows:
            mt = r["model_type"]
            if not mt:
                # Unmapped version (e.g. retired model purged from
                # model_versions). Skip — we can't classify it.
                continue
            entry = by_type.setdefault(mt, {
                "model_type": mt,
                "version": r["version"],
                "is_production": (r["model_status"] == "production"),
                "_days": {},
                "_all": [],
            })
            # Always keep the most recent production version as the
            # representative version for the model_type.
            if r["model_status"] == "production":
                entry["version"] = r["version"]
                entry["is_production"] = True
            day = r["day"]
            if not day:
                continue
            day_bucket = entry["_days"].setdefault(
                day, {"conf_sum": 0.0, "conf_n": 0, "correct": 0, "n": 0},
            )
            conf = r["confidence"]
            if conf is not None:
                day_bucket["conf_sum"] += float(conf)
                day_bucket["conf_n"] += 1
            day_bucket["correct"] += int(r["correct"] or 0)
            day_bucket["n"] += 1
            entry["_all"].append({
                "confidence": float(conf) if conf is not None else None,
                "correct": int(r["correct"] or 0),
                "day": day,
            })

        bucket_edges = [
            (0.5, 0.6, "0.50-0.60"),
            (0.6, 0.7, "0.60-0.70"),
            (0.7, 0.8, "0.70-0.80"),
            (0.8, 0.9, "0.80-0.90"),
            (0.9, 1.0001, "0.90-1.00"),
        ]

        warnings: list[str] = []
        model_versions: list[dict[str, Any]] = []
        for mt, entry in by_type.items():
            # Build by_day list sorted ascending.
            by_day = []
            for day in sorted(entry["_days"].keys()):
                d = entry["_days"][day]
                predicted = (
                    d["conf_sum"] / d["conf_n"] if d["conf_n"] > 0 else None
                )
                realised = d["correct"] / d["n"] if d["n"] > 0 else 0.0
                by_day.append({
                    "date": day,
                    "predicted_win_rate": (
                        round(predicted, 4) if predicted is not None else None
                    ),
                    "realised_win_rate": round(realised, 4),
                    "sample_size": d["n"],
                })

            # Calibration buckets over the full window.
            calibration_buckets = []
            for lo, hi, label in bucket_edges:
                items = [
                    a for a in entry["_all"]
                    if a["confidence"] is not None
                    and lo <= a["confidence"] < hi
                ]
                if not items:
                    calibration_buckets.append({
                        "bucket": label,
                        "predicted_mean": None,
                        "realised_rate": None,
                        "samples": 0,
                    })
                    continue
                pred_mean = sum(i["confidence"] for i in items) / len(items)
                real_rate = sum(i["correct"] for i in items) / len(items)
                calibration_buckets.append({
                    "bucket": label,
                    "predicted_mean": round(pred_mean, 4),
                    "realised_rate": round(real_rate, 4),
                    "samples": len(items),
                })

            # Drift detection: realised win-rate last 7d vs prior 7d.
            now_d = now_utc().date()
            recent_correct = recent_n = prior_correct = prior_n = 0
            for a in entry["_all"]:
                try:
                    d = datetime.fromisoformat(a["day"]).date()
                except (TypeError, ValueError):
                    continue
                age = (now_d - d).days
                if 0 <= age < 7:
                    recent_correct += a["correct"]
                    recent_n += 1
                elif 7 <= age < 14:
                    prior_correct += a["correct"]
                    prior_n += 1
            if recent_n >= 5 and prior_n >= 5:
                recent_rate = recent_correct / recent_n
                prior_rate = prior_correct / prior_n
                drop = prior_rate - recent_rate
                if drop > 0.15:
                    warnings.append(
                        f"{mt} model realised win-rate dropped "
                        f"{int(round(drop * 100))}% in last 7 days "
                        f"({int(round(prior_rate * 100))}% -> "
                        f"{int(round(recent_rate * 100))}%)"
                    )

            model_versions.append({
                "model_type": mt,
                "version": entry["version"],
                "is_production": entry["is_production"],
                "by_day": by_day,
                "calibration_buckets": calibration_buckets,
            })

        # Stable ordering: intraday first, then swing, then anything else.
        order = {"intraday": 0, "swing": 1}
        model_versions.sort(key=lambda m: (order.get(m["model_type"], 99), m["model_type"]))

        return {
            "model_versions": model_versions,
            "warning": "; ".join(warnings) if warnings else None,
        }

    async def get_fii_dii_timeline(self, days: int = 30) -> list[dict[str, Any]]:
        """Return the last `days` rows of FII/DII flows ordered by
        date ascending (so the dashboard line chart can plot directly).
        Values are in ₹ crore as published by NSE.
        """
        cursor = await self.read_conn.execute(
            "SELECT date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net "
            "FROM fii_dii_daily "
            "WHERE date >= date('now', ?) "
            "ORDER BY date",
            (f"-{int(days)} day",),
        )
        return [
            {
                "date": r[0],
                "fii_buy": float(r[1] or 0),
                "fii_sell": float(r[2] or 0),
                "fii_net": float(r[3] or 0),
                "dii_buy": float(r[4] or 0),
                "dii_sell": float(r[5] or 0),
                "dii_net": float(r[6] or 0),
            }
            for r in await cursor.fetchall()
        ]

    async def get_bulk_deals_list(
        self, days: int = 30, symbol: str | None = None, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Recent bulk/block deals for the dashboard table. Filtered
        to the last `days` calendar days and optionally to a single
        symbol. Ordered most-recent-first.
        """
        params: list[Any] = [f"-{int(days)} day"]
        sym_clause = ""
        if symbol:
            sym_clause = " AND symbol = ?"
            params.append(symbol)
        params.append(int(limit))
        cursor = await self.read_conn.execute(
            "SELECT deal_date, symbol, deal_type, client_name, buy_sell, "
            "       quantity, trade_price "
            "FROM bulk_deals "
            "WHERE deal_date >= date('now', ?)" + sym_clause +
            " ORDER BY deal_date DESC, symbol "
            "LIMIT ?",
            params,
        )
        return [
            {
                "deal_date": r[0],
                "symbol": r[1],
                "deal_type": r[2],
                "client_name": r[3],
                "buy_sell": r[4],
                "quantity": r[5],
                "trade_price": r[6],
            }
            for r in await cursor.fetchall()
        ]

    async def get_fii_dii_timeline_summary(
        self, days: int = 30,
    ) -> dict[str, Any]:
        """Quick aggregates for the dashboard header pills."""
        timeline = await self.get_fii_dii_timeline(days)
        if not timeline:
            return {
                "days_covered": 0,
                "fii_net_total": 0.0,
                "dii_net_total": 0.0,
                "fii_net_today": None,
                "dii_net_today": None,
            }
        last = timeline[-1]
        return {
            "days_covered": len(timeline),
            "fii_net_total": round(sum(r["fii_net"] for r in timeline), 2),
            "dii_net_total": round(sum(r["dii_net"] for r in timeline), 2),
            "fii_net_today": last["fii_net"],
            "dii_net_today": last["dii_net"],
        }

    async def upsert_bulk_deals(
        self, deals: list[dict[str, Any]], deal_date: str | None = None,
    ) -> int:
        """Persist bulk/block deals. Each row's date comes from its own
        `deal_date` field when present (the consolidated NSE largedeal
        endpoint returns deals from multiple past sessions); the caller-
        supplied `deal_date` arg is a fallback when the payload omits
        it, and that fallback defaults to today (IST).

        Returns the number of new rows inserted (duplicates ignored via
        unique constraint).

        Skips rows where every payload field beyond symbol is empty.
        Without this guard, a schema change at NSE that renames the
        client_name / buy_sell / quantity / trade_price keys would
        produce one symbol-only row per heartbeat, with the unique
        constraint not catching the dupes (SQLite treats NULL ≠ NULL).
        """
        if not deals:
            return 0
        fallback_date = deal_date or now_ist().strftime("%Y-%m-%d")
        before = (await (await self.conn.execute(
            "SELECT COUNT(*) FROM bulk_deals",
        )).fetchone())[0]
        skipped_empty = 0
        for d in deals:
            sym = str(d.get("symbol") or "").strip()
            if not sym:
                continue
            client_raw = d.get("client_name")
            bs_raw = d.get("buy_sell")
            qty_raw = d.get("quantity")
            price_raw = d.get("trade_price")
            client = str(client_raw or "").strip()
            bs = str(bs_raw or "").strip()
            # Always store numeric values — NEVER NULL — so the
            # UNIQUE(deal_date, symbol, client_name, buy_sell,
            # quantity, trade_price) constraint actually dedupes
            # (SQLite treats NULL != NULL in UNIQUE, so rows with
            # a missing trade_price would otherwise accumulate
            # one new copy per heartbeat on NSE responses that
            # omit the price field).
            try:
                qty_val = int(float(str(qty_raw).replace(",", ""))) if qty_raw else 0
            except (TypeError, ValueError):
                qty_val = 0
            try:
                price_val = (
                    float(str(price_raw).replace(",", "")) if price_raw else 0.0
                )
            except (TypeError, ValueError):
                price_val = 0.0
            # Reject rows where everything beyond symbol is empty —
            # a NSE schema mismatch dropping payload keys would
            # otherwise persist one symbol-only stub row per deal.
            if not client and not bs and qty_val == 0 and price_val == 0.0:
                skipped_empty += 1
                continue
            # Per-deal date when NSE gave us one — else fall back.
            row_date = _normalize_iso_date(d.get("deal_date")) or fallback_date
            await self.conn.execute(
                "INSERT OR IGNORE INTO bulk_deals "
                "(deal_date, symbol, deal_type, client_name, buy_sell, "
                " quantity, trade_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row_date, sym,
                    str(d.get("deal_type") or "bulk"),
                    client, bs, qty_val, price_val,
                ),
            )
        await self.conn.commit()
        if skipped_empty:
            logger.warning(
                "upsert_bulk_deals: skipped %d symbol-only rows (likely "
                "NSE schema mismatch — only deal_type/symbol present)",
                skipped_empty,
            )
        after = (await (await self.conn.execute(
            "SELECT COUNT(*) FROM bulk_deals",
        )).fetchone())[0]
        return after - before

    async def upsert_fii_dii(self, data: dict[str, Any]) -> bool:
        """Persist FII/DII net flows for the day. `data` shape matches
        NSEOfficialSource.fetch_fii_dii output: {date, fii: {...}, dii: {...}}.
        Returns True when a row was written.

        The `date` value from NSE arrives in display format (e.g.
        "15-May-2026"). Normalise to ISO `YYYY-MM-DD` before storing so
        `WHERE date >= date('now', '-30 day')` lookups and `ORDER BY
        date DESC` work — string-compared, "15-May-2026" sorts before
        "2026-04-16" and the institutional-flows dashboard reads
        empty. Other date formats NSE has used historically
        ("15-05-2026", "15/05/2026", "2026-05-15") are also accepted.
        """
        if not data or not data.get("date"):
            return False
        raw_date = str(data["date"]).strip()
        iso_date: str | None = None
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y %H:%M"):
            try:
                iso_date = datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if iso_date is None:
            logger.warning(
                "upsert_fii_dii: skipping row with unparseable date %r", raw_date,
            )
            return False
        fii = data.get("fii") or {}
        dii = data.get("dii") or {}
        # Default missing values to 0.0 — INSERT OR REPLACE so the latest
        # snapshot of the day wins (NSE refreshes mid-day).
        await self.conn.execute(
            "INSERT OR REPLACE INTO fii_dii_daily "
            "(date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                iso_date,
                float(fii.get("buy_value") or 0.0),
                float(fii.get("sell_value") or 0.0),
                float(fii.get("net_value") or 0.0),
                float(dii.get("buy_value") or 0.0),
                float(dii.get("sell_value") or 0.0),
                float(dii.get("net_value") or 0.0),
            ),
        )
        await self.conn.commit()
        return True

    async def count_recent_bulk_deals(
        self, symbol: str, lookback_days: int = 5,
    ) -> dict[str, int]:
        """Return {buy_count, sell_count} of bulk/block deal entries on
        `symbol` within the last `lookback_days` calendar days. Used as
        a live risk-check signal and as an ML feature.
        """
        cursor = await self.read_conn.execute(
            "SELECT buy_sell, COUNT(*) FROM bulk_deals "
            "WHERE symbol = ? AND deal_date >= date('now', ?) "
            "GROUP BY buy_sell",
            (symbol, f"-{int(lookback_days)} day"),
        )
        out = {"buy_count": 0, "sell_count": 0}
        for buy_sell, cnt in await cursor.fetchall():
            if str(buy_sell).upper() == "BUY":
                out["buy_count"] = int(cnt)
            elif str(buy_sell).upper() == "SELL":
                out["sell_count"] = int(cnt)
        return out

    async def get_latest_fii_dii(self) -> dict[str, float] | None:
        """Return the most recent FII/DII row, or None if the table is
        empty. Used by risk_check to gate signals when foreigners are
        net sellers.
        """
        cursor = await self.read_conn.execute(
            "SELECT date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net "
            "FROM fii_dii_daily ORDER BY date DESC LIMIT 1",
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "date": row[0],
            "fii_buy": float(row[1] or 0),
            "fii_sell": float(row[2] or 0),
            "fii_net": float(row[3] or 0),
            "dii_buy": float(row[4] or 0),
            "dii_sell": float(row[5] or 0),
            "dii_net": float(row[6] or 0),
        }

    async def get_symbol_sectors_map(
        self, symbols: list[str] | None = None,
    ) -> dict[str, str]:
        """Return a {symbol: sector} mapping. Prefers symbol_sectors
        (canonical NSE Industry) and falls back to watchlist.sector
        for user-added symbols. Symbols with no sector are omitted.
        """
        if symbols is not None:
            if not symbols:
                return {}
            placeholders = ", ".join(["?"] * len(symbols))
            cursor = await self.read_conn.execute(
                f"SELECT symbol, COALESCE(sector, '') FROM symbol_sectors "
                f"WHERE symbol IN ({placeholders})",
                symbols,
            )
        else:
            cursor = await self.read_conn.execute(
                "SELECT symbol, COALESCE(sector, '') FROM symbol_sectors"
            )
        out: dict[str, str] = {}
        for sym, sector in await cursor.fetchall():
            if sector:
                out[sym] = sector
        # Watchlist fallback for any symbols missing from symbol_sectors.
        if symbols is not None:
            missing = [s for s in symbols if s not in out]
            if missing:
                placeholders = ", ".join(["?"] * len(missing))
                cursor = await self.read_conn.execute(
                    f"SELECT symbol, COALESCE(sector, '') FROM watchlist "
                    f"WHERE symbol IN ({placeholders})",
                    missing,
                )
                for sym, sector in await cursor.fetchall():
                    if sector:
                        out[sym] = sector
        else:
            cursor = await self.read_conn.execute(
                "SELECT symbol, COALESCE(sector, '') FROM watchlist"
            )
            for sym, sector in await cursor.fetchall():
                if sector and sym not in out:
                    out[sym] = sector
        return out

    async def compute_live_regime(
        self, exclude_date: str | None = None,
    ) -> dict[str, float]:
        """Cross-sectional regime stats over the latest two daily closes
        of every tracked symbol. Cheap proxy for "is the broad market
        trending or chopping right now". Heartbeats during market
        hours have today's developing daily bar (Kite returns close =
        current LTP), so by default the comparison is "today-so-far vs
        yesterday" — the live intent the regime risk-gate wants.

        ``exclude_date`` (YYYY-MM-DD) drops that date's bars before
        ranking. The MODEL-feature path passes today's date so
        universe_breadth/avg_return are "last completed session vs the
        one before" — matching training, which only ever sees completed
        sessions (a partial-day breadth is a different distribution).

        Returns: {"breadth": 0..1, "avg_return": float, "sample_size": int}.
        breadth = fraction of symbols up vs prior close. avg_return =
        mean per-symbol % change. sample_size = number of symbols
        that had two consecutive daily bars available.

        Empty / single-symbol result: returns neutral {0.5, 0.0, 0}.
        """
        cursor = await self.read_conn.execute(
            """
            WITH ranked AS (
                SELECT symbol, close,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY timestamp DESC
                       ) AS rn
                FROM ohlcv
                WHERE interval = 'daily'
                  AND timestamp >= date('now', '-10 day')
                  AND (? IS NULL OR substr(timestamp, 1, 10) <> ?)
            )
            SELECT
                MAX(CASE WHEN rn = 1 THEN close END) AS latest,
                MAX(CASE WHEN rn = 2 THEN close END) AS prev
            FROM ranked
            WHERE rn <= 2
            GROUP BY symbol
            HAVING latest > 0 AND prev > 0
            """,
            (exclude_date, exclude_date),
        )
        rows = await cursor.fetchall()
        if not rows:
            return {"breadth": 0.5, "avg_return": 0.0, "sample_size": 0}
        returns = [(float(r[0]) - float(r[1])) / float(r[1]) for r in rows]
        up = sum(1 for x in returns if x > 0)
        return {
            "breadth": up / len(returns),
            "avg_return": sum(returns) / len(returns),
            "sample_size": len(returns),
        }

    async def compute_live_sector_regime(
        self, exclude_date: str | None = None,
    ) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
        """Live per-sector breadth/avg-return plus per-symbol daily return,
        for the inference-time `sector_breadth` / `sector_avg_return` /
        `relative_momentum` features (training computes the same via
        `_compute_sector_index`). Uses the latest two daily closes of every
        tracked symbol, grouped by sector via the symbol_sectors map.
        ``exclude_date`` drops that date's (developing) bars first — see
        compute_live_regime.

        Returns (sector_stats, symbol_returns) where sector_stats[sector] =
        {"breadth", "avg_return", "n"} and symbol_returns[symbol] = pct
        change. A sector needs >= 3 peers to get stats (mirrors training's
        min-peer guard); thinner sectors are simply absent.
        """
        cursor = await self.read_conn.execute(
            """
            WITH ranked AS (
                SELECT symbol, close,
                       ROW_NUMBER() OVER (
                           PARTITION BY symbol ORDER BY timestamp DESC
                       ) AS rn
                FROM ohlcv
                WHERE interval = 'daily'
                  AND timestamp >= date('now', '-10 day')
                  AND (? IS NULL OR substr(timestamp, 1, 10) <> ?)
            )
            SELECT symbol,
                   MAX(CASE WHEN rn = 1 THEN close END) AS latest,
                   MAX(CASE WHEN rn = 2 THEN close END) AS prev
            FROM ranked
            WHERE rn <= 2
            GROUP BY symbol
            HAVING latest > 0 AND prev > 0
            """,
            (exclude_date, exclude_date),
        )
        rows = await cursor.fetchall()
        symbol_returns: dict[str, float] = {}
        for sym, latest, prev in rows:
            try:
                symbol_returns[sym] = (float(latest) - float(prev)) / float(prev)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if not symbol_returns:
            return {}, {}
        sector_map = await self.get_symbol_sectors_map(list(symbol_returns.keys()))
        by_sector: dict[str, list[float]] = {}
        for sym, ret in symbol_returns.items():
            sec = sector_map.get(sym)
            if sec:
                by_sector.setdefault(sec, []).append(ret)
        sector_stats: dict[str, dict[str, float]] = {}
        for sec, rets in by_sector.items():
            if len(rets) < 3:
                continue
            up = sum(1 for x in rets if x > 0)
            sector_stats[sec] = {
                "breadth": up / len(rets),
                "avg_return": sum(rets) / len(rets),
                "n": len(rets),
            }
        return sector_stats, symbol_returns

    async def compute_market_trend(self, ma_window: int = 50) -> dict[str, Any]:
        """Equal-weight market-index trend vs its moving average — the
        long-only circuit-breaker signal. Builds an index from the mean
        daily return across all tracked symbols, then reports whether the
        latest index level is at/above its trailing `ma_window`-day MA.

        Returns: {"in_uptrend": bool, "index_level": float, "ma": float,
                  "sample_size": int, "ma_window": int}. Neutral
        (in_uptrend=True, sample_size=0) when there isn't enough history —
        fail-open so a cold cache never blocks trading.
        """
        lookback = ma_window + 10
        cursor = await self.read_conn.execute(
            "SELECT symbol, timestamp, close FROM ohlcv "
            "WHERE interval = 'daily' AND timestamp >= date('now', ?) "
            "ORDER BY symbol, timestamp",
            (f"-{int(lookback)} day",),
        )
        rows = await cursor.fetchall()
        neutral = {
            "in_uptrend": True, "index_level": 1.0, "ma": 1.0,
            "sample_size": 0, "ma_window": ma_window,
        }
        if not rows:
            return neutral
        by_symbol: dict[str, list[tuple[str, float]]] = {}
        for sym, ts, close in rows:
            try:
                c = float(close)
            except (TypeError, ValueError):
                continue
            by_symbol.setdefault(sym, []).append((str(ts)[:10], c))
        ret_sum: dict[str, float] = {}
        ret_cnt: dict[str, int] = {}
        for series in by_symbol.values():
            series.sort()
            for i in range(1, len(series)):
                pc = series[i - 1][1]
                if pc > 0:
                    d = series[i][0]
                    ret_sum[d] = ret_sum.get(d, 0.0) + (series[i][1] / pc - 1)
                    ret_cnt[d] = ret_cnt.get(d, 0) + 1
        dates = sorted(ret_cnt)
        if len(dates) < 2:
            return neutral
        level = 1.0
        levels: list[float] = []
        for d in dates:
            level *= (1 + ret_sum[d] / ret_cnt[d])
            levels.append(level)
        window = levels[-ma_window:] if len(levels) >= ma_window else levels
        ma = sum(window) / len(window)
        latest = levels[-1]
        return {
            "in_uptrend": latest >= ma,
            "index_level": latest,
            "ma": ma,
            "sample_size": ret_cnt[dates[-1]],
            "ma_window": ma_window,
        }

    async def minutes_since_last_loss_for_symbol(
        self, symbol: str, mode: str | None = None,
    ) -> float:
        """Minutes since the most recent losing trade closed for this
        symbol+mode. Returns a large sentinel (999999) when the symbol
        has never recorded a loss in the current mode.
        """
        mode_clause = " AND mode = ?" if mode else ""
        params: tuple[Any, ...] = (
            (symbol, mode) if mode else (symbol,)
        )
        cursor = await self.read_conn.execute(
            f"SELECT closed_at FROM trades "
            f"WHERE symbol = ? AND pnl IS NOT NULL AND pnl < 0{mode_clause} "
            f"ORDER BY closed_at DESC LIMIT 1",
            params,
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return 999999.0
        last_loss_time = datetime.fromisoformat(row[0])
        if last_loss_time.tzinfo is None:
            last_loss_time = last_loss_time.replace(tzinfo=IST)
        now = datetime.now(IST)
        return (now - last_loss_time).total_seconds() / 60

    async def get_feedback_data(self, lookback_days: int = 14) -> dict[str, dict[str, float]]:
        """Get per-symbol feedback stats from recent predictions, dry runs, and trades.

        Returns a dict keyed by symbol with rolling accuracy, PnL, slippage stats.
        Used to augment ML training features and compute sample weights.
        """
        from datetime import timedelta

        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        feedback: dict[str, dict[str, float]] = {}

        def _ensure(sym: str) -> dict[str, float]:
            if sym not in feedback:
                feedback[sym] = {}
            return feedback[sym]

        # 1. Prediction outcomes
        try:
            cursor = await self.read_conn.execute(
                "SELECT COALESCE(p.symbol, s.symbol, t.symbol) as symbol, "
                "p.direction_correct, p.target_hit, p.actual_pnl_pct "
                "FROM predictions p "
                "LEFT JOIN signals s ON p.signal_id = s.id "
                "LEFT JOIN trades t ON p.trade_id = t.trade_id "
                "WHERE p.actual_price IS NOT NULL AND p.scored_at >= ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            sym_preds: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                sym = r[0]
                if not sym:
                    continue
                sym_preds.setdefault(sym, []).append({
                    "correct": r[1], "target_hit": r[2], "pnl_pct": r[3],
                })
            for sym, preds in sym_preds.items():
                fb = _ensure(sym)
                n = len(preds)
                fb["pred_count"] = float(n)
                fb["pred_accuracy"] = sum(1 for p in preds if p["correct"]) / n
                fb["pred_target_hit_rate"] = sum(1 for p in preds if p["target_hit"]) / n
                pnls = [p["pnl_pct"] for p in preds if p["pnl_pct"] is not None]
                fb["pred_avg_pnl_pct"] = sum(pnls) / len(pnls) if pnls else 0.0
        except Exception as e:
            logger.warning("Feedback: prediction query failed: %s", e)

        # 2. Dry run scores
        try:
            cursor = await self.read_conn.execute(
                "SELECT symbol, direction_correct, target_hit, actual_move_pct "
                "FROM dry_run_results "
                "WHERE scored_at IS NOT NULL AND scored_at >= ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            sym_dr: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                sym = r[0]
                if not sym:
                    continue
                sym_dr.setdefault(sym, []).append({
                    "correct": r[1], "target_hit": r[2], "move_pct": r[3],
                })
            for sym, drs in sym_dr.items():
                fb = _ensure(sym)
                n = len(drs)
                fb["dry_run_count"] = float(n)
                fb["dry_run_accuracy"] = sum(1 for d in drs if d["correct"]) / n
                moves = [d["move_pct"] for d in drs if d["move_pct"] is not None]
                fb["dry_run_avg_move_pct"] = sum(moves) / len(moves) if moves else 0.0
        except Exception as e:
            logger.warning("Feedback: dry run query failed: %s", e)

        # 3. Closed trades
        try:
            cursor = await self.read_conn.execute(
                "SELECT symbol, pnl, slippage, entry_price, fill_price "
                "FROM trades "
                "WHERE closed_at IS NOT NULL AND closed_at >= ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            sym_trades: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                sym = r[0]
                if not sym:
                    continue
                entry = r[3] or 1
                sym_trades.setdefault(sym, []).append({
                    "pnl": r[1], "slippage_pct": abs(r[2] or 0) / entry * 100,
                })
            for sym, trades in sym_trades.items():
                fb = _ensure(sym)
                n = len(trades)
                fb["trade_count"] = float(n)
                fb["trade_win_rate"] = sum(1 for t in trades if (t["pnl"] or 0) > 0) / n
                fb["trade_loss_count"] = float(
                    sum(1 for t in trades if (t["pnl"] or 0) < 0),
                )
                pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
                fb["trade_avg_pnl"] = sum(pnls) / len(pnls) if pnls else 0.0
                slips = [t["slippage_pct"] for t in trades]
                fb["trade_avg_slippage_pct"] = sum(slips) / len(slips) if slips else 0.0
        except Exception as e:
            logger.warning("Feedback: trades query failed: %s", e)

        return feedback

    # ------------------------------------------------------------------
