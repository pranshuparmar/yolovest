"""Skill: model-retrain — Retrain ML models and manage versioning.

Covers: FR-7.4, FR-7.5, FR-7.6, FR-7.7
Trigger: CRON — configurable via retraining.schedule_cron (default: Saturday 6 AM)
Pipeline position: Offline — runs outside market hours.

Flow:
1. Load accumulated prediction vs actual data from DB
2. Load latest OHLCV + features data
3. Retrain both intraday and swing models (FR-7.4):
   - Walk-forward split (no lookahead bias)
   - Train XGBoost/LightGBM on new data
   - Compute metrics: accuracy, Sharpe, max drawdown, win rate
4. Version the new model artifacts with metrics (FR-7.7)
5. Compare new model metrics vs current production model
6. If improved: deploy to shadow mode for retraining.shadow_mode_days (FR-7.5)
7. If shadow model outperforms after N days: promote to production
8. If shadow model underperforms: rollback to previous version
9. Use Gemini to analyze prediction failures (FR-7.6):
   - What patterns does the model consistently get wrong?
   - Market conditions where model breaks down?
10. Store analysis for dashboard display
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class ModelRetrainSkill(SkillBase):
    name = "model-retrain"
    description = "Retrain ML models, version artifacts, A/B test"
    trigger = SkillTrigger.CRON
    schedule = None  # set from retraining.schedule_cron config

    def should_run(self) -> bool:
        # Only run if enough new data has accumulated
        return not self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        cfg = self.ctx.config.retraining

        # Step 1-2: Load training data
        training_data = await self.ctx.db.get_training_dataset()
        predictions_vs_actual = await self.ctx.db.get_prediction_outcomes()

        # Step 3: Retrain models
        intraday_metrics = await self._retrain_model("intraday", training_data)
        swing_metrics = await self._retrain_model("swing", training_data)

        # Step 4: Version artifacts (FR-7.7)
        intraday_version = await self.ctx.ml.save_model(
            "intraday", metrics=intraday_metrics
        )
        swing_version = await self.ctx.ml.save_model(
            "swing", metrics=swing_metrics
        )

        # Step 5: Compare with production
        current_intraday = await self.ctx.ml.get_production_metrics("intraday")
        current_swing = await self.ctx.ml.get_production_metrics("swing")

        intraday_improved = intraday_metrics["sharpe"] > current_intraday.get("sharpe", 0)
        swing_improved = swing_metrics["sharpe"] > current_swing.get("sharpe", 0)

        # Step 6: Deploy to shadow mode if improved (FR-7.5)
        shadow_deployed = []
        if intraday_improved:
            await self.ctx.ml.deploy_shadow("intraday", intraday_version, cfg.shadow_mode_days)
            shadow_deployed.append("intraday")
        if swing_improved:
            await self.ctx.ml.deploy_shadow("swing", swing_version, cfg.shadow_mode_days)
            shadow_deployed.append("swing")

        # Step 7: Check if existing shadow models should be promoted/rolled back
        promotions = await self._check_shadow_promotions()

        # Step 9: Gemini failure analysis (FR-7.6)
        failure_analysis = None
        if predictions_vs_actual:
            failures = [p for p in predictions_vs_actual if not p["direction_correct"]]
            if failures:
                failure_analysis = await self.ctx.llm.analyze_prediction_failures(failures)
                await self.ctx.db.store_failure_analysis(failure_analysis)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "intraday": {
                    "version": intraday_version,
                    "metrics": intraday_metrics,
                    "improved": intraday_improved,
                },
                "swing": {
                    "version": swing_version,
                    "metrics": swing_metrics,
                    "improved": swing_improved,
                },
                "shadow_deployed": shadow_deployed,
                "promotions": promotions,
                "failure_analysis_generated": failure_analysis is not None,
            },
        )

    async def _retrain_model(self, model_type: str, data: Any) -> dict:
        """Retrain a single model with walk-forward validation."""
        raise NotImplementedError

    async def _check_shadow_promotions(self) -> list[dict]:
        """Check if shadow models have completed their trial period. FR-7.5."""
        raise NotImplementedError
