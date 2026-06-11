"""Skill: predict-track — Log predictions and score outcomes.

Trigger: EVENT (post-trade) + HEARTBEAT (check elapsed predictions)
Pipeline position: Runs after trade-execute (to log) and on heartbeat (to score).

Flow:
Logging (EVENT trigger, post-trade):
1. Log every prediction: symbol, predicted direction, confidence,
   predicted target, predicted timeframe, model version
2. Store with trade_id linkage for full traceability

Scoring (HEARTBEAT trigger):
1. Query predictions whose timeframe has elapsed
2. For each: fetch actual price at prediction end time
3. Compute: was direction correct? did it hit target? actual PnL?
4. Update prediction record with actual outcome
5. Maintain scoreboard: accuracy by symbol, strategy, market condition, timeframe
6. Feed results back to model-retrain for continuous improvement
"""

import logging
from datetime import UTC, datetime
from typing import Any

from yolovest.scoring import path_aware_score
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST

logger = logging.getLogger(__name__)


def _ist_date(raw: Any) -> str | None:
    """ISO/UTC timestamp → IST trading-day string (YYYY-MM-DD)."""
    if not raw:
        return None
    s = str(raw).replace(" ", "T")
    try:
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(IST).strftime("%Y-%m-%d")
    except Exception:
        return s[:10]


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
        """Use Gemini to analyze recent prediction failures."""
        if not self.ctx.config.llm.enabled:
            return False
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
        """Log a new prediction."""
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
            "mode": self.ctx.config.mode,
        }
        pred_id = await self.ctx.db.insert_prediction(prediction)

        logger.info(
            "predict-track: logged %s %s conf=%.2f target=%.2f (pred=%s, trade=%s)",
            signal["signal_type"], signal["symbol"],
            prediction["confidence"], prediction["predicted_target"],
            pred_id, trade_id,
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"prediction_id": pred_id, "mode": "log"},
        )

    async def _score_elapsed_predictions(self) -> SkillResult:
        """Score predictions whose timeframe has elapsed.

        Scores against the actuals on the prediction's END date — the
        actual daily bars over the (created, end] holding window, path-aware
        for target-hit — rather than today's (possibly drifted) LTP. When
        the end-date bar isn't in the DB yet the prediction is left
        pending instead of scored against a stale price.
        """
        pending = await self.ctx.db.get_unscored_predictions(mode=self.ctx.config.mode)
        scored = 0
        correct = 0
        skipped = 0

        for pred in pending:
            try:
                symbol = pred.get("symbol")
                entry = pred.get("entry_price", 0)
                if not symbol or not entry or entry <= 0:
                    continue

                direction = pred.get("predicted_direction", "BUY")
                created_date = _ist_date(pred.get("created_at"))
                end_date = _ist_date(pred.get("prediction_end_time"))
                if not end_date:
                    continue

                # Holding window (created, end]; same-day predictions fall
                # back to the end-date bar alone (intraday path within the day).
                bars = await self.ctx.db.get_daily_ohlc_between(
                    symbol, created_date or end_date, end_date,
                )
                if not bars:
                    one = await self.ctx.db.get_daily_bar_on(symbol, end_date)
                    bars = [one] if one else []
                if not bars:
                    # End-date OHLCV not ingested yet — wait, don't score
                    # against a stale current price.
                    skipped += 1
                    continue

                m = path_aware_score(
                    bars, entry, pred.get("predicted_target"),
                    pred.get("predicted_stop_loss"), direction,
                )
                await self.ctx.db.score_prediction(
                    pred["id"],
                    actual_price=m["actual_close"],
                    direction_correct=bool(m["direction_correct"]),
                    target_hit=bool(m["target_hit"]),
                    actual_pnl_pct=m["actual_move_pct"] / 100.0,
                )
                scored += 1
                if m["direction_correct"]:
                    correct += 1

            except Exception as e:
                logger.warning("Failed to score prediction %s: %s", pred.get("id"), e)

        # Update scoreboard
        if scored > 0:
            await self.ctx.db.refresh_prediction_scoreboard()

        # Trigger failure analysis when enough failures accumulate
        failure_analysis_run = False
        failures_count = scored - correct
        if failures_count >= 5:
            failure_analysis_run = await self._run_failure_analysis()

        if scored > 0 or pending:
            logger.info(
                "predict-track: scored %d/%d predictions, accuracy=%.0f%%%s%s",
                correct, scored,
                (correct / scored * 100) if scored > 0 else 0,
                f", {skipped} awaiting end-date data" if skipped else "",
                ", failure analysis triggered" if failure_analysis_run else "",
            )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "mode": "score",
                "predictions_scored": scored,
                "correct": correct,
                "skipped_awaiting_data": skipped,
                "accuracy": correct / scored if scored > 0 else None,
                "failure_analysis_triggered": failure_analysis_run,
            },
        )
