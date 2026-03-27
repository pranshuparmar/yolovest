"""Skill: generate-signals — ML-based trade signal generation.

Covers: FR-4.1 to FR-4.5
Trigger: HEARTBEAT during market hours
Pipeline position: After market-scan, before risk-check.

Flow:
1. Load current watchlist from DB
2. For each watchlist stock, compute features (FR-4.1)
3. Run appropriate ML model (FR-4.2, FR-4.3)
4. Generate signal with required fields (FR-4.5)
5. Filter: only emit signals where confidence >= risk.min_confidence_score
6. Emit signals as events for risk-check skill to consume
"""

import logging
from datetime import datetime, time
from typing import Any
from yolovest.data.features import IndicatorConfig, compute_features
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST

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

        use_intraday = self._should_use_intraday_model()
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

        # Skip symbols that already have a signal or open position today
        already_signaled = await self.ctx.db.get_todays_signaled_symbols()
        if already_signaled:
            filter_counts["already_signaled"] = 0

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

                # Step 4: Skip HOLD signals
                if prediction.signal_type == "HOLD":
                    filter_counts["hold_signal"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "hold_signal",
                        "detail": f"HOLD @ confidence {prediction.confidence:.2f}",
                    })
                    logger.info("HOLD signal for %s (confidence %.2f)", symbol, prediction.confidence)
                    continue

                signal = {
                    "symbol": symbol,
                    "signal_type": prediction.signal_type,
                    "entry_price": prediction.entry_price,
                    "target_price": prediction.target_price,
                    "stop_loss_price": prediction.stop_loss_price,
                    "position_size": prediction.position_size,
                    "expected_holding_period": prediction.holding_period,
                    "confidence_score": prediction.confidence,
                    "features_snapshot": features,
                    "model_version": prediction.model_version,
                }

                # Step 5: Confidence filter (FR-4.8)
                if signal["confidence_score"] >= min_confidence:
                    filter_counts["passed"] += 1
                    await self.ctx.db.insert_signal(signal)
                    signals_generated.append(signal)
                    await self.broadcast("signal_generated", {
                        "symbol": symbol,
                        "signal_type": prediction.signal_type,
                        "confidence": prediction.confidence,
                        "entry_price": prediction.entry_price,
                    })
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

    def _should_use_intraday_model(self) -> bool:
        """Decide model type based on time of day and config (FR-4.3, H7)."""
        if self.ctx.config.strategy.default_trade_type == "swing":
            return False
        # Before 14:00 IST → intraday (MIS needs time to play out before 15:15 square-off)
        now = datetime.now(IST).time()
        return now < time(14, 0)
