"""Skill: generate-signals — ML-based trade signal generation.

Trigger: HEARTBEAT during market hours
Pipeline position: After market-scan, before risk-check.

Flow:
1. Load current watchlist from DB
2. For each watchlist stock, compute features
3. Run appropriate ML model
4. Generate signal with required fields
5. Filter: only emit signals where confidence >= risk.min_confidence_score
6. Emit signals as events for risk-check skill to consume
"""

import logging
from datetime import datetime, time, timedelta
from typing import Any
from yolovest.data.features import IndicatorConfig, compute_features
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)


class GenerateSignalsSkill(SkillBase):
    name = "generate-signals"
    description = "Run ML models on watchlist to produce trade signals"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_market_hours())

    async def execute(self, **kwargs: Any) -> SkillResult:
        watchlist = await self.ctx.db.get_combined_watchlist()
        signals_generated = []
        min_confidence = self.ctx.config.risk.min_confidence_score

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

        strategy_cfg = self.ctx.config.strategy
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
        )

        # Build set of currently held symbols (open positions)
        # Used to decide if SELL = exit-owned-stock (CNC ok) vs short-sell (force MIS)
        open_positions = await self.ctx.db.get_open_positions()
        held_symbols = {p["symbol"] for p in open_positions}

        # Load locked symbols — SELL signals for these will be skipped entirely
        locked_symbols = await self.ctx.db.get_locked_symbols()

        # Skip symbols that already have a signal or open position today
        already_signaled = await self.ctx.db.get_todays_signaled_symbols()
        if already_signaled:
            filter_counts["already_signaled"] = 0

        # Load symbol cooldown/repeat data
        cooldown_days = self.ctx.config.risk.symbol_cooldown_days
        repeat_lookback = self.ctx.config.risk.symbol_repeat_lookback_days
        repeat_min_conf = self.ctx.config.risk.symbol_repeat_min_confidence
        recently_traded: dict[str, str] = {}
        if repeat_lookback > 0:
            recently_traded = await self.ctx.db.get_recently_traded_symbols(repeat_lookback)

        now = now_ist()

        for stock in watchlist:
            symbol = stock["symbol"]

            if symbol in already_signaled:
                filter_counts.setdefault("already_signaled", 0)
                filter_counts["already_signaled"] += 1
                rejection_details.append({
                    "symbol": symbol, "reason": "already_signaled",
                    "detail": "signal or open position exists today",
                })
                continue

            # Symbol cooldown: hard block if traded within cooldown_days
            # With smart re-entry enabled, allow re-entry under specific conditions
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
                        # Check if smart re-entry can override the cooldown
                        reentry_allowed = False
                        if reentry_cfg.enabled:
                            reentry_allowed = await self._check_reentry_conditions(
                                symbol, reentry_cfg,
                            )
                        if reentry_allowed:
                            is_reentry = True
                            logger.info(
                                "Re-entry allowed for %s: traded %dd ago (cooldown=%dd), conditions met",
                                symbol, days_since, cooldown_days,
                            )
                        else:
                            filter_counts.setdefault("cooldown", 0)
                            filter_counts["cooldown"] += 1
                            rejection_details.append({
                                "symbol": symbol, "reason": "cooldown",
                                "detail": f"traded {days_since}d ago, cooldown={cooldown_days}d",
                            })
                            logger.info(
                                "Cooldown for %s: traded %dd ago (cooldown=%dd)",
                                symbol, days_since, cooldown_days,
                            )
                            continue
                except (ValueError, TypeError):
                    pass

            try:
                # Step 2: Fetch OHLCV and compute features
                # Always use daily bars for feature computation — intraday bars
                # are too few for long-window indicators (EMA-50/200, MACD etc.)
                # which causes feature shape mismatches with the trained model.
                daily_bars = await self.ctx.db.get_ohlcv(symbol, "daily", days=365)

                if len(daily_bars) < 50:
                    filter_counts["insufficient_bars"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "insufficient_bars",
                        "detail": f"{len(daily_bars)} bars < 50 required",
                    })
                    logger.info("Insufficient daily data for %s (%d bars)", symbol, len(daily_bars))
                    continue

                features = compute_features(daily_bars, indicator_cfg)
                if not features:
                    filter_counts["feature_computation_failed"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "feature_computation_failed",
                        "detail": "compute_features returned empty",
                    })
                    logger.info("Feature computation failed for %s", symbol)
                    continue

                # Fetch fresh LTP for accurate entry/target/SL pricing
                current_price: float | None = None
                try:
                    current_price = await self.ctx.market_data.get_ltp(symbol)
                except Exception:
                    pass  # fall back to features["close"] in _predict()

                # Decide holding period based on stock characteristics and strategy mode
                holding_period, product, expected_days = await self._decide_holding_period(
                    features, existing_positions=open_positions,
                )
                use_intraday = holding_period == "intraday"

                # Use latest intraday price for feature close during market hours
                if use_intraday:
                    intraday_bars = await self.ctx.db.get_ohlcv(symbol, "5minute", days=1)
                    if intraday_bars:
                        features["close"] = intraday_bars[-1].close

                # Step 3: Run ML model (features for classification, current_price for entry/target/SL)
                if use_intraday:
                    prediction = await self.ctx.ml.predict_intraday(
                        symbol, features, current_price=current_price,
                    )
                else:
                    prediction = await self.ctx.ml.predict_swing(
                        symbol, features, current_price=current_price,
                    )

                # Shadow inference (non-blocking, best-effort)
                model_type = "intraday" if use_intraday else "swing"
                if self.ctx.ml.has_shadow(model_type):
                    try:
                        if use_intraday:
                            shadow_pred = await self.ctx.ml.predict_shadow_intraday(
                                symbol, features, current_price=current_price,
                            )
                        else:
                            shadow_pred = await self.ctx.ml.predict_shadow_swing(
                                symbol, features, current_price=current_price,
                            )
                        if shadow_pred is not None:
                            await self.ctx.db.insert_shadow_prediction({
                                "symbol": symbol,
                                "predicted_direction": shadow_pred.signal_type,
                                "confidence": shadow_pred.confidence,
                                "predicted_target": shadow_pred.target_price,
                                "predicted_stop_loss": shadow_pred.stop_loss_price,
                                "expected_holding_period": shadow_pred.holding_period,
                                "model_version": shadow_pred.model_version,
                                "entry_price": shadow_pred.entry_price,
                            })
                    except Exception as e:
                        logger.debug("Shadow inference failed for %s: %s", symbol, e)

                # Step 4: Skip HOLD signals
                if prediction.signal_type == "HOLD":
                    filter_counts["hold_signal"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "hold_signal",
                        "detail": f"HOLD @ confidence {prediction.confidence:.2f}",
                    })
                    logger.info("HOLD signal for %s (confidence %.2f)", symbol, prediction.confidence)
                    continue

                # Skip SELL signals for locked holdings (user explicitly protected them)
                if prediction.signal_type == "SELL" and symbol in locked_symbols:
                    filter_counts.setdefault("locked_holding", 0)
                    filter_counts["locked_holding"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "locked_holding",
                        "detail": f"SELL blocked — {symbol} is locked",
                    })
                    logger.info("Locked holding: skipping SELL for %s", symbol)
                    continue

                # Adjust SELL signals: force to MIS/intraday if user doesn't hold the stock
                from yolovest.strategy.holding_period import adjust_sell_for_holdings

                holding_period, product, expected_days = adjust_sell_for_holdings(
                    prediction.signal_type, holding_period, product,
                    symbol, held_symbols, expected_days,
                )

                # Override target/SL with ATR multipliers interpolated for holding duration
                from yolovest.strategy.holding_period import interpolate_atr_multipliers

                entry = prediction.entry_price
                atr = features.get("atr_14", entry * 0.02)
                target_mult, sl_mult = interpolate_atr_multipliers(
                    expected_days, self.ctx.config.strategy.holding_periods,
                )

                if prediction.signal_type == "BUY":
                    target_price = entry + target_mult * atr
                    stop_loss_price = entry - sl_mult * atr
                elif prediction.signal_type == "SELL":
                    target_price = entry - target_mult * atr
                    stop_loss_price = entry + sl_mult * atr
                else:
                    target_price = prediction.target_price
                    stop_loss_price = prediction.stop_loss_price

                target_price = max(target_price, 0.01)
                stop_loss_price = max(stop_loss_price, 0.01)

                signal = {
                    "symbol": symbol,
                    "signal_type": prediction.signal_type,
                    "entry_price": entry,
                    "target_price": round(target_price, 2),
                    "stop_loss_price": round(stop_loss_price, 2),
                    "position_size": prediction.position_size,
                    "expected_holding_period": holding_period,
                    "expected_holding_days": expected_days,
                    "product": product,
                    "confidence_score": prediction.confidence,
                    "features_snapshot": features,
                    "model_version": prediction.model_version,
                }

                if is_reentry:
                    signal["reentry"] = True

                # Step 5: Confidence filter
                # Use elevated threshold for recently traded symbols
                effective_min = min_confidence
                is_repeat = symbol in recently_traded
                if is_repeat and repeat_lookback > 0:
                    effective_min = max(min_confidence, repeat_min_conf)

                if signal["confidence_score"] >= effective_min:
                    # Re-entry: require higher confidence than original trade
                    if is_reentry and reentry_cfg.require_higher_confidence:
                        orig_conf = await self._get_last_trade_confidence(symbol)
                        if orig_conf is not None and prediction.confidence <= orig_conf:
                            filter_counts.setdefault("reentry_low_confidence", 0)
                            filter_counts["reentry_low_confidence"] += 1
                            rejection_details.append({
                                "symbol": symbol, "reason": "reentry_low_confidence",
                                "detail": (
                                    f"re-entry {prediction.signal_type} @ {prediction.confidence:.2f} "
                                    f"<= original {orig_conf:.2f}"
                                ),
                            })
                            logger.info(
                                "Re-entry blocked for %s: confidence %.2f <= original %.2f",
                                symbol, prediction.confidence, orig_conf,
                            )
                            continue

                    filter_counts["passed"] += 1
                    await self.ctx.db.insert_signal(signal)
                    signals_generated.append(signal)
                    await self.broadcast("signal_generated", {
                        "symbol": symbol,
                        "signal_type": prediction.signal_type,
                        "confidence": prediction.confidence,
                        "entry_price": prediction.entry_price,
                    })
                elif is_repeat and signal["confidence_score"] >= min_confidence:
                    # Would have passed normal threshold but blocked by repeat rule
                    filter_counts.setdefault("repeat_low_confidence", 0)
                    filter_counts["repeat_low_confidence"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "repeat_low_confidence",
                        "detail": (
                            f"{prediction.signal_type} @ {prediction.confidence:.2f} "
                            f"< {effective_min} (repeat threshold, normal={min_confidence})"
                        ),
                    })
                    logger.info(
                        "Repeat confidence filter for %s: %s @ %.2f < %.2f (repeat, normal=%.2f)",
                        symbol, prediction.signal_type, prediction.confidence,
                        effective_min, min_confidence,
                    )
                else:
                    filter_counts["low_confidence"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "low_confidence",
                        "detail": f"{prediction.signal_type} @ confidence {prediction.confidence:.2f} < {min_confidence}",
                    })
                    logger.info(
                        "Low confidence for %s: %s @ %.2f < %.2f",
                        symbol, prediction.signal_type, prediction.confidence, min_confidence,
                    )

            except Exception as e:
                filter_counts["error"] += 1
                rejection_details.append({
                    "symbol": symbol, "reason": "error", "detail": str(e),
                })
                logger.warning("Signal generation failed for %s: %s", symbol, e)

        logger.info(
            "generate-signals: %d signals from %d watchlist stocks — %s",
            len(signals_generated), len(watchlist), filter_counts,
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "watchlist_size": len(watchlist),
                "signals_generated": len(signals_generated),
                "signals": signals_generated,  # full signal dicts for downstream skills
                "diagnostics": {
                    "min_confidence_threshold": min_confidence,
                    "filter_counts": filter_counts,
                    "rejection_details": rejection_details,
                },
            },
        )

    async def _decide_holding_period(
        self, features: dict, existing_positions: list[dict] | None = None,
    ) -> tuple[str, str, int]:
        """Decide holding period, product type, and expected days based on stock characteristics."""
        from yolovest.config import _MODE_HOLDING_DAYS
        from yolovest.strategy.holding_period import decide_holding_period

        allowed = self.ctx.config.strategy.allowed_holding_periods or ["intraday", "short_term", "long_term"]
        mode = self.ctx.config.strategy.mode
        mode_days = _MODE_HOLDING_DAYS.get(mode)
        now_time = datetime.now(IST).time()
        vol_cfg = self.ctx.config.strategy.volatility

        # Read market regime (persisted by market-scan skill)
        regime_cfg = self.ctx.config.strategy.market_regime
        regime = None
        bear_max = None
        if regime_cfg.enabled:
            regime = await self.ctx.db.get_system_state("market_regime")
            bear_max = regime_cfg.bear_max_holding_days

        return decide_holding_period(
            features, allowed, vol_cfg, now_time,
            mode_days_range=mode_days,
            existing_positions=existing_positions,
            market_regime=regime,
            bear_max_holding_days=bear_max,
        )

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
            bars_after_exit = sum(1 for bar in bars if bar.timestamp > exit_dt)
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
            return None
