"""Dry-run signal preview and scoring.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
)

from yolovest.dashboard.helpers import (
    _compute_scan_scores,
)

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

    # ------------------------------------------------------------------
    # Dry-Run Signal Preview
    # ------------------------------------------------------------------

    @app.post("/api/dry-run")
    async def run_dry_run_signals(
        mode: str | None = Query(
            default=None,
            pattern=r"^(intraday|short_term|balanced|long_term|swing)$",
            description="Strategy mode override",
        ),
        as_of: str | None = Query(
            default=None,
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="Historical date (YYYY-MM-DD). Evaluate signals as of this "
                        "past day using only bars up to it (no look-ahead). "
                        "Omit for the latest market data.",
        ),
        model_version: str | None = Query(
            default=None,
            pattern=r"^[A-Za-z0-9_.\-]+$",
            description="Evaluate against a specific model version (shadow / "
                        "retired / production) instead of the loaded production "
                        "model. Omit to use whatever is currently in production.",
        ),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Run market-scan + signal generation on current data (read-only, no trades).

        Works regardless of market hours. Results are stored for next-day comparison.
        Optional `mode` query param overrides the configured strategy.mode for this run.
        Optional `as_of` evaluates signals as they'd have looked on a past date.
        """
        from datetime import datetime as dt

        from yolovest.config import _MODE_HOLDING_DAYS, _MODE_HOLDING_PERIODS

        # holding-period decision and target/SL geometry now live inside
        # the shared signal_evaluator — no direct imports needed here.
        from yolovest.timezone import IST

        run_id = str(uuid.uuid4())[:8]
        cfg = ctx.config

        # Historical "as of" date → end-of-day IST timestamp used to bound
        # the bar fetch. When set, we evaluate purely on bars up to that
        # day and use each symbol's as-of close as the price (no live LTP),
        # so the preview reflects what the model would have signalled then.
        as_of_dt: dt | None = None
        if as_of:
            try:
                _d = dt.strptime(as_of, "%Y-%m-%d")
                as_of_dt = _d.replace(hour=23, minute=59, second=59, tzinfo=IST)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"Invalid as_of date: {as_of}",
                ) from None

        # Resolve effective strategy mode and allowed holding periods
        effective_mode = mode or cfg.strategy.mode
        mode_days_range = _MODE_HOLDING_DAYS.get(effective_mode)
        allowed_periods = _MODE_HOLDING_PERIODS.get(
            effective_mode, cfg.strategy.allowed_holding_periods or ["intraday", "short_term", "long_term"],
        )

        # Optional model override: evaluate against a specific (shadow /
        # retired / production) version instead of the loaded production
        # model. We load it into a SEPARATE ML instance and run the eval
        # through a ctx copy whose `.ml` points at it — the live engine's
        # ctx.ml is never mutated, so a concurrent heartbeat keeps using
        # production. The other model slot is pre-loaded from production so
        # every strategy mode (e.g. balanced) still works.
        import dataclasses as _dataclasses

        override_ml = None
        selected_model: dict[str, Any] | None = None
        if model_version:
            override_ml, selected_model = await _load_model_override(
                ctx, cfg, model_version,
            )

        run_ctx = (
            _dataclasses.replace(ctx, ml=override_ml) if override_ml is not None else ctx
        )

        # Step 1: Run market-scan logic (without writing to watchlist)
        universe, shortlist = await _build_shortlist(ctx, cfg)

        if not shortlist:
            return {
                "success": True,
                "run_id": run_id,
                "mode": effective_mode,
                "as_of": as_of,
                "selected_model": selected_model,
                "universe_size": len(universe),
                "shortlist_size": 0,
                "signals": [],
                "diagnostics": {
                    "min_confidence_threshold": min(cfg.risk.min_confidence_buy, cfg.risk.min_confidence_sell),
                    "min_confidence_buy": cfg.risk.min_confidence_buy,
                    "min_confidence_sell": cfg.risk.min_confidence_sell,
                    "ml_available": run_ctx.ml is not None,
                    "filter_counts": {
                        "insufficient_bars": 0, "feature_computation_failed": 0,
                        "ml_unavailable": 0, "hold_signal": 0,
                        "low_confidence": 0, "error": 0, "passed": 0,
                    },
                    "rejection_details": [],
                },
            }

        # Step 2: Generate signals from shortlisted stocks via the
        # shared evaluator. Same code path the production heartbeat
        # uses — dry-run and live trading agree by construction.
        ev = await _evaluate_shortlist(
            ctx, run_ctx, cfg, shortlist,
            as_of_dt=as_of_dt,
            effective_mode=effective_mode,
            allowed_periods=allowed_periods,
            mode_days_range=mode_days_range,
        )
        signals_out = ev.signals_out
        filter_counts = ev.filter_counts
        rejection_details = ev.rejection_details
        ml_unavailable = ev.ml_unavailable

        conviction, eff_thr = _conviction_summary(
            run_ctx, effective_mode, ev.conviction_buy, ev.conviction_sell,
        )

        # Log diagnostics summary (always, not just on 0 signals)
        logger.info(
            "Dry-run %s (%s mode, as_of=%s) complete: scanned %d, shortlisted %d, "
            "generated %d signals — %s | conviction: max_buy=%.2f max_sell=%.2f "
            "buy>=.55=%.0f%% sell>=.60=%.0f%% eff_thr=%s",
            run_id, effective_mode, as_of or "latest", len(universe), len(shortlist),
            len(signals_out), filter_counts,
            conviction["max_buy"], conviction["max_sell"],
            conviction["buy_ge_0.55"] * 100, conviction["sell_ge_0.60"] * 100, eff_thr,
        )

        # Step 3: Persist for next-day comparison
        scoring: dict[str, Any] | None = None
        if signals_out:
            await ctx.db.insert_dry_run_results(run_id, signals_out, as_of=as_of)

            # For a historical (as_of) run, the holding windows of some or
            # all signals already lie in the past — score them right away
            # so the user doesn't have to click "Score". score_dry_run is
            # partial: anything whose window hasn't elapsed yet is left
            # pending for a later pass. Latest-data (as_of=None) runs always
            # predict the future, so there's nothing to score yet.
            if as_of:
                try:
                    scoring = await ctx.db.score_dry_run(run_id)
                    logger.info("Dry-run %s auto-scored (as_of=%s): %s",
                                run_id, as_of, scoring)
                except Exception:
                    logger.warning("Dry-run %s auto-score failed", run_id, exc_info=True)

        result: dict[str, Any] = {
            "success": True,
            "run_id": run_id,
            "mode": effective_mode,
            "as_of": as_of,
            "selected_model": selected_model,
            "universe_size": len(universe),
            "shortlist_size": len(shortlist),
            "signals": signals_out,
            "scoring": scoring,
            "diagnostics": {
                "min_confidence_threshold": min(cfg.risk.min_confidence_buy, cfg.risk.min_confidence_sell),
                "min_confidence_buy": cfg.risk.min_confidence_buy,
                "min_confidence_sell": cfg.risk.min_confidence_sell,
                "ml_available": run_ctx.ml is not None,
                "filter_counts": filter_counts,
                "rejection_details": rejection_details,
                "conviction": conviction,
            },
        }
        if ml_unavailable:
            result["warning"] = (
                "ML model is not loaded — 0 signals generated. "
                "Run the model-retrain skill first to train an XGBoost model, "
                "then re-run the dry run."
            )
        return result

    @app.get("/api/dry-run/history")
    async def get_dry_run_history(
        limit: int = Query(default=10, ge=1, le=50),
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get past dry-run summaries."""
        return await ctx.db.get_dry_run_history(limit)

    @app.get("/api/dry-run/{run_id}")
    async def get_dry_run_detail(
        run_id: str,
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get all signals for a specific dry-run.

        Each signal is enriched with its target (predicted-exit) date and
        the cost-adjusted net gain / loss, mirroring the recommendations
        view. The base date is the run's `as_of` (historical run) or the
        row's created date (latest-data run); the target date is that plus
        the expected holding-day horizon.
        """
        from yolovest.dashboard.helpers import compute_signal_economics

        rows = await ctx.db.get_dry_run_signals(run_id)
        for r in rows:
            r.update(compute_signal_economics(
                ctx,
                signal_type=r.get("signal_type"),
                entry_price=r.get("entry_price"),
                target_price=r.get("target_price"),
                stop_loss_price=r.get("stop_loss_price"),
                position_size=r.get("position_size"),
                product=r.get("product"),
                base_date=r.get("as_of") or r.get("created_at"),
                expected_holding_days=r.get("expected_holding_days"),
            ))
        return rows

    @app.post("/api/dry-run/{run_id}/score")
    async def score_dry_run(
        run_id: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Score a dry-run against actual next-day market data."""
        return await ctx.db.score_dry_run(run_id)

    @app.delete("/api/dry-run/{run_id}")
    async def delete_dry_run(
        run_id: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a dry-run and all its signals."""
        deleted = await ctx.db.delete_dry_run(run_id)
        logger.info("Dry-run %s deleted (%d signals removed)", run_id, deleted)
        return {"success": True, "run_id": run_id, "deleted": deleted}


@dataclass
class _DryRunEval:
    """Per-run accumulator returned by _evaluate_shortlist."""

    signals_out: list[dict[str, Any]]
    filter_counts: dict[str, int]
    rejection_details: list[dict[str, str]]
    conviction_buy: list[float]
    conviction_sell: list[float]
    ml_unavailable: bool


async def _load_model_override(
    ctx: "AppContext", cfg: Any, model_version: str,
) -> tuple[Any, dict[str, Any] | None]:
    """Load a specific (shadow / retired / production) model version into a
    SEPARATE ML instance for this run — the live engine's ctx.ml is never
    mutated, so a concurrent heartbeat keeps using production. The other
    model slot is pre-loaded from production so every strategy mode (e.g.
    balanced) still works."""
    override_ml = None
    selected_model: dict[str, Any] | None = None
    if model_version:
        if ctx.ml is None:
            raise HTTPException(status_code=400, detail="ML subsystem unavailable")
        row = await ctx.db.get_model_version(model_version)
        if not row:
            raise HTTPException(
                status_code=404, detail=f"Unknown model version: {model_version}",
            )
        override_type = row["model_type"]
        from yolovest.strategy.ml_signal import XGBoostSignalModel

        _model_dir = getattr(cfg.strategy, "model_dir", "./models")
        override_ml = XGBoostSignalModel(model_dir=_model_dir, db=ctx.db, config=cfg)
        for _t in ("intraday", "swing"):
            try:
                _prod = await ctx.db.get_production_model(_t)
                if _prod and _prod.get("version"):
                    await override_ml.load_model(_t, _prod["version"])
            except Exception:
                logger.debug("dry-run: preload production %s failed", _t, exc_info=True)
        try:
            await override_ml.load_model(override_type, model_version)
        except FileNotFoundError:
            raise HTTPException(
                status_code=400,
                detail=f"Model file for '{model_version}' not found on disk",
            ) from None
        selected_model = {
            "version": model_version,
            "model_type": override_type,
            "status": row.get("status"),
        }
        logger.info(
            "Dry-run: evaluating against non-production %s model %s (status=%s)",
            override_type, model_version, row.get("status"),
        )
    return override_ml, selected_model


async def _build_shortlist(
    ctx: "AppContext", cfg: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Market-scan logic without writing to the watchlist: liquidity
    filter -> composite scoring -> top shortlist_size. Returns
    (universe, shortlist)."""
    universe = await ctx.db.get_nse_universe()
    if not universe:
        universe = [
            {"symbol": s, "avg_daily_volume": cfg.scanning.min_avg_daily_volume + 1}
            for s in cfg.scanning.seed_symbols
        ]
    liquid = [
        s for s in universe
        if (s.get("avg_daily_volume") or 0) >= cfg.scanning.min_avg_daily_volume
    ]

    # Score stocks (including volatility)
    weights = cfg.scanning.weights
    scored = []
    for stock in liquid:
        sub = _compute_scan_scores(stock, cfg.scanning.min_avg_daily_volume, cfg.strategy.volatility)
        composite = (
            sub["technical_score"] * weights.technical
            + sub["volume_momentum_score"] * weights.volume_momentum
            + sub["news_sentiment_score"] * weights.news_sentiment
            + sub["fundamental_score"] * weights.fundamental
            + sub["volatility_score"] * weights.volatility
        )
        scored.append({**stock, **sub, "composite_score": composite})

    scored.sort(
        key=lambda s: (s["composite_score"], s.get("avg_daily_volume") or 0),
        reverse=True,
    )
    shortlist = scored[: cfg.scanning.shortlist_size]
    return universe, shortlist


async def _evaluate_shortlist(
    ctx: "AppContext",
    run_ctx: "AppContext",
    cfg: Any,
    shortlist: list[dict[str, Any]],
    *,
    as_of_dt: Any,
    effective_mode: str,
    allowed_periods: list[str],
    mode_days_range: Any,
) -> _DryRunEval:
    """Run the shared signal evaluator over the shortlist with full
    feature parity to the live heartbeat (news / VIX / F&O / regime /
    sector / institutional / feedback merges), collecting per-symbol
    outcomes and conviction diagnostics."""
    from datetime import datetime as dt
    from datetime import timedelta

    from yolovest.costs import compute_transaction_costs
    from yolovest.data.features import IndicatorConfig, compute_features
    from yolovest.skills.generate_signals import _format_class_probs
    from yolovest.strategy.signal_evaluator import evaluate_symbol_signal
    from yolovest.timezone import IST

    signals_out: list[dict[str, Any]] = []
    ml_unavailable = run_ctx.ml is None

    # Build held + locked symbol sets (the evaluator needs both
    # for SELL adjustment and lock skip)
    open_positions = await ctx.db.get_open_positions()
    held_symbols = {p["symbol"] for p in open_positions}
    locked_symbols = set(await ctx.db.get_locked_symbols())

    if ml_unavailable:
        logger.warning("Dry-run: ML model not loaded — cannot generate signals. "
                       "Train a model first via the model-retrain skill.")

    # Market regime (same source generate-signals reads from)
    regime_state = None
    if cfg.strategy.market_regime.enabled:
        regime_state = await ctx.db.get_system_state("market_regime")

    # Diagnostics: track why stocks get filtered out. Buckets
    # mirror the SignalEvaluation.outcome enum + the pre-
    # evaluator gates (insufficient_bars / feature_computation
    # _failed / ml_unavailable / error).
    filter_counts: dict[str, int] = {
        "insufficient_bars": 0,
        "feature_computation_failed": 0,
        "ml_unavailable": 0,
        "hold_signal": 0,
        "low_confidence": 0,
        "error": 0,
        "passed": 0,
    }
    rejection_details: list[dict[str, str]] = []
    # Conviction diagnostic: the model's per-stock directional
    # probability. Makes the real blocker visible — if these rarely
    # reach the tuned thresholds, it's a model-conviction ceiling, not
    # a data/threshold-tuning problem.
    conviction_buy: list[float] = []
    conviction_sell: list[float] = []

    indicator_cfg = IndicatorConfig(
        ema_periods=cfg.strategy.ema_periods,
        rsi=cfg.strategy.indicators.rsi,
        macd=cfg.strategy.indicators.macd,
        bollinger_bands=cfg.strategy.indicators.bollinger_bands,
        vwap=cfg.strategy.indicators.vwap,
        atr=cfg.strategy.indicators.atr,
        volume_profile=cfg.strategy.indicators.volume_profile,
        obv=cfg.strategy.indicators.obv,
        supertrend=cfg.strategy.indicators.supertrend,
        # Dry-run must mirror the live swing engine's feature set.
        extended_momentum=cfg.strategy.indicators.extended_momentum,
    )

    # Feature parity with the live heartbeat: the model trains on news,
    # VIX, F&O, regime, sector, institutional and feedback features.
    # Merge them here too so the dry-run mirrors live (without this the
    # model is fed ~30 zeroed features and never signals).
    from yolovest.data.fno_features import FNO_FEATURE_KEYS, compute_fno_features
    from yolovest.data.news_features import NEWS_FEATURE_KEYS, compute_news_features
    from yolovest.data.vix_features import (
        VIX_FEATURE_KEYS,
        compute_vix_features,
    )
    from yolovest.strategy.inference_features import (
        enrich_features,
        load_inference_feature_context,
    )

    _ref_dt = as_of_dt or dt.now(IST)
    _today_str = _ref_dt.strftime("%Y-%m-%d")
    try:
        _vix_timeline = await ctx.db.get_vix_timeline(
            date_from=(_ref_dt - timedelta(days=40)).strftime("%Y-%m-%d"),
        )
        vix_feats_today = compute_vix_features(_vix_timeline, _today_str)
    except Exception:
        vix_feats_today = {k: 0.0 for k in VIX_FEATURE_KEYS}
    try:
        fno_lookup = await ctx.db.get_fno_timeline(
            date_from=(_ref_dt - timedelta(days=5)).strftime("%Y-%m-%d"),
        )
    except Exception:
        fno_lookup = {}
    inference_ctx = await load_inference_feature_context(run_ctx)

    for stock in shortlist:
        symbol = stock["symbol"]
        try:
            bars = await ctx.db.get_ohlcv(
                symbol, "daily", days=365, end=as_of_dt,
            )
            # Drop the reference date's developing/own bar — mirrors the
            # heartbeat's filter in generate_signals so a dry-run "as of
            # day D" sees exactly the window the live engine saw on D
            # (features as-of the last COMPLETED session).
            _ref_date = _ref_dt.date()
            bars = [
                b for b in bars
                if (
                    b.timestamp.astimezone(IST) if b.timestamp.tzinfo
                    else b.timestamp
                ).date() < _ref_date
            ]
            if len(bars) < 50:
                filter_counts["insufficient_bars"] += 1
                rejection_details.append({
                    "symbol": symbol,
                    "reason": "insufficient_bars",
                    "detail": f"{len(bars)} bars < 50 required",
                })
                logger.info("Dry-run: Insufficient data for %s (%d bars)", symbol, len(bars))
                continue

            features = compute_features(bars, indicator_cfg)
            if not features:
                filter_counts["feature_computation_failed"] += 1
                rejection_details.append({
                    "symbol": symbol,
                    "reason": "feature_computation_failed",
                    "detail": "compute_features returned empty",
                })
                logger.info("Dry-run: Feature computation failed for %s", symbol)
                continue

            # Merge the full training feature set (mirror generate_signals)
            # so the dry-run feeds the model the same 54 features it
            # trained on, not ~22 with the rest zeroed.
            try:
                news_rows = await ctx.db.get_news_articles(
                    symbol=symbol,
                    date_from=(_ref_dt - timedelta(days=7)).isoformat(),
                    limit=500,
                )
                _heads = []
                for r in news_rows:
                    _p = r.get("published_at")
                    if not _p:
                        continue
                    try:
                        _pd = dt.fromisoformat(_p)
                        if _pd.tzinfo is None:
                            _pd = _pd.replace(tzinfo=IST)
                        _heads.append((r.get("headline", ""), _pd))
                    except (ValueError, TypeError):
                        continue
                features.update(compute_news_features(_heads, _ref_dt))
            except Exception:
                features.update({k: 0.0 for k in NEWS_FEATURE_KEYS})
            features.update(vix_feats_today)
            _sym_fno = fno_lookup.get(symbol)
            if _sym_fno:
                _pc = bars[-2].close if len(bars) >= 2 else None
                features.update(compute_fno_features(
                    _sym_fno, _today_str,
                    prior_stock_close=_pc, current_stock_close=bars[-1].close,
                ))
            else:
                features.update({k: 0.0 for k in FNO_FEATURE_KEYS})
            await enrich_features(run_ctx, symbol, features, inference_ctx)

            if run_ctx.ml is None:
                filter_counts["ml_unavailable"] += 1
                rejection_details.append({
                    "symbol": symbol,
                    "reason": "ml_unavailable",
                    "detail": "ML model not loaded",
                })
                continue

            # Fetch fresh LTP for realistic entry/target/SL. For a
            # historical (as_of) run, live LTP would be look-ahead —
            # leave current_price None so the evaluator uses the as-of
            # bar close instead.
            current_price: float | None = None
            if as_of_dt is None:
                try:
                    current_price = await ctx.market_data.get_ltp(symbol)
                except Exception:
                    logger.debug("LTP unavailable for dry-run %s, using bar close", symbol)

            evaluation = await evaluate_symbol_signal(
                run_ctx, symbol, features,
                current_price=current_price,
                held_symbols=held_symbols,
                locked_symbols=locked_symbols,
                now_time=dt.now(IST).time(),
                effective_mode=effective_mode,
                allowed_periods=allowed_periods,
                mode_days_range=mode_days_range,
                existing_positions=open_positions,
                market_regime=regime_state,
                # Dry-run is a preview — don't let time-of-day
                # execution gates (intraday cutoff etc.) suppress
                # signals the model would produce earlier in the day.
                bypass_time_gates=True,
            )

            _cp = evaluation.class_probabilities or {}
            if _cp:
                conviction_buy.append(float(_cp.get("BUY", 0.0)))
                conviction_sell.append(float(_cp.get("SELL", 0.0)))

            if evaluation.outcome != "passed":
                bucket = evaluation.outcome
                filter_counts.setdefault(bucket, 0)
                filter_counts[bucket] += 1
                rejection_details.append({
                    "symbol": symbol,
                    "reason": bucket,
                    "detail": evaluation.detail,
                })
                continue

            # Passed all evaluator gates. Compute transaction costs
            # (dry-run-only — production builds the signal dict
            # without these as risk-check needs the raw figures).
            est_costs = compute_transaction_costs(
                evaluation.entry_price, evaluation.target_price,
                evaluation.prediction.position_size,  # type: ignore[union-attr]  # passed => prediction set
                product=evaluation.product,
                cost_config=cfg.transaction_costs,
            )

            filter_counts["passed"] += 1
            logger.info(
                "Dry-run: PASSED %s for %s @ %.2f (%s)",
                evaluation.signal_type, symbol,
                evaluation.confidence,
                _format_class_probs(evaluation.prediction),
            )
            signals_out.append({
                "symbol": symbol,
                "signal_type": evaluation.signal_type,
                "entry_price": evaluation.entry_price,
                "target_price": evaluation.target_price,
                "stop_loss_price": evaluation.stop_loss_price,
                "confidence_score": evaluation.confidence,
                "position_size": evaluation.prediction.position_size,  # type: ignore[union-attr]  # passed => prediction set
                "model_version": evaluation.model_version,
                "holding_period": evaluation.holding_period,
                "expected_holding_days": evaluation.expected_days,
                "product": evaluation.product,
                "estimated_costs": est_costs,
                "composite_score": stock.get("composite_score"),
                "technical_score": stock.get("technical_score"),
                "volume_momentum_score": stock.get("volume_momentum_score"),
                "news_sentiment_score": stock.get("news_sentiment_score"),
                "fundamental_score": stock.get("fundamental_score"),
                "volatility_score": stock.get("volatility_score"),
                "strategy_mode": effective_mode,
            })
        except Exception as e:
            filter_counts["error"] += 1
            rejection_details.append({
                "symbol": symbol,
                "reason": "error",
                "detail": str(e),
            })
            logger.warning("Dry-run signal failed for %s: %s", symbol, e)
    return _DryRunEval(
        signals_out=signals_out,
        filter_counts=filter_counts,
        rejection_details=rejection_details,
        conviction_buy=conviction_buy,
        conviction_sell=conviction_sell,
        ml_unavailable=ml_unavailable,
    )


def _conviction_summary(
    run_ctx: "AppContext",
    effective_mode: str,
    conviction_buy: list[float],
    conviction_sell: list[float],
) -> tuple[dict[str, Any], Any]:
    """Summarize the model's reachable directional probability vs the
    gate it must clear — when max conviction sits below the effective
    threshold, that IS the blocker, independent of date/regime."""
    def _pct_ge(vals: list[float], t: float) -> float:
        return (sum(1 for v in vals if v >= t) / len(vals)) if vals else 0.0

    eff_thr = None
    try:
        _mt = "intraday" if effective_mode == "intraday" else "swing"
        eff_thr = run_ctx.ml.get_effective_thresholds(_mt) if run_ctx.ml else None
    except Exception:
        eff_thr = None
    conviction: dict[str, Any] = {
        "max_buy": round(max(conviction_buy), 4) if conviction_buy else 0.0,
        "max_sell": round(max(conviction_sell), 4) if conviction_sell else 0.0,
        "buy_ge_0.45": round(_pct_ge(conviction_buy, 0.45), 4),
        "buy_ge_0.50": round(_pct_ge(conviction_buy, 0.50), 4),
        "buy_ge_0.55": round(_pct_ge(conviction_buy, 0.55), 4),
        "sell_ge_0.55": round(_pct_ge(conviction_sell, 0.55), 4),
        "sell_ge_0.60": round(_pct_ge(conviction_sell, 0.60), 4),
        "effective_thresholds": eff_thr,
        "n_scored": len(conviction_buy),
    }
    return conviction, eff_thr
