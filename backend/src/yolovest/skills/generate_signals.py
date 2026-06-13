"""Skill: generate-signals — ML-based trade signal generation.

Trigger: HEARTBEAT during market hours
Pipeline position: After market-scan, before risk-check.

Flow:
1. Load current watchlist from DB
2. For each watchlist stock, compute features
3. Run appropriate ML model
4. Generate signal with required fields
5. Filter: only emit signals where confidence >= per-direction threshold
   (risk.min_confidence_buy for BUY, risk.min_confidence_sell for SELL)
6. Emit signals as events for risk-check skill to consume
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from yolovest.data.features import (
    IndicatorConfig,
    compute_daily_trend_features,
    compute_features,
)
from yolovest.data.fno_features import FNO_FEATURE_KEYS, compute_fno_features
from yolovest.data.news_features import NEWS_FEATURE_KEYS, compute_news_features
from yolovest.data.vix_features import VIX_FEATURE_KEYS, compute_vix_features
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.strategy.inference_features import (
    enrich_features,
    load_inference_feature_context,
)
from yolovest.strategy.signal_evaluator import evaluate_symbol_signal
from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)


def _format_class_probs(prediction: Any) -> str:
    """Render a prediction's per-class probability vector for logs.

    Falls back to single-confidence form when the model didn't expose
    its class_probabilities (older shadow models, defensive paths).
    """
    probs = getattr(prediction, "class_probabilities", None)
    if not probs:
        return f"confidence {getattr(prediction, 'confidence', 0):.2f}"
    # Stable ordering with BUY first so a "zero BUYs" pattern is
    # impossible to miss in a long log block.
    return " ".join(
        f"{label}={probs[label]:.2f}"
        for label in ("BUY", "HOLD", "SELL")
        if label in probs
    )


class GenerateSignalsSkill(SkillBase):
    name = "generate-signals"
    description = "Run ML models on watchlist to produce trade signals"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_market_hours())

    async def execute(self, **kwargs: Any) -> SkillResult:
        # Drift-watch hard suspension. When drift_auto_suspend_enabled
        # is on, drift-watch sets the signal_gen_suspended_by_drift
        # flag after detecting >15pp win-rate decay or class collapse.
        # Block all signal generation until the next successful
        # model-retrain (which clears the flag) — the position monitor
        # still runs so open trades keep their SL / target.
        try:
            suspension_reason = await self.ctx.db.get_system_state(
                "signal_gen_suspended_by_drift",
            )
        except Exception:
            suspension_reason = None
        if suspension_reason:
            logger.warning(
                "generate-signals SUSPENDED by drift-watch (reason: %s) — "
                "run model-retrain or clear the flag from the dashboard "
                "to resume.",
                suspension_reason,
            )
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={
                    "suspended": True,
                    "reason": suspension_reason,
                    "signals": [],
                    "filter_counts": {"drift_suspended": 1},
                },
            )

        watchlist = await self.ctx.db.get_combined_watchlist()
        # Apply quarantine policy to watchlist entries:
        #   - Quarantined + replacement → rewrite the entry's symbol
        #     to the replacement (preserves its composite scores).
        #   - Quarantined + no replacement → drop the entry.
        #   - Active symbol → keep as-is.
        # Algorithmic watchlist is already quarantine-clean (market-scan reads
        # from get_nse_universe which excludes quarantined), but user_watchlist
        # entries pinned before quarantine can otherwise leak through here.
        repl = await self.ctx.db.get_quarantine_replacements()
        quarantined = await self.ctx.db.get_all_quarantined_symbol_set()
        if repl or quarantined:
            filtered: list[dict[str, Any]] = []
            seen: set[str] = set()
            for w in watchlist:
                sym = w["symbol"]
                if sym in quarantined:
                    target = repl.get(sym)
                    if not target:
                        continue  # drop
                    w = {**w, "symbol": target}
                    sym = target
                if sym in seen:
                    continue
                seen.add(sym)
                filtered.append(w)
            watchlist = filtered
        signals_generated: list[dict[str, Any]] = []
        risk_cfg = self.ctx.config.risk
        min_confidence_buy = risk_cfg.min_confidence_buy
        min_confidence_sell = risk_cfg.min_confidence_sell
        # Legacy threshold kept for diagnostics
        min_confidence = min(min_confidence_buy, min_confidence_sell)
        rotation_cfg = self.ctx.config.scanning
        # Tracks per-symbol signal productivity for watchlist rotation
        outcome_tracker: dict[str, bool] = {}

        # Diagnostics: track why stocks get filtered out
        filter_counts = {
            "insufficient_bars": 0,
            "feature_computation_failed": 0,
            "hold_signal": 0,
            "low_confidence": 0,
            "error": 0,
            "passed": 0,
        }
        rejection_details: list[dict[str, str]] = []

        if not watchlist:
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"watchlist_size": 0, "signals_generated": 0, "signals": []},
            )

        # Check if ML is available
        if self.ctx.ml is None:
            logger.warning("No ML provider configured, skipping signal generation")
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={
                    "watchlist_size": len(watchlist),
                    "signals_generated": 0,
                    "signals": [],
                    "reason": "no_ml",
                },
            )

        indicator_cfg = IndicatorConfig(
            ema_periods=self.ctx.config.strategy.ema_periods,
            rsi=self.ctx.config.strategy.indicators.rsi,
            macd=self.ctx.config.strategy.indicators.macd,
            bollinger_bands=self.ctx.config.strategy.indicators.bollinger_bands,
            vwap=self.ctx.config.strategy.indicators.vwap,
            atr=self.ctx.config.strategy.indicators.atr,
            volume_profile=self.ctx.config.strategy.indicators.volume_profile,
            obv=self.ctx.config.strategy.indicators.obv,
            supertrend=self.ctx.config.strategy.indicators.supertrend,
            # Daily inference → must match the swing model's training set.
            extended_momentum=self.ctx.config.strategy.indicators.extended_momentum,
        )

        # Build set of currently held symbols (open positions)
        # Used to decide if SELL = exit-owned-stock (CNC ok) vs short-sell (force MIS)
        open_positions = await self.ctx.db.get_open_positions(mode=self.ctx.config.mode)
        held_symbols = {p["symbol"] for p in open_positions}

        # Load locked symbols — SELL signals for these will be skipped entirely
        locked_symbols = await self.ctx.db.get_locked_symbols()

        # Skip symbols that already have a signal or open position today.
        # Risk-rejected symbols are intentionally re-evaluated each
        # heartbeat (capped) — most rejection reasons are transient.
        already_signaled = await self.ctx.db.get_todays_signaled_symbols(
            mode=self.ctx.config.mode,
            risk_rejected_retry_cap=self.ctx.config.risk.max_risk_rejected_retries_per_day,
        )
        if already_signaled:
            filter_counts["already_signaled"] = 0
            logger.info(
                "generate-signals: %d symbols blocked by already_signaled dedup: %s",
                len(already_signaled), sorted(already_signaled),
            )

        # Load symbol cooldown/repeat data
        cooldown_days = self.ctx.config.risk.symbol_cooldown_days
        repeat_lookback = self.ctx.config.risk.symbol_repeat_lookback_days
        repeat_min_conf = self.ctx.config.risk.symbol_repeat_min_confidence
        recently_traded: dict[str, str] = {}
        if repeat_lookback > 0:
            recently_traded = await self.ctx.db.get_recently_traded_symbols(
                repeat_lookback, mode=self.ctx.config.mode,
            )

        now = now_ist()

        # India VIX is a broadcast series — every symbol on this run gets
        # the same trailing-window value. Load once, before the per-symbol
        # loop. Empty result → neutral VIX features at compute time.
        vix_timeline: list[tuple[str, float]] = []
        try:
            vix_timeline = await self.ctx.db.get_vix_timeline(
                date_from=(now - timedelta(days=40)).strftime("%Y-%m-%d"),
            )
        except Exception:
            logger.debug("VIX timeline load failed; defaulting to neutral", exc_info=True)
        _today_str = now.strftime("%Y-%m-%d")
        if vix_timeline:
            _vix_feats_today = compute_vix_features(vix_timeline, _today_str)
        else:
            _vix_feats_today = {k: 0.0 for k in VIX_FEATURE_KEYS}

        # F&O option-chain timeline. Per-symbol lookup; misses → neutral.
        # Only the last 3 days are needed to derive today's oi_change_pct
        # and oi_buildup vs yesterday — keep the read window tight.
        fno_lookup: dict[str, list[tuple[str, dict[str, float]]]] = {}
        try:
            fno_lookup = await self.ctx.db.get_fno_timeline(
                date_from=(now - timedelta(days=5)).strftime("%Y-%m-%d"),
            )
        except Exception:
            logger.debug("F&O timeline load failed; defaulting to neutral", exc_info=True)

        # Pre-fetch market_regime once — it's the same blob for every
        # symbol on this heartbeat, so paying for N DB round-trips
        # inside the loop was pure waste.
        regime_state: Any = None
        if self.ctx.config.strategy.market_regime.enabled:
            try:
                regime_state = await self.ctx.db.get_system_state("market_regime")
            except Exception:
                logger.debug("market_regime fetch failed", exc_info=True)

        # Inference feature-enrichment context (universe/sector regime,
        # feedback) loaded once per heartbeat. The per-symbol enrich call
        # adds the ~19 features the model trains on but compute_features
        # doesn't produce — without this they default to 0.0 (off the
        # training distribution) and the model never reaches its thresholds.
        inference_ctx = await load_inference_feature_context(self.ctx)

        # Capture the symbol evaluation in chunks so DB / LTP / news /
        # ML I/O for ~N symbols overlaps instead of running strictly
        # sequentially. Each task returns a typed result; serial state
        # mutation (filter_counts, insert_signal, broadcast) happens in
        # the main loop after the chunk completes so ordering and
        # signal-id assignment stay deterministic.
        async def _evaluate_one(stock: dict[str, Any]) -> dict[str, Any]:
            """Per-symbol pipeline. Returns a result dict with one of:
              - {"outcome": "skip", "reason": str, "detail": str}
                  → tally only, no signal generated
              - {"outcome": "evaluator", "evaluation": SignalEvaluation,
                  "features": dict, "current_price": float|None,
                  "is_reentry": bool, "shadow_pred": MLPrediction|None}
                  → main loop tallies + inserts + broadcasts
              - {"outcome": "error", "detail": str}
                  → tally as error
            """
            symbol = stock["symbol"]
            return await self._evaluate_symbol_for_signal(
                symbol=symbol,
                already_signaled=already_signaled,
                recently_traded=recently_traded,
                cooldown_days=cooldown_days,
                now=now,
                indicator_cfg=indicator_cfg,
                vix_feats_today=_vix_feats_today,
                fno_lookup=fno_lookup,
                today_str=_today_str,
                held_symbols=held_symbols,
                locked_symbols=locked_symbols,
                open_positions=open_positions,
                regime_state=regime_state,
                inference_ctx=inference_ctx,
            )

        chunk_size = max(
            1, self.ctx.config.strategy.signal_generation_concurrency,
        )

        for chunk_start in range(0, len(watchlist), chunk_size):
            chunk = watchlist[chunk_start: chunk_start + chunk_size]
            # gather with return_exceptions so a single symbol blowing
            # up doesn't take the whole chunk down — the per-symbol
            # function already catches its own exceptions and returns
            # outcome="error", but we keep the safety net here in case
            # something escapes.
            results = await asyncio.gather(
                *(_evaluate_one(s) for s in chunk),
                return_exceptions=True,
            )

            for stock, result in zip(chunk, results):
                symbol = stock["symbol"]
                if isinstance(result, BaseException):
                    filter_counts["error"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "error",
                        "detail": str(result),
                    })
                    logger.warning(
                        "Signal generation failed for %s: %s",
                        symbol, result, exc_info=result,
                    )
                    continue

                outcome = result.get("outcome")
                if outcome == "skip":
                    bucket = result["reason"]
                    filter_counts.setdefault(bucket, 0)
                    filter_counts[bucket] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": bucket,
                        "detail": result.get("detail", ""),
                    })
                    continue
                if outcome == "error":
                    filter_counts["error"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "error",
                        "detail": result.get("detail", ""),
                    })
                    continue

                # outcome == "evaluator" — apply the result through the
                # rest of the pipeline (shadow persistence, gate tally,
                # signal insert, broadcast) serially so DB ordering and
                # the signal_id chain are deterministic.
                await self._apply_evaluation_result(
                    symbol=symbol,
                    result=result,
                    filter_counts=filter_counts,
                    rejection_details=rejection_details,
                    outcome_tracker=outcome_tracker,
                    signals_generated=signals_generated,
                    recently_traded=recently_traded,
                    repeat_lookback=repeat_lookback,
                    repeat_min_conf=repeat_min_conf,
                )

        logger.info(
            "generate-signals: %d signals from %d watchlist stocks — %s",
            len(signals_generated), len(watchlist), filter_counts,
        )

        # Persist rotation outcomes so market-scan can cooldown stale symbols.
        if rotation_cfg.rotation_enabled and outcome_tracker:
            threshold = rotation_cfg.rotation_no_signal_threshold
            cooldown_hours = rotation_cfg.rotation_cooldown_hours
            for sym, produced in outcome_tracker.items():
                try:
                    await self.ctx.db.record_signal_outcome(
                        sym, produced,
                        threshold=threshold, cooldown_hours=cooldown_hours,
                    )
                except Exception:
                    logger.exception("Failed to record signal outcome for %s", sym)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "watchlist_size": len(watchlist),
                "signals_generated": len(signals_generated),
                "signals": signals_generated,  # full signal dicts for downstream skills
                "diagnostics": {
                    "min_confidence_threshold": min_confidence,  # legacy (min of buy/sell)
                    "min_confidence_buy": min_confidence_buy,
                    "min_confidence_sell": min_confidence_sell,
                    "filter_counts": filter_counts,
                    "rejection_details": rejection_details,
                },
            },
        )

    async def _evaluate_symbol_for_signal(
        self,
        symbol: str,
        already_signaled: set[str],
        recently_traded: dict[str, str],
        cooldown_days: int,
        now: datetime,
        indicator_cfg: IndicatorConfig,
        vix_feats_today: dict[str, float],
        fno_lookup: dict[str, list[tuple[str, dict[str, float]]]],
        today_str: str,
        held_symbols: set[str],
        locked_symbols: set[str],
        open_positions: list[dict[str, Any]],
        regime_state: Any,
        inference_ctx: dict[str, Any],
    ) -> dict[str, Any]:
        """All per-symbol async work (DB reads, LTP, feature merge, ML
        predict, shadow predict) with NO shared-state mutation. Returns
        a result dict the chunked-gather driver dispatches on.

        Outcome shapes:
          {"outcome": "skip", "reason": str, "detail": str}
          {"outcome": "evaluator", "evaluation": SignalEvaluation,
           "features": dict, "current_price": float|None,
           "is_reentry": bool, "shadow_pred": MLPrediction|None}
          {"outcome": "error", "detail": str}
        """
        try:
            if symbol in already_signaled:
                return {
                    "outcome": "skip",
                    "reason": "already_signaled",
                    "detail": "signal or open position exists today",
                }

            # Cooldown / smart-reentry resolution. Stays per-symbol async
            # because _check_reentry_conditions does its own DB I/O.
            reentry_cfg = self.ctx.config.risk.reentry
            is_reentry = False
            if symbol in recently_traded and cooldown_days > 0:
                last_trade_str = recently_traded[symbol]
                try:
                    last_trade_dt = datetime.fromisoformat(last_trade_str)
                    if last_trade_dt.tzinfo is None:
                        last_trade_dt = last_trade_dt.replace(tzinfo=IST)
                    days_since = (now - last_trade_dt).days
                    if days_since < cooldown_days:
                        reentry_allowed = False
                        if reentry_cfg.enabled:
                            reentry_allowed = await self._check_reentry_conditions(
                                symbol, reentry_cfg,
                            )
                        if reentry_allowed:
                            is_reentry = True
                            logger.info(
                                "Re-entry allowed for %s: traded %dd ago "
                                "(cooldown=%dd), conditions met",
                                symbol, days_since, cooldown_days,
                            )
                        else:
                            return {
                                "outcome": "skip",
                                "reason": "cooldown",
                                "detail": (
                                    f"traded {days_since}d ago, "
                                    f"cooldown={cooldown_days}d"
                                ),
                            }
                except (ValueError, TypeError):
                    logger.debug(
                        "Cooldown check parse error for %s", symbol,
                        exc_info=True,
                    )

            daily_bars = await self.ctx.db.get_ohlcv(symbol, "daily", days=365)
            # Drop today's DEVELOPING daily bar (present whenever an
            # intra-session ingest ran: close = running LTP, partial
            # volume). The model trained exclusively on completed
            # sessions — partial-bar volume z-scores / ranges / closes
            # are a different distribution, worst in the morning when
            # most signals fire. All features are therefore as-of the
            # last COMPLETED session; entry price stays the live LTP.
            _today = now.date()
            daily_bars = [
                b for b in daily_bars
                if (
                    b.timestamp.astimezone(IST) if b.timestamp.tzinfo
                    else b.timestamp
                ).date() < _today
            ]
            if len(daily_bars) < 50:
                return {
                    "outcome": "skip",
                    "reason": "insufficient_bars",
                    "detail": f"{len(daily_bars)} bars < 50 required",
                }

            latest_bar_date = daily_bars[-1].timestamp.date()
            expected_freshest = self.ctx.market_hours.most_recent_completed_trading_day(now)
            missing = self.ctx.market_hours.trading_days_missing_after(
                latest_bar_date, expected_freshest,
            )
            max_age = self.ctx.config.market_data.max_signal_data_age_trading_days
            if missing > max_age:
                return {
                    "outcome": "skip",
                    "reason": "stale_data",
                    "detail": (
                        f"latest bar {latest_bar_date} is {missing} trading "
                        f"days behind {expected_freshest} (max {max_age})"
                    ),
                }

            features = compute_features(daily_bars, indicator_cfg)
            if not features:
                return {
                    "outcome": "skip",
                    "reason": "feature_computation_failed",
                    "detail": "compute_features returned empty",
                }

            # Higher-timeframe (daily) trend context for the intraday model.
            # Closes strictly before the decision day mirror the prior-session
            # window the intraday training labels see — same train/serve keys,
            # no intraday-day lookahead. Survives the {**features, **tech}
            # merge in signal_evaluator (compute_features emits no daily_* key).
            _decision_day = now.date()
            _prior_closes = [
                float(b.close)
                for b in daily_bars
                if b.close is not None and b.timestamp.date() < _decision_day
            ]
            features.update(compute_daily_trend_features(_prior_closes))

            try:
                news_from = (now - timedelta(days=7)).isoformat()
                news_rows = await self.ctx.db.get_news_articles(
                    symbol=symbol, date_from=news_from, limit=500,
                )
                headlines: list[tuple[str, datetime]] = []
                for row in news_rows:
                    pub_raw = row.get("published_at")
                    if not pub_raw:
                        continue
                    try:
                        dt = datetime.fromisoformat(pub_raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=IST)
                    except (ValueError, TypeError):
                        continue
                    headlines.append((row.get("headline", ""), dt))
                features.update(compute_news_features(headlines, now))
            except Exception:
                logger.debug("News-feature merge failed for %s", symbol, exc_info=True)
                features.update({k: 0.0 for k in NEWS_FEATURE_KEYS})

            features.update(vix_feats_today)

            _sym_fno = fno_lookup.get(symbol)
            if _sym_fno:
                _prior_close = (
                    daily_bars[-2].close if len(daily_bars) >= 2 else None
                )
                _current_close = daily_bars[-1].close
                features.update(compute_fno_features(
                    _sym_fno, today_str,
                    prior_stock_close=_prior_close,
                    current_stock_close=_current_close,
                ))
            else:
                features.update({k: 0.0 for k in FNO_FEATURE_KEYS})

            # Regime / sector / institutional / feedback features — the
            # set the model trains on but compute_features doesn't produce.
            await enrich_features(self.ctx, symbol, features, inference_ctx)

            # Persist the UNCONDITIONED feature vector for drift-watch's
            # PSI check (pre-gate: every evaluated symbol, not just
            # passed signals — signals.features_snapshot is a gate-
            # conditioned subset and would alarm spuriously). One row
            # per (day, symbol, mode); later heartbeats overwrite.
            try:
                await self.ctx.db.upsert_feature_snapshot(
                    now.strftime("%Y-%m-%d"), symbol,
                    self.ctx.config.mode, features,
                )
            except Exception:
                logger.debug(
                    "feature snapshot persist failed for %s",
                    symbol, exc_info=True,
                )

            current_price: float | None = None
            try:
                current_price = await self.ctx.market_data.get_ltp(symbol)
            except Exception:
                logger.debug("LTP unavailable for %s, falling back to bar close", symbol)

            evaluation = await evaluate_symbol_signal(
                self.ctx,
                symbol,
                features,
                current_price=current_price,
                held_symbols=held_symbols,
                locked_symbols=locked_symbols,
                now_time=now.time(),
                existing_positions=open_positions,
                market_regime=regime_state,
            )

            shadow_pred = None
            if evaluation.prediction is not None:
                model_type = (
                    "intraday" if evaluation.holding_period == "intraday" else "swing"
                )
                if self.ctx.ml.has_shadow(model_type):
                    try:
                        if model_type == "intraday":
                            shadow_pred = await self.ctx.ml.predict_shadow_intraday(
                                symbol, features, current_price=current_price,
                            )
                        else:
                            shadow_pred = await self.ctx.ml.predict_shadow_swing(
                                symbol, features, current_price=current_price,
                            )
                    except Exception as e:
                        logger.debug("Shadow inference failed for %s: %s", symbol, e)

            return {
                "outcome": "evaluator",
                "evaluation": evaluation,
                "features": features,
                "current_price": current_price,
                "is_reentry": is_reentry,
                "shadow_pred": shadow_pred,
            }
        except Exception as e:
            logger.warning(
                "Signal evaluation failed for %s: %s", symbol, e, exc_info=True,
            )
            return {"outcome": "error", "detail": str(e)}

    async def _apply_evaluation_result(
        self,
        symbol: str,
        result: dict[str, Any],
        filter_counts: dict[str, int],
        rejection_details: list[dict[str, Any]],
        outcome_tracker: dict[str, bool],
        signals_generated: list[dict[str, Any]],
        recently_traded: dict[str, str],
        repeat_lookback: int,
        repeat_min_conf: float,
    ) -> None:
        """Serial post-processing of one chunk-task result. Owns all
        the shared-state mutations (counters, signal_id allocation,
        shadow-prediction insert, broadcast) so the parallel evaluator
        tasks above can stay pure.
        """
        evaluation = result["evaluation"]
        features = result["features"]
        is_reentry = result["is_reentry"]
        shadow_pred = result.get("shadow_pred")
        prediction = evaluation.prediction
        holding_period = evaluation.holding_period
        product = evaluation.product
        expected_days = evaluation.expected_days

        if shadow_pred is not None:
            try:
                await self.ctx.db.insert_shadow_prediction({
                    "symbol": symbol,
                    "predicted_direction": shadow_pred.signal_type,
                    "confidence": shadow_pred.confidence,
                    "predicted_target": shadow_pred.target_price,
                    "predicted_stop_loss": shadow_pred.stop_loss_price,
                    "expected_holding_period": shadow_pred.holding_period,
                    "model_version": shadow_pred.model_version,
                    "entry_price": shadow_pred.entry_price,
                    "mode": self.ctx.config.mode,
                })
            except Exception:
                logger.debug(
                    "Shadow prediction insert failed for %s", symbol,
                    exc_info=True,
                )

        if evaluation.outcome != "passed":
            bucket = evaluation.outcome
            filter_counts.setdefault(bucket, 0)
            filter_counts[bucket] += 1
            rejection_details.append({
                "symbol": symbol, "reason": bucket,
                "detail": evaluation.detail,
            })
            if bucket in (
                "hold_signal", "low_confidence", "sell_on_holding",
                "short_on_swing_horizon", "intraday_atr_ineligible",
                "implausible_atr",
            ):
                outcome_tracker[symbol] = False
            return

        assert prediction is not None
        signal = {
            "symbol": symbol,
            "signal_type": evaluation.signal_type,
            "entry_price": evaluation.entry_price,
            "target_price": evaluation.target_price,
            "stop_loss_price": evaluation.stop_loss_price,
            "position_size": prediction.position_size,
            "expected_holding_period": holding_period,
            "expected_holding_days": expected_days,
            "product": product,
            "confidence_score": evaluation.confidence,
            "features_snapshot": features,
            "model_version": evaluation.model_version,
            "attribution": (
                [
                    {
                        "feature": a.feature,
                        "value": a.value,
                        "contribution": a.contribution,
                    }
                    for a in prediction.attribution
                ]
                if prediction.attribution else None
            ),
        }
        if is_reentry:
            signal["reentry"] = True

        base_threshold = evaluation.effective_min_confidence or 0.0
        effective_min = base_threshold
        is_repeat = symbol in recently_traded
        if is_repeat and repeat_lookback > 0:
            effective_min = max(base_threshold, repeat_min_conf)

        if signal["confidence_score"] < effective_min:
            filter_counts.setdefault("repeat_low_confidence", 0)
            filter_counts["repeat_low_confidence"] += 1
            outcome_tracker[symbol] = False
            rejection_details.append({
                "symbol": symbol, "reason": "repeat_low_confidence",
                "detail": (
                    f"{prediction.signal_type} @ {prediction.confidence:.2f} "
                    f"< {effective_min} (repeat threshold, base={base_threshold})"
                ),
            })
            logger.info(
                "Repeat confidence filter for %s: %s @ %.2f < %.2f "
                "(repeat, base=%.2f)",
                symbol, prediction.signal_type, prediction.confidence,
                effective_min, base_threshold,
            )
            return

        reentry_cfg = self.ctx.config.risk.reentry
        if is_reentry and reentry_cfg.require_higher_confidence:
            orig_conf = await self._get_last_trade_confidence(symbol)
            tol = reentry_cfg.confidence_tolerance
            floor = reentry_cfg.min_reentry_confidence
            relative_threshold = (
                (orig_conf * tol) if orig_conf is not None else 0.0
            )
            effective_threshold = max(floor, relative_threshold)
            if prediction.confidence < effective_threshold:
                filter_counts.setdefault("reentry_low_confidence", 0)
                filter_counts["reentry_low_confidence"] += 1
                rejection_details.append({
                    "symbol": symbol, "reason": "reentry_low_confidence",
                    "detail": (
                        f"re-entry {prediction.signal_type} @ "
                        f"{prediction.confidence:.2f} < threshold "
                        f"{effective_threshold:.2f} "
                        f"(orig {orig_conf if orig_conf is not None else 'n/a'}, "
                        f"tol {tol:.2f}, floor {floor:.2f})"
                    ),
                })
                logger.info(
                    "Re-entry blocked for %s: confidence %.2f < "
                    "effective threshold %.2f (orig=%s tol=%.2f floor=%.2f)",
                    symbol, prediction.confidence,
                    effective_threshold,
                    f"{orig_conf:.2f}" if orig_conf is not None else "n/a",
                    tol, floor,
                )
                return

        filter_counts["passed"] += 1
        outcome_tracker[symbol] = True
        signal.setdefault("mode", self.ctx.config.mode)
        signal_id = await self.ctx.db.insert_signal(signal)
        if signal_id:
            signal["signal_id"] = signal_id
        signals_generated.append(signal)
        logger.info(
            "PASSED %s for %s @ %.2f (%s)",
            prediction.signal_type, symbol, prediction.confidence,
            _format_class_probs(prediction),
        )
        ticker = getattr(self.ctx, "ticker", None)
        if ticker is not None:
            try:
                await ticker.subscribe([symbol])
            except Exception:
                logger.debug(
                    "ticker subscribe failed for %s", symbol,
                    exc_info=True,
                )
        await self.broadcast("signal_generated", {
            "symbol": symbol,
            "signal_type": prediction.signal_type,
            "confidence": prediction.confidence,
            "entry_price": prediction.entry_price,
        })

    async def _check_reentry_conditions(
        self, symbol: str, reentry_cfg: Any,
    ) -> bool:
        """Check whether smart re-entry conditions are met for a symbol in cooldown.

        Evaluates:
        1. min_bars_after_exit: enough bars have passed since the last trade closed
        2. min_price_move_pct: price has moved sufficiently from the exit price
        3. max_reentries_per_symbol: haven't exceeded re-entry limit for today

        Returns True if all conditions are met and re-entry should be allowed.
        """
        try:
            # Get last closed trade for this symbol
            trades = await self.ctx.db.get_symbol_trades(symbol, limit=5)
            closed_trades = [
                t for t in trades
                if t.get("closed_at") is not None and t.get("status") in ("closed", "filled", "squared_off")
            ]
            if not closed_trades:
                return False

            last_trade = closed_trades[0]  # most recent closed trade

            # Condition 1: min_bars_after_exit
            exit_date_str = last_trade.get("closed_at")
            if not exit_date_str:
                return False

            exit_dt = datetime.fromisoformat(exit_date_str)
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=IST)

            # Count bars since exit using OHLCV data
            bars = await self.ctx.db.get_ohlcv(symbol, "daily", days=reentry_cfg.min_bars_after_exit + 5)
            # Different providers (jugaad / yfinance / tvdatafeed / kite)
            # store OHLCV timestamps with mixed tz state — some naive,
            # some aware. exit_dt is always-aware now, so normalize each
            # bar before the > comparison to avoid TypeError.
            bars_after_exit = sum(
                1 for bar in bars
                if (
                    bar.timestamp.replace(tzinfo=IST)
                    if bar.timestamp.tzinfo is None
                    else bar.timestamp
                ) > exit_dt
            )
            if bars_after_exit < reentry_cfg.min_bars_after_exit:
                logger.debug(
                    "Re-entry blocked for %s: only %d bars after exit (need %d)",
                    symbol, bars_after_exit, reentry_cfg.min_bars_after_exit,
                )
                return False

            # Condition 2: min_price_move_pct
            exit_price = last_trade.get("fill_price") or last_trade.get("entry_price")
            if not exit_price or exit_price <= 0:
                return False

            try:
                current_price = await self.ctx.market_data.get_ltp(symbol)
            except Exception:
                logger.debug("LTP unavailable for %s re-entry check, using bar close", symbol)
                # Fall back to last bar close
                if bars:
                    current_price = bars[-1].close
                else:
                    return False

            price_move_pct = abs(current_price - exit_price) / exit_price
            if price_move_pct < reentry_cfg.min_price_move_pct:
                logger.debug(
                    "Re-entry blocked for %s: price move %.2f%% < %.2f%% required",
                    symbol, price_move_pct * 100, reentry_cfg.min_price_move_pct * 100,
                )
                return False

            # Condition 3: max_reentries_per_symbol
            todays_trades = await self.ctx.db.get_todays_trades()
            symbol_trades_today = sum(
                1 for t in todays_trades if t.get("symbol") == symbol
            )
            if symbol_trades_today >= reentry_cfg.max_reentries_per_symbol:
                logger.debug(
                    "Re-entry blocked for %s: %d trades today >= max %d",
                    symbol, symbol_trades_today, reentry_cfg.max_reentries_per_symbol,
                )
                return False

            # Note: require_higher_confidence is checked downstream during
            # the confidence filter, since we don't have the new signal's
            # confidence yet at this point. We store the original confidence
            # for comparison later.

            logger.info(
                "Re-entry conditions met for %s: bars_after_exit=%d, price_move=%.2f%%, "
                "today_trades=%d",
                symbol, bars_after_exit, price_move_pct * 100, symbol_trades_today,
            )
            return True

        except Exception as e:
            logger.warning("Re-entry condition check failed for %s: %s", symbol, e)
            return False

    async def _get_last_trade_confidence(self, symbol: str) -> float | None:
        """Get the confidence score of the last closed trade for a symbol.

        Used by the smart re-entry feature to enforce require_higher_confidence.
        Returns None if no trade is found or confidence is unavailable.
        """
        try:
            trades = await self.ctx.db.get_symbol_trades(symbol, limit=5)
            for t in trades:
                if t.get("closed_at") is not None:
                    conf = t.get("confidence_score")
                    if conf is not None:
                        return float(conf)
            return None
        except Exception:
            logger.debug("Failed to get last trade confidence for %s", symbol, exc_info=True)
            return None
