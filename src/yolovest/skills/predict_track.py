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

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


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

    async def _log_prediction(self, signal: dict, trade_id: str | None) -> SkillResult:
        """FR-7.1: Log a new prediction."""
        prediction = {
            "symbol": signal["symbol"],
            "predicted_direction": signal["signal_type"],
            "confidence": signal["confidence_score"],
            "predicted_target": signal["target_price"],
            "predicted_stop_loss": signal["stop_loss_price"],
            "expected_holding_period": signal["expected_holding_period"],
            "model_version": signal.get("model_version"),
            "trade_id": trade_id,
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
            # Check if holding period has elapsed
            if not self._is_elapsed(pred):
                continue

            # Fetch actual price at prediction end
            actual_price = await self.ctx.market_data.get_price_at(
                pred["symbol"], pred["prediction_end_time"]
            )

            # Score: direction correct?
            entry = pred.get("entry_price", pred["predicted_target"])
            if pred["predicted_direction"] == "BUY":
                direction_correct = actual_price > entry
                actual_pnl_pct = (actual_price - entry) / entry
            else:
                direction_correct = actual_price < entry
                actual_pnl_pct = (entry - actual_price) / entry

            # Target hit?
            target_hit = (
                (pred["predicted_direction"] == "BUY" and actual_price >= pred["predicted_target"])
                or (pred["predicted_direction"] == "SELL" and actual_price <= pred["predicted_target"])
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

        # FR-7.3: Update scoreboard
        if scored > 0:
            await self.ctx.db.refresh_prediction_scoreboard()

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "mode": "score",
                "predictions_scored": scored,
                "correct": correct,
                "accuracy": correct / scored if scored > 0 else None,
            },
        )

    def _is_elapsed(self, prediction: dict) -> bool:
        """Check if prediction's expected holding period has passed."""
        raise NotImplementedError
