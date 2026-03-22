"""Skill: model-retrain — Retrain ML models and manage versioning.

Covers: FR-7.4, FR-7.5, FR-7.6, FR-7.7
Trigger: CRON — configurable via retraining.schedule_cron (default: Saturday 6 AM)
Pipeline position: Offline — runs outside market hours.

Flow:
1. Load accumulated prediction vs actual data from DB
2. Load latest OHLCV + features data
3. Retrain both intraday and swing models (FR-7.4)
4. Version the new model artifacts with metrics (FR-7.7)
5. Compare new model metrics vs current production model
6. If improved: deploy to shadow mode for retraining.shadow_mode_days (FR-7.5)
7. If shadow model outperforms after N days: promote to production
8. If shadow model underperforms: rollback to previous version
9. Use Gemini to analyze prediction failures (FR-7.6)
10. Store analysis for dashboard display
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class ModelRetrainSkill(SkillBase):
    name = "model-retrain"
    description = "Retrain ML models, version artifacts, A/B test"
    trigger = SkillTrigger.CRON
    schedule = None  # set from retraining.schedule_cron config

    def should_run(self) -> bool:
        return not self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        if self.ctx.ml is None:
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"reason": "no_ml_provider"},
            )

        cfg = self.ctx.config.retraining
        min_samples = self.ctx.config.strategy.min_training_samples

        # Step 1-2: Load training data
        training_data = await self.ctx.db.get_training_dataset()
        predictions_vs_actual = await self.ctx.db.get_prediction_outcomes()

        # Guard: minimum training data (PM G5)
        bar_count = len(training_data.get("bars", []))
        if bar_count < min_samples:
            logger.warning(
                "Insufficient training data (%d bars, need %d), skipping retrain",
                bar_count, min_samples,
            )
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"reason": "insufficient_data", "bar_count": bar_count},
            )

        # Step 3: Retrain models
        results: dict[str, Any] = {}
        shadow_deployed = []

        for model_type in ("intraday", "swing"):
            try:
                metrics = await self.ctx.ml.train(
                    model_type, training_data, None, {}
                )
                version = await self.ctx.ml.save_model(model_type, metrics=metrics)
                await self.ctx.db.save_model_version(
                    model_type, version, f"models/{model_type}_{version}.pkl", metrics
                )

                # Step 5: Compare with production
                current = await self.ctx.db.get_production_model(model_type)
                current_sharpe = current.get("sharpe_ratio", 0) if current else 0

                improved = metrics.get("sharpe_ratio", 0) > current_sharpe
                if improved:
                    await self.ctx.ml.deploy_shadow(model_type, version, cfg.shadow_mode_days)
                    shadow_deployed.append(model_type)

                results[model_type] = {
                    "version": version,
                    "metrics": metrics,
                    "improved": improved,
                }
            except Exception as e:
                logger.warning("Retrain failed for %s: %s", model_type, e)
                results[model_type] = {"error": str(e)}

        # Step 7: Check shadow promotions
        promotions = await self._check_shadow_promotions()

        # Step 9: Gemini failure analysis (FR-7.6)
        failure_analysis = None
        if predictions_vs_actual:
            failures = [p for p in predictions_vs_actual if not p.get("direction_correct")]
            if failures:
                try:
                    failure_analysis = await self.ctx.llm.analyze_prediction_failures(failures)
                    await self.ctx.db.store_failure_analysis(failure_analysis)
                except Exception as e:
                    logger.warning("Failure analysis failed: %s", e)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "models": results,
                "shadow_deployed": shadow_deployed,
                "promotions": promotions,
                "failure_analysis_generated": failure_analysis is not None,
            },
        )

    async def _check_shadow_promotions(self) -> list[dict]:
        """Check if shadow models have completed trial period. FR-7.5."""
        # In production, query model_versions for shadow models past shadow_mode_days
        # and compare their live performance vs production. For now, return empty.
        return []
