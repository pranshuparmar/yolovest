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

        for stock in watchlist:
            symbol = stock["symbol"]

            try:
                # Step 2: Fetch OHLCV and compute features
                interval = "5minute" if use_intraday else "daily"
                days = 1 if use_intraday else 60
                bars = await self.ctx.db.get_ohlcv(symbol, interval, days=days)

                if not bars:
                    # Fallback to daily if intraday not available
                    bars = await self.ctx.db.get_ohlcv(symbol, "daily", days=60)

                if len(bars) < 15:
                    logger.debug("Insufficient data for %s (%d bars)", symbol, len(bars))
                    continue

                features = compute_features(bars, indicator_cfg)
                if not features:
                    continue

                # Step 3: Run ML model
                if use_intraday:
                    prediction = await self.ctx.ml.predict_intraday(symbol, features)
                else:
                    prediction = await self.ctx.ml.predict_swing(symbol, features)

                # Step 4: Skip HOLD signals
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

                # Step 5: Confidence filter (FR-4.8)
                if signal["confidence_score"] >= min_confidence:
                    await self.ctx.db.insert_signal(signal)
                    signals_generated.append(signal)

            except Exception as e:
                logger.warning("Signal generation failed for %s: %s", symbol, e)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "watchlist_size": len(watchlist),
                "signals_generated": len(signals_generated),
                "signals": signals_generated,  # full signal dicts for downstream skills
            },
        )

    def _should_use_intraday_model(self) -> bool:
        """Decide model type based on time of day and config (FR-4.3, H7)."""
        if self.ctx.config.strategy.default_trade_type == "swing":
            return False
        # Before 14:00 IST → intraday (MIS needs time to play out before 15:15 square-off)
        now = datetime.now(IST).time()
        return now < time(14, 0)
