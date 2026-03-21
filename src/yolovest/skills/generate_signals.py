"""Skill: generate-signals — ML-based trade signal generation.

Covers: FR-4.1 to FR-4.5
Trigger: HEARTBEAT during market hours
Pipeline position: After market-scan, before risk-check.

Flow:
1. Load current watchlist from DB
2. For each watchlist stock, compute features (FR-4.1):
   - RSI, MACD, Bollinger Bands, VWAP, ATR, OBV, SuperTrend
   - EMAs at configurable periods (strategy.ema_periods)
   - Volume profile, delivery %
   - Only enabled indicators (strategy.indicators toggle map)
3. Run appropriate ML model (FR-4.2, FR-4.3):
   - Intraday model: uses 1-5min candle features
   - Swing model: uses daily candle features, multi-day patterns
4. Generate signal with required fields (FR-4.5):
   - entry_price, target_price, stop_loss_price
   - position_size, expected_holding_period, confidence_score
   - signal_type: BUY / SELL / HOLD
5. Filter: only emit signals where confidence >= risk.min_confidence_score
6. Emit signals as events for risk-check skill to consume
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class GenerateSignalsSkill(SkillBase):
    name = "generate-signals"
    description = "Run ML models on watchlist to produce trade signals"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return self.ctx.market_hours.is_market_hours() and self.ctx.broker.is_authenticated()

    async def execute(self, **kwargs: Any) -> SkillResult:
        watchlist = await self.ctx.db.get_watchlist()
        signals_generated = []
        min_confidence = self.ctx.config.risk.min_confidence_score

        for stock in watchlist:
            symbol = stock["symbol"]

            # Step 2: Feature engineering
            features = await self.ctx.features.compute(
                symbol=symbol,
                ema_periods=self.ctx.config.strategy.ema_periods,
                indicators=self.ctx.config.strategy.indicators,
            )

            # Step 3: Run ML model (intraday or swing based on time/config)
            if self._should_use_intraday_model():
                prediction = await self.ctx.ml.predict_intraday(symbol, features)
            else:
                prediction = await self.ctx.ml.predict_swing(symbol, features)

            # Step 4: Build signal
            if prediction.signal_type == "HOLD":
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

            # Step 5: Confidence filter
            if signal["confidence_score"] >= min_confidence:
                await self.ctx.db.insert_signal(signal)
                signals_generated.append(signal)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "watchlist_size": len(watchlist),
                "signals_generated": len(signals_generated),
                "signals": [
                    {
                        "symbol": s["symbol"],
                        "type": s["signal_type"],
                        "confidence": s["confidence_score"],
                    }
                    for s in signals_generated
                ],
            },
        )

    def _should_use_intraday_model(self) -> bool:
        """Decide model type based on time of day and config."""
        raise NotImplementedError
