"""Skill: predict-track — Log predictions and score outcomes.

Covers: FR-7.1, FR-7.2, FR-7.3
Trigger: EVENT (post-trade) + HEARTBEAT (check elapsed predictions)
Pipeline position: Runs after trade-execute (to log) and on heartbeat (to score).

Flow:
Phase A — Logging (EVENT trigger, post-trade):
1. Log every prediction: symbol, predicted direction, confidence,
   predicted target, predicted timeframe, model version
2. Store with trade_id linkage for full traceability

Phase B — Scoring (HEARTBEAT trigger):
1. Query predictions whose timeframe has elapsed
2. For each: fetch actual price at prediction end time
3. Compute: was direction correct? did it hit target? actual PnL?
4. Update prediction record with actual outcome
5. Maintain scoreboard: accuracy by symbol, strategy, market condition, timeframe
6. Feed results back to model-retrain for continuous improvement
"""

import logging
from typing import Any
from zoneinfo import ZoneInfo

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class PredictTrackSkill(SkillBase):
    name = "predict-track"
    description = "Log predictions and score actual outcomes"
    trigger = SkillTrigger.HEARTBEAT  # also triggered by events
    schedule = None

    def should_run(self) -> bool:
        return True  # always available — scoring can happen anytime

    async def execute(self, **kwargs: Any) -> SkillResult:
        mode = kwargs.get("mode", "score")  # "log" or "score"

        if mode == "log":
            return await self._log_prediction(kwargs["signal"], kwargs.get("trade_id"))
        else:
            return await self._score_elapsed_predictions()

    async def _run_failure_analysis(self) -> bool:
        """FR-7.6: Use Gemini to analyze recent prediction failures."""
        try:
            outcomes = await self.ctx.db.get_prediction_outcomes()
            failures = [p for p in outcomes if not p.get("direction_correct")]
            if len(failures) < 3:
                return False

            # Only analyze recent failures (last 20)
            recent_failures = failures[-20:]
            analysis = await self.ctx.llm.analyze_prediction_failures(recent_failures)
            await self.ctx.db.store_failure_analysis(analysis)
            logger.info(
                "Failure analysis completed: %d failures analyzed", len(recent_failures)
            )
            return True
        except Exception as e:
            logger.warning("Failure analysis failed: %s", e)
            return False

    async def _log_prediction(self, signal: dict[str, Any], trade_id: str | None) -> SkillResult:
        """FR-7.1: Log a new prediction."""
        prediction = {
            "symbol": signal["symbol"],
            "predicted_direction": signal["signal_type"],
            "confidence": signal.get("confidence_score", signal.get("confidence", 0)),
            "predicted_target": signal["target_price"],
            "predicted_stop_loss": signal["stop_loss_price"],
            "expected_holding_period": signal.get("expected_holding_period", "intraday"),
            "model_version": signal.get("model_version"),
            "trade_id": trade_id,
            "entry_price": signal.get("entry_price"),
        }
        pred_id = await self.ctx.db.insert_prediction(prediction)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"prediction_id": pred_id, "mode": "log"},
        )

    async def _score_elapsed_predictions(self) -> SkillResult:
        """FR-7.2, FR-7.3: Score predictions whose timeframe has elapsed."""
        pending = await self.ctx.db.get_unscored_predictions()
        scored = 0
        correct = 0

        for pred in pending:
            try:
                symbol = pred.get("symbol")
                if not symbol:
                    continue

                # Fetch current price (for elapsed predictions, current price is the outcome)
                try:
                    actual_price = await self.ctx.market_data.get_ltp(symbol)
                except Exception:
                    # Fall back to latest OHLCV close
                    bars = await self.ctx.market_data.get_ohlcv(symbol, "daily", days=1)
                    if bars:
                        actual_price = bars[-1].close
                    else:
                        logger.warning("Cannot get price for %s, skipping", symbol)
                        continue

                entry = pred.get("entry_price", 0)
                if not entry or entry <= 0:
                    continue

                direction = pred.get("predicted_direction", "BUY")

                # Score: direction correct?
                if direction == "BUY":
                    direction_correct = actual_price > entry
                    actual_pnl_pct = (actual_price - entry) / entry
                else:
                    direction_correct = actual_price < entry
                    actual_pnl_pct = (entry - actual_price) / entry

                # Target hit?
                target = pred.get("predicted_target", 0)
                target_hit = (
                    (direction == "BUY" and actual_price >= target)
                    or (direction == "SELL" and actual_price <= target)
                )

                await self.ctx.db.score_prediction(
                    pred["id"],
                    actual_price=actual_price,
                    direction_correct=direction_correct,
                    target_hit=target_hit,
                    actual_pnl_pct=actual_pnl_pct,
                )
                scored += 1
                if direction_correct:
                    correct += 1

            except Exception as e:
                logger.warning("Failed to score prediction %s: %s", pred.get("id"), e)

        # FR-7.3: Update scoreboard
        if scored > 0:
            await self.ctx.db.refresh_prediction_scoreboard()

        # FR-7.6: Trigger failure analysis when enough failures accumulate
        failure_analysis_run = False
        failures_count = scored - correct
        if failures_count >= 5:
            failure_analysis_run = await self._run_failure_analysis()

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "mode": "score",
                "predictions_scored": scored,
                "correct": correct,
                "accuracy": correct / scored if scored > 0 else None,
                "failure_analysis_triggered": failure_analysis_run,
            },
        )
