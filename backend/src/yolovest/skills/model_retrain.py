"""Skill: model-retrain — Retrain ML models and manage versioning.

Trigger: CRON — configurable via retraining.schedule_cron (default: Saturday 6 AM)
Pipeline position: Offline — runs outside market hours.

Flow:
1. Load accumulated prediction vs actual data from DB
2. Load latest OHLCV + features data
3. Retrain both intraday and swing models
4. Version the new model artifacts with metrics
5. Compare new model metrics vs current production model
6. If improved: deploy to shadow mode for retraining.shadow_mode_days
7. If shadow model outperforms after N days: promote to production
8. If shadow model underperforms: rollback to previous version
9. Use Gemini to analyze prediction failures
10. Store analysis for dashboard display
"""

import logging
from typing import Any

from yolovest.data.features import IndicatorConfig, compute_features, merge_feedback_features
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

        # Step 1-2: Load training data and feedback
        training_data = await self.ctx.db.get_training_dataset()
        predictions_vs_actual = await self.ctx.db.get_prediction_outcomes()

        # Load feedback data for the ML feedback loop
        feedback_cfg = self.ctx.config.strategy.feedback
        feedback_data: dict[str, dict[str, float]] | None = None
        if feedback_cfg.enabled:
            feedback_data = await self.ctx.db.get_feedback_data(
                lookback_days=feedback_cfg.lookback_days,
            )
            logger.info(
                "Feedback loop: loaded data for %d symbols (lookback=%dd)",
                len(feedback_data), feedback_cfg.lookback_days,
            )

        # Guard: minimum training data
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
            # Build feature matrix with model-specific labeling + feedback features
            lookahead = lookahead_map[model_type]
            X, y, feat_names, sample_weights, bars_meta = self._prepare_training_data(
                training_data, lookahead_bars=lookahead, feedback_data=feedback_data,
            )
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
                train_params: dict[str, Any] = {}
                if sample_weights:
                    train_params["sample_weights"] = sample_weights
                # Real-PnL backtest config: intraday model uses MIS for
                # cost calc (lower STT); swing model uses CNC.
                train_params["bars_meta"] = bars_meta
                train_params["backtest_product"] = (
                    "MIS" if model_type == "intraday" else "CNC"
                )
                metrics = await self.ctx.ml.train(
                    model_type, X, y, train_params, feature_names=feat_names,
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

        # Step 9: Gemini failure analysis
        failure_analysis = None
        if predictions_vs_actual and self.ctx.config.llm.enabled:
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
        self, training_data: dict[str, Any], lookahead_bars: int = 1,
        feedback_data: dict[str, dict[str, float]] | None = None,
    ) -> tuple[
        list[list[float]], list[int], list[str], list[float],
        list[dict[str, Any]],
    ]:
        """Convert raw OHLCV bars into feature matrix X, labels y, and sample weights.

        Groups bars by symbol, computes technical features using a sliding window,
        and generates labels based on future price returns over lookahead_bars:
          - BUY (2): return > +0.5%
          - SELL (0): return < -0.5%
          - HOLD (1): otherwise

        When feedback_data is provided:
          - Merges per-symbol feedback features (prediction accuracy, trade win rate, etc.)
          - Computes sample weights: upweights symbols where the model recently performed poorly

        Args:
            training_data: Dict with "bars" key containing OHLCV row dicts.
            lookahead_bars: Number of bars to look ahead for labeling.
            feedback_data: Per-symbol feedback stats from get_feedback_data().
        """
        raw_bars = training_data.get("bars", [])
        weight_boost = self.ctx.config.strategy.feedback.sample_weight_boost

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
        sample_weights: list[float] = []
        feature_names: list[str] = []
        feature_names_set: set[str] = set()
        # Parallel to X/y — used by the walk-forward backtest to
        # simulate real PnL instead of the legacy +1%/-0.5% fiction.
        bars_meta: list[dict[str, Any]] = []

        for sym, rows in by_symbol.items():
            if len(rows) < window_size + 1:
                continue

            # Compute sample weight for this symbol based on recent performance
            sym_weight = 1.0
            if feedback_data and sym in feedback_data:
                fb = feedback_data[sym]
                # Upweight symbols where model accuracy was poor (< 50%)
                pred_acc = fb.get("pred_accuracy", 0.5)
                dry_acc = fb.get("dry_run_accuracy", 0.5)
                # Use worst accuracy signal to determine weight
                worst_acc = min(pred_acc, dry_acc)
                if worst_acc < 0.5:
                    sym_weight = weight_boost

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

                # Merge feedback features for this symbol
                if feedback_data:
                    merge_feedback_features(features, sym, feedback_data)

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

                # Maintain a stable feature_names list across all samples.
                # Indicators that need more history (e.g. EMA-200) only
                # appear in features once enough bars are in the window —
                # so later samples may produce keys the first sample
                # didn't have. When that happens, extend feature_names
                # and backfill 0.0 into every prior row so np.array(X)
                # ends up rectangular instead of inhomogeneous.
                for k in features:
                    if k not in feature_names_set:
                        feature_names.append(k)
                        feature_names_set.add(k)
                        for existing in X:
                            existing.append(0.0)
                X.append([features.get(k, 0.0) for k in feature_names])
                y.append(label)
                sample_weights.append(sym_weight)
                bars_meta.append({
                    "symbol": sym,
                    "entry_close": float(current_close),
                    "exit_close": float(future_close),
                })

        return X, y, feature_names, sample_weights, bars_meta

    async def _check_shadow_promotions(self) -> list[dict[str, Any]]:
        """Check if shadow models have completed trial period.

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
