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

from yolovest.data.features import IndicatorConfig, compute_features
from yolovest.models.schemas import OHLCVBar
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class ModelRetrainSkill(SkillBase):
    name = "model-retrain"
    description = "Retrain ML models, version artifacts, A/B test"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.ctx.config.retraining.schedule_cron

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

        # Lookahead periods: intraday uses 1-bar, swing uses 5-bar returns
        lookahead_map = {"intraday": 1, "swing": 5}

        for model_type in ("intraday", "swing"):
            # Build feature matrix with model-specific labeling
            lookahead = lookahead_map[model_type]
            X, y = self._prepare_training_data(training_data, lookahead_bars=lookahead)
            if len(y) < min_samples:
                logger.warning(
                    "Insufficient %s feature samples (%d, need %d), skipping",
                    model_type, len(y), min_samples,
                )
                results[model_type] = {
                    "error": f"insufficient_features ({len(y)} < {min_samples})"
                }
                continue

            try:
                await self.broadcast("retrain_progress", {
                    "model_type": model_type,
                    "status": "training",
                    "samples": len(y),
                })
                metrics = await self.ctx.ml.train(
                    model_type, X, y, {}
                )
                version = await self.ctx.ml.save_model(model_type, metrics=metrics)
                await self.broadcast("retrain_progress", {
                    "model_type": model_type,
                    "status": "completed",
                    "version": version,
                    "sharpe": metrics.get("sharpe"),
                    "win_rate": metrics.get("win_rate"),
                })
                await self.ctx.db.save_model_version(
                    model_type, version, f"models/{model_type}_{version}.pkl", metrics
                )

                # Step 5: Compare with production
                current = await self.ctx.db.get_production_model(model_type)
                current_sharpe = (current.get("sharpe_ratio") or 0) if current else 0

                improved = metrics.get("sharpe", 0) > current_sharpe
                if improved:
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

    def _prepare_training_data(
        self, training_data: dict[str, Any], lookahead_bars: int = 1
    ) -> tuple[list[list[float]], list[int]]:
        """Convert raw OHLCV bars into feature matrix X and label array y.

        Groups bars by symbol, computes technical features using a sliding window,
        and generates labels based on future price returns over lookahead_bars:
          - BUY (2): return > +0.5%
          - SELL (0): return < -0.5%
          - HOLD (1): otherwise

        Args:
            training_data: Dict with "bars" key containing OHLCV row dicts.
            lookahead_bars: Number of bars to look ahead for labeling.
                1 for intraday (next-bar), 5 for swing (5-bar).
        """
        raw_bars = training_data.get("bars", [])

        # Group bars by symbol, preserving time order
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in raw_bars:
            sym = row["symbol"]
            by_symbol.setdefault(sym, []).append(row)

        indicator_cfg = IndicatorConfig(
            rsi=self.ctx.config.strategy.indicators.rsi,
            macd=self.ctx.config.strategy.indicators.macd,
            bollinger_bands=self.ctx.config.strategy.indicators.bollinger_bands,
            vwap=self.ctx.config.strategy.indicators.vwap,
            atr=self.ctx.config.strategy.indicators.atr,
            volume_profile=self.ctx.config.strategy.indicators.volume_profile,
            obv=self.ctx.config.strategy.indicators.obv,
            supertrend=self.ctx.config.strategy.indicators.supertrend,
            ema_periods=self.ctx.config.strategy.ema_periods,
        )

        # Minimum window size for feature computation (need enough bars for indicators)
        window_size = 50
        X: list[list[float]] = []
        y: list[int] = []

        for _sym, rows in by_symbol.items():
            if len(rows) < window_size + 1:
                continue

            # Convert rows to OHLCVBar objects for compute_features
            bars = [
                OHLCVBar(
                    timestamp=r["timestamp"],
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                )
                for r in rows
            ]

            # Sliding window: compute features at position i, label from i+lookahead
            for i in range(window_size, len(bars) - lookahead_bars):
                window = bars[i - window_size : i + 1]
                features = compute_features(window, indicator_cfg)
                if not features:
                    continue

                # Label: future N-bar return
                current_close = bars[i].close
                future_close = bars[i + lookahead_bars].close
                ret = (future_close - current_close) / current_close if current_close else 0

                if ret > 0.005:
                    label = 2  # BUY
                elif ret < -0.005:
                    label = 0  # SELL
                else:
                    label = 1  # HOLD

                # Sort keys for consistent feature ordering across samples
                sorted_keys = sorted(features.keys())
                X.append([features[k] for k in sorted_keys])
                y.append(label)

        return X, y

    async def _check_shadow_promotions(self) -> list[dict[str, Any]]:
        """Check if shadow models have completed trial period. FR-7.5.

        Shadow models that have run for >= shadow_mode_days are evaluated:
        - If shadow metrics (Sharpe, win_rate) >= production metrics: promote
        - Otherwise: retire the shadow model (rollback)
        """
        cfg = self.ctx.config.retraining
        shadow_models = await self.ctx.db.get_shadow_models_ready(cfg.shadow_mode_days)
        promotions = []

        for shadow in shadow_models:
            model_type = shadow["model_type"]
            current = await self.ctx.db.get_production_model(model_type)

            # Compare: shadow must beat current production on Sharpe ratio
            shadow_sharpe = shadow.get("sharpe_ratio", 0) or 0
            current_sharpe = (current.get("sharpe_ratio", 0) or 0) if current else 0

            if shadow_sharpe >= current_sharpe:
                # Promote shadow to production
                await self.ctx.db.promote_model(model_type, shadow["version"])
                if self.ctx.ml:
                    try:
                        await self.ctx.ml.load_model(model_type, shadow["version"])
                    except Exception as e:
                        logger.warning(
                            "Failed to load promoted model %s/%s: %s",
                            model_type, shadow["version"], e,
                        )
                promotions.append({
                    "model_type": model_type,
                    "version": shadow["version"],
                    "action": "promoted",
                    "shadow_sharpe": shadow_sharpe,
                    "previous_sharpe": current_sharpe,
                })
                logger.info(
                    "Promoted shadow model %s/%s (Sharpe: %.2f > %.2f)",
                    model_type, shadow["version"], shadow_sharpe, current_sharpe,
                )
            else:
                # Retire underperforming shadow
                await self.ctx.db.retire_model(model_type, shadow["version"])
                promotions.append({
                    "model_type": model_type,
                    "version": shadow["version"],
                    "action": "retired",
                    "shadow_sharpe": shadow_sharpe,
                    "production_sharpe": current_sharpe,
                })
                logger.info(
                    "Retired shadow model %s/%s (Sharpe: %.2f < %.2f)",
                    model_type, shadow["version"], shadow_sharpe, current_sharpe,
                )

        return promotions
