"""XGBoost-based ML signal model implementation.

Uses XGBoost for classification (BUY/SELL/HOLD) with probability calibration
via Platt scaling. Supports intraday and swing model slots.

All blocking ML operations are offloaded via asyncio.to_thread.
XGBoost and sklearn are lazily imported so tests can run without them.
"""

import asyncio
import logging
from datetime import UTC, datetime

from yolovest.timezone import now_ist
from pathlib import Path
from typing import Any

from yolovest.models.schemas import MLPrediction
from yolovest.strategy.ml_base import MLBase

logger = logging.getLogger(__name__)

# Label mapping for model output
_LABEL_MAP = {0: "SELL", 1: "HOLD", 2: "BUY"}
_MIN_TRAINING_SAMPLES_DEFAULT = 200


class XGBoostSignalModel(MLBase):
    """XGBoost/LightGBM signal model with probability calibration.

    Maintains two model slots (intraday, swing) and an optional calibrator
    for each. Models are serialized with joblib.
    """

    def __init__(self, model_dir: str = "./models", db: Any = None) -> None:
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.db = db

        # Model slots
        self._intraday_model: Any | None = None
        self._swing_model: Any | None = None
        self._intraday_calibrator: Any | None = None
        self._swing_calibrator: Any | None = None

        # Track versions
        self._intraday_version: str = "untrained"
        self._swing_version: str = "untrained"

        # Feature names used during training (for consistent inference)
        self._intraday_features: list[str] | None = None
        self._swing_features: list[str] | None = None

        # Shadow model slots (for A/B testing against production)
        self._shadow_intraday_model: Any | None = None
        self._shadow_swing_model: Any | None = None
        self._shadow_intraday_calibrator: Any | None = None
        self._shadow_swing_calibrator: Any | None = None
        self._shadow_intraday_version: str | None = None
        self._shadow_swing_version: str | None = None
        self._shadow_intraday_features: list[str] | None = None
        self._shadow_swing_features: list[str] | None = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_feature_vector(
        features: dict[str, Any],
        expected_features: list[str] | None = None,
    ) -> list[list[float]]:
        """Build a 2D feature array from a dict, sorted by key for consistency.

        If expected_features is set (from training), use exactly those features
        in that order. Missing features get 0.0, extra features are dropped.
        """
        if expected_features:
            values = [float(features.get(k, 0.0)) for k in expected_features]
        else:
            sorted_keys = sorted(features.keys())
            values = [float(features[k]) for k in sorted_keys]
        return [values]  # single-sample 2D array for predict

    def _get_model(self, model_type: str) -> Any:
        if model_type == "intraday":
            return self._intraday_model
        elif model_type == "swing":
            return self._swing_model
        else:
            raise ValueError(f"Unknown model_type: {model_type}")

    def _get_calibrator(self, model_type: str) -> Any:
        if model_type == "intraday":
            return self._intraday_calibrator
        elif model_type == "swing":
            return self._swing_calibrator
        return None

    def _get_version(self, model_type: str) -> str:
        if model_type == "intraday":
            return self._intraday_version
        return self._swing_version

    def _set_model(self, model_type: str, model: Any) -> None:
        if model_type == "intraday":
            self._intraday_model = model
        elif model_type == "swing":
            self._swing_model = model

    def _set_calibrator(self, model_type: str, calibrator: Any) -> None:
        if model_type == "intraday":
            self._intraday_calibrator = calibrator
        elif model_type == "swing":
            self._swing_calibrator = calibrator

    def _set_version(self, model_type: str, version: str) -> None:
        if model_type == "intraday":
            self._intraday_version = version
        elif model_type == "swing":
            self._swing_version = version

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    async def predict_intraday(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction:
        """Generate intraday signal using the intraday model slot."""
        return await self._predict(symbol, features, "intraday", current_price=current_price)

    async def predict_swing(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction:
        """Generate swing signal using the swing model slot."""
        return await self._predict(symbol, features, "swing", current_price=current_price)

    # ------------------------------------------------------------------
    # Shadow prediction (A/B testing)
    # ------------------------------------------------------------------

    def has_shadow(self, model_type: str) -> bool:
        """Check if a shadow model is loaded for this model_type."""
        if model_type == "intraday":
            return self._shadow_intraday_model is not None
        elif model_type == "swing":
            return self._shadow_swing_model is not None
        return False

    def clear_shadow(self, model_type: str) -> None:
        """Unload shadow model after promotion or retirement."""
        if model_type == "intraday":
            self._shadow_intraday_model = None
            self._shadow_intraday_calibrator = None
            self._shadow_intraday_version = None
            self._shadow_intraday_features = None
        elif model_type == "swing":
            self._shadow_swing_model = None
            self._shadow_swing_calibrator = None
            self._shadow_swing_version = None
            self._shadow_swing_features = None

    async def load_shadow_model(
        self, model_type: str, version: str | None = None,
    ) -> None:
        """Load a model into the shadow slot for A/B testing."""
        def _load() -> dict[str, Any]:
            import joblib

            if version:
                filepath = self.model_dir / f"{version}.pkl"
            else:
                pattern = f"{model_type}_v*.pkl"
                matches = sorted(self.model_dir.glob(pattern))
                if not matches:
                    raise FileNotFoundError(
                        f"No saved {model_type} model found in {self.model_dir}"
                    )
                filepath = matches[-1]
            return dict[str, Any](joblib.load(filepath))

        artifact = await asyncio.to_thread(_load)

        if model_type == "intraday":
            self._shadow_intraday_model = artifact["model"]
            self._shadow_intraday_calibrator = artifact.get("calibrator")
            self._shadow_intraday_version = artifact.get("version", "unknown")
            self._shadow_intraday_features = artifact.get("feature_names")
        elif model_type == "swing":
            self._shadow_swing_model = artifact["model"]
            self._shadow_swing_calibrator = artifact.get("calibrator")
            self._shadow_swing_version = artifact.get("version", "unknown")
            self._shadow_swing_features = artifact.get("feature_names")

        logger.info("Loaded shadow %s model version %s",
                     model_type, artifact.get("version", "unknown"))

    async def predict_shadow_intraday(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction | None:
        """Run shadow intraday model. Returns None if no shadow loaded."""
        if not self.has_shadow("intraday"):
            return None
        return await self._predict_shadow(symbol, features, "intraday", current_price=current_price)

    async def predict_shadow_swing(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction | None:
        """Run shadow swing model. Returns None if no shadow loaded."""
        if not self.has_shadow("swing"):
            return None
        return await self._predict_shadow(symbol, features, "swing", current_price=current_price)

    async def _predict_shadow(
        self, symbol: str, features: dict[str, Any], model_type: str,
        *, current_price: float | None = None,
    ) -> MLPrediction:
        """Run inference on the shadow model slot."""
        if model_type == "intraday":
            model = self._shadow_intraday_model
            calibrator = self._shadow_intraday_calibrator
            expected = self._shadow_intraday_features
            version = self._shadow_intraday_version or "unknown"
        else:
            model = self._shadow_swing_model
            calibrator = self._shadow_swing_calibrator
            expected = self._shadow_swing_features
            version = self._shadow_swing_version or "unknown"

        if model is None:
            raise RuntimeError(f"No shadow {model_type} model loaded")

        # Reuse the same inference logic as production
        feature_vector = self._build_feature_vector(features, expected)

        def _run_inference() -> tuple[int, float, list[float]]:
            import numpy as np
            X = np.array(feature_vector)
            pred_label = int(model.predict(X)[0])
            probas = model.predict_proba(X)[0]
            confidence = float(probas[pred_label])
            return pred_label, confidence, [float(p) for p in probas]

        pred_label, raw_confidence, probas_list = await asyncio.to_thread(_run_inference)
        confidence = raw_confidence
        chosen_probas = probas_list

        if calibrator is not None:
            def _calibrate() -> tuple[int, float, list[float]]:
                import numpy as np
                X = np.array(feature_vector)
                cal_label = int(calibrator.predict(X)[0])
                cal_probas = calibrator.predict_proba(X)[0]
                return cal_label, float(cal_probas[cal_label]), [float(p) for p in cal_probas]

            cal_label, cal_confidence, cal_probas = await asyncio.to_thread(_calibrate)
            if cal_confidence > raw_confidence:
                pred_label = cal_label
                confidence = cal_confidence
                chosen_probas = cal_probas

        signal_type_str = _LABEL_MAP.get(pred_label, "HOLD")
        entry_price = current_price or features.get("close", 100.0)
        atr = features.get("atr_14", entry_price * 0.02)

        if signal_type_str == "BUY":
            target_price = entry_price + 2 * atr
            stop_loss_price = entry_price - 1 * atr
        elif signal_type_str == "SELL":
            target_price = entry_price - 2 * atr
            stop_loss_price = entry_price + 1 * atr
        else:
            target_price = entry_price + 1 * atr
            stop_loss_price = entry_price - 1 * atr

        target_price = max(target_price, 0.01)
        stop_loss_price = max(stop_loss_price, 0.01)
        entry_price = max(entry_price, 0.01)

        holding_period = "intraday" if model_type == "intraday" else "3d"  # default; caller overrides via signal

        from typing import Literal, cast
        signal_type = cast(Literal["BUY", "SELL", "HOLD"], signal_type_str)

        class_probs = {
            _LABEL_MAP.get(i, str(i)): round(float(p), 4)
            for i, p in enumerate(chosen_probas)
        }

        return MLPrediction(
            signal_type=signal_type,
            entry_price=round(entry_price, 2),
            target_price=round(target_price, 2),
            stop_loss_price=round(stop_loss_price, 2),
            position_size=1,
            holding_period=holding_period,
            confidence=round(confidence, 4),
            model_version=version,
            class_probabilities=class_probs,
        )

    # ------------------------------------------------------------------
    # Core prediction
    # ------------------------------------------------------------------

    async def _predict(
        self, symbol: str, features: dict[str, Any], model_type: str,
        *, current_price: float | None = None,
    ) -> MLPrediction:
        model = self._get_model(model_type)
        if model is None:
            raise RuntimeError(
                f"No {model_type} model loaded. Call load_model() first."
            )

        expected = (self._intraday_features if model_type == "intraday"
                    else self._swing_features)
        feature_vector = self._build_feature_vector(features, expected)

        def _run_inference() -> tuple[int, float, list[float]]:
            import numpy as np

            X = np.array(feature_vector)  # noqa: N806
            pred_label = int(model.predict(X)[0])
            # Get probability for the predicted class + full distribution
            probas = model.predict_proba(X)[0]
            confidence = float(probas[pred_label])
            return pred_label, confidence, [float(p) for p in probas]

        pred_label, raw_confidence, probas_list = await asyncio.to_thread(_run_inference)
        confidence = raw_confidence
        chosen_probas = probas_list

        # Calibrate if calibrator available
        calibrator = self._get_calibrator(model_type)
        if calibrator is not None:

            def _calibrate() -> tuple[int, float, list[float]]:
                import numpy as np

                X = np.array(feature_vector)  # noqa: N806
                cal_label = int(calibrator.predict(X)[0])
                cal_probas = calibrator.predict_proba(X)[0]
                cal_confidence = float(cal_probas[cal_label])
                return cal_label, cal_confidence, [float(p) for p in cal_probas]

            cal_label, cal_confidence, cal_probas = await asyncio.to_thread(_calibrate)

            if cal_confidence > raw_confidence:
                # Calibration improved confidence — use calibrated values
                pred_label = cal_label
                confidence = cal_confidence
                chosen_probas = cal_probas
            else:
                # Calibration compressed confidence — keep raw model output
                logger.debug(
                    "Calibration compressed %s confidence from %.4f to %.4f, using raw",
                    symbol, raw_confidence, cal_confidence,
                )

        signal_type_str = _LABEL_MAP.get(pred_label, "HOLD")

        # Use fresh LTP for entry/target/SL when available, fall back to features
        entry_price = current_price or features.get("close", 100.0)
        atr = features.get("atr_14", entry_price * 0.02)

        if signal_type_str == "BUY":
            target_price = entry_price + 2 * atr
            stop_loss_price = entry_price - 1 * atr
        elif signal_type_str == "SELL":
            target_price = entry_price - 2 * atr
            stop_loss_price = entry_price + 1 * atr
        else:
            # HOLD: set symmetric levels
            target_price = entry_price + 1 * atr
            stop_loss_price = entry_price - 1 * atr

        # Ensure prices are positive
        target_price = max(target_price, 0.01)
        stop_loss_price = max(stop_loss_price, 0.01)
        entry_price = max(entry_price, 0.01)

        holding_period = "intraday" if model_type == "intraday" else "3d"  # default; caller overrides via signal

        # Cast to Literal type expected by MLPrediction
        from typing import Literal, cast
        signal_type = cast(Literal["BUY", "SELL", "HOLD"], signal_type_str)

        attribution = await asyncio.to_thread(
            self._compute_attribution,
            model, feature_vector, expected, pred_label,
        )

        class_probs = {
            _LABEL_MAP.get(i, str(i)): round(float(p), 4)
            for i, p in enumerate(chosen_probas)
        }

        return MLPrediction(
            signal_type=signal_type,
            entry_price=round(entry_price, 2),
            target_price=round(target_price, 2),
            stop_loss_price=round(stop_loss_price, 2),
            position_size=1,  # risk-check skill determines actual sizing
            holding_period=holding_period,
            confidence=round(confidence, 4),
            model_version=self._get_version(model_type),
            class_probabilities=class_probs,
            attribution=attribution,
        )

    @staticmethod
    def _compute_attribution(
        model: Any,
        feature_vector: list[float],
        feature_names: list[str] | None,
        pred_label: int,
        top_n: int = 5,
    ) -> list[Any] | None:
        """Return the top-N feature contributions to `pred_label` via
        XGBoost's `pred_contribs=True` (TreeSHAP-style). For multiclass
        the output shape is (1, n_classes, n_features+1); the last
        column is bias. Reading the slice for the predicted class
        gives us per-feature contributions in log-odds space.

        Returns None defensively when the booster isn't reachable
        through the model wrapper or anything else throws — we don't
        want a presentation feature to break prediction.
        """
        if not feature_names:
            return None
        try:
            import numpy as np
            import xgboost as xgb

            booster = model.get_booster()
            X = np.array(feature_vector, dtype=np.float32)
            d = xgb.DMatrix(X, feature_names=feature_names)
            contribs = booster.predict(d, pred_contribs=True)
            # Multiclass: (1, n_classes, n_features+1). Binary: (1, n_features+1).
            if contribs.ndim == 3:
                class_idx = min(pred_label, contribs.shape[1] - 1)
                feat_contribs = contribs[0, class_idx, :-1]
            else:
                feat_contribs = contribs[0, :-1]
            # Top-N by absolute value of contribution.
            order = np.argsort(-np.abs(feat_contribs))[:top_n]
            from yolovest.models.schemas import FeatureAttribution
            out: list[Any] = []
            for idx in order:
                idx_i = int(idx)
                if idx_i >= len(feature_names):
                    continue
                out.append(FeatureAttribution(
                    feature=feature_names[idx_i],
                    value=float(feature_vector[idx_i]),
                    contribution=float(feat_contribs[idx_i]),
                ))
            return out
        except Exception:
            logger.debug("_compute_attribution failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    async def train(
        self, model_type: str, X: Any, y: Any, params: dict[str, Any],  # noqa: N803
        feature_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Train an XGBoost classifier with walk-forward validation.

        Args:
            model_type: "intraday" or "swing"
            X: Feature matrix (numpy array or list of lists)
            y: Label array (numpy array or list)
            params: XGBoost params + optional min_training_samples
            feature_names: Ordered feature names matching X columns

        Returns:
            Metrics dict with sharpe, drawdown, win_rate, profit_factor.
        """
        min_samples = params.pop(
            "min_training_samples", _MIN_TRAINING_SAMPLES_DEFAULT
        )
        sample_weights_raw = params.pop("sample_weights", None)
        # Walk-forward backtest inputs — when bars_meta is supplied,
        # we score predictions through the real cost / sizing /
        # slippage model in strategy/walk_forward_backtest.py instead
        # of the legacy +1%/-0.5% synthetic payoff. Callers that don't
        # pass these (e.g. older tests) keep the synthetic path.
        bars_meta_raw = params.pop("bars_meta", None)
        backtest_product = params.pop("backtest_product", "MIS")
        backtest_max_positions = int(params.pop("backtest_max_positions", 0))

        import numpy as np

        y_arr = np.asarray(y)
        weights_arr = (
            np.asarray(sample_weights_raw, dtype=np.float64)
            if sample_weights_raw
            else None
        )
        if len(y_arr) < min_samples:
            raise ValueError(
                f"Insufficient training data: {len(y_arr)} samples "
                f"(minimum {min_samples} required)"
            )

        def _train_blocking() -> tuple[Any, Any, dict[str, Any]]:
            import numpy as np

            try:
                import xgboost as xgb
            except ImportError as e:
                raise ImportError(
                    "xgboost is required for training. "
                    "Install with: pip install xgboost"
                ) from e

            try:
                from sklearn.calibration import CalibratedClassifierCV
                from sklearn.model_selection import TimeSeriesSplit
            except ImportError as e:
                raise ImportError(
                    "scikit-learn is required for training. "
                    "Install with: pip install scikit-learn"
                ) from e

            # float32 halves the feature-matrix memory vs the default
            # float64 (~440 MB → ~220 MB on a 911K × 60 matrix). XGBoost
            # tree-method=hist works natively in float32 and the
            # accuracy difference is negligible at this scale. Help the
            # GC drop the Python list-of-lists as soon as the array is
            # built — list-of-lists has higher per-cell overhead than
            # the ndarray on top of the data it holds.
            X_arr = np.asarray(X, dtype=np.float32)  # noqa: N806
            X.clear()
            import gc as _gc
            _gc.collect()
            y_arr = np.asarray(y)

            # Walk-forward split via TimeSeriesSplit
            tscv = TimeSeriesSplit(n_splits=min(5, len(y_arr) // 50 or 2))

            xgb_params = {
                "n_estimators": params.get("n_estimators", 100),
                "max_depth": params.get("max_depth", 6),
                "learning_rate": params.get("learning_rate", 0.1),
                "objective": "multi:softprob",
                "num_class": 3,
                "eval_metric": "mlogloss",
                "random_state": 42,
                # Histogram tree method: bins continuous features into a
                # fixed number of buckets, avoiding the full sorted matrix.
                "tree_method": params.get("tree_method", "hist"),
            }

            # Train on full data first
            model = xgb.XGBClassifier(**xgb_params)

            # Walk-forward predictions accumulator. We collect every test
            # fold's predictions (with the matching bars_meta when
            # available) and feed them through the real-PnL backtest at
            # the end so the metrics reflect actual costs / sizing /
            # slippage rather than the legacy +1%/-0.5% fiction.
            from yolovest.strategy.walk_forward_backtest import (
                BacktestConfig, BarMeta, run_walk_forward_backtest,
            )

            collected_preds: list[int] = []
            collected_meta: list[BarMeta] = []
            # Legacy synthetic accumulators — kept so trainings without
            # bars_meta (older callers, focused unit tests) still emit
            # comparable metrics.
            synthetic_returns: list[float] = []
            synthetic_wins = 0
            synthetic_losses = 0
            synthetic_gross_profit = 0.0
            synthetic_gross_loss = 0.0

            for train_idx, test_idx in tscv.split(X_arr):
                X_train, X_test = X_arr[train_idx], X_arr[test_idx]  # noqa: N806
                y_train, y_test = y_arr[train_idx], y_arr[test_idx]
                w_train = weights_arr[train_idx] if weights_arr is not None else None

                fold_model = xgb.XGBClassifier(**xgb_params)
                fold_model.fit(X_train, y_train, sample_weight=w_train, verbose=False)

                preds = fold_model.predict(X_test)

                if bars_meta_raw is not None:
                    for pred, idx in zip(preds, test_idx, strict=False):
                        meta = bars_meta_raw[int(idx)]
                        collected_preds.append(int(pred))
                        collected_meta.append(BarMeta(
                            symbol=str(meta.get("symbol", "")),
                            entry_close=float(meta.get("entry_close") or 0.0),
                            exit_close=float(meta.get("exit_close") or 0.0),
                            path_highs=list(meta.get("path_highs") or []),
                            path_lows=list(meta.get("path_lows") or []),
                            target_pct=float(meta.get("target_pct") or 0.0),
                            sl_pct=float(meta.get("sl_pct") or 0.0),
                            entry_date=str(meta.get("entry_date") or ""),
                        ))
                else:
                    # Legacy synthetic payoff — kept for backwards compat
                    # with callers that don't yet thread bars_meta.
                    for pred, actual in zip(preds, y_test, strict=False):
                        if pred == actual and pred != 1:
                            ret = 0.01
                            synthetic_wins += 1
                            synthetic_gross_profit += ret
                        elif pred != actual and pred != 1:
                            ret = -0.005
                            synthetic_losses += 1
                            synthetic_gross_loss += abs(ret)
                        else:
                            ret = 0.0
                        synthetic_returns.append(ret)

            # Free per-fold scratch before the final fit allocates its
            # own DMatrix copy — XGBoost's hist tree-method copies the
            # data into its own bin-quantized representation, briefly
            # doubling memory. On a 2 GB host this can OOM without the
            # collect.
            _gc.collect()
            # Final model trained on all data (with sample weights if available)
            model.fit(X_arr, y_arr, sample_weight=weights_arr, verbose=False)

            # Calibrate probabilities (Platt scaling)
            calibrator = CalibratedClassifierCV(
                model, method="sigmoid", cv=min(3, len(y_arr) // 50 or 2)
            )
            calibrator.fit(X_arr, y_arr)

            if bars_meta_raw is not None and collected_preds:
                # Real-PnL backtest path
                bt = run_walk_forward_backtest(
                    preds=collected_preds,
                    bars_meta=collected_meta,
                    config=BacktestConfig(
                        product=backtest_product,
                        max_concurrent_positions=backtest_max_positions,
                    ),
                )
                metrics = {
                    "sharpe": bt.sharpe,
                    "max_drawdown_pct": bt.max_drawdown_pct,
                    "win_rate": bt.win_rate,
                    "profit_factor": (
                        bt.profit_factor if bt.profit_factor != float("inf")
                        else 999.0
                    ),
                    "total_trades": bt.total_trades,
                    "total_samples": len(y_arr),
                    # Extra real-PnL fields not produced by the legacy
                    # synthetic path — useful on the ML Models dashboard.
                    "net_pnl": bt.net_pnl,
                    "final_capital": bt.final_capital,
                    "backtest_source": "walk_forward_real_pnl",
                    "signals_skipped_at_cap": bt.signals_skipped_at_cap,
                    "backtest_max_positions": backtest_max_positions,
                }
            else:
                # Legacy synthetic metrics — kept for tests / older callers
                returns_arr = np.array(synthetic_returns)
                if len(returns_arr) > 0 and returns_arr.std() > 0:
                    sharpe = float(
                        (returns_arr.mean() / returns_arr.std()) * np.sqrt(252)
                    )
                else:
                    sharpe = 0.0
                equity = np.cumsum(returns_arr) + 1.0
                peak = np.maximum.accumulate(equity)
                drawdowns = (peak - equity) / np.where(peak > 0, peak, 1.0)
                max_dd = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0
                total_trades = synthetic_wins + synthetic_losses
                win_rate = (
                    synthetic_wins / total_trades if total_trades > 0 else 0.0
                )
                profit_factor = (
                    synthetic_gross_profit / synthetic_gross_loss
                    if synthetic_gross_loss > 0 else float("inf")
                )
                metrics = {
                    "sharpe": round(sharpe, 4),
                    "max_drawdown_pct": round(max_dd, 4),
                    "win_rate": round(win_rate, 4),
                    "profit_factor": round(profit_factor, 4),
                    "total_trades": total_trades,
                    "total_samples": len(y_arr),
                    "backtest_source": "synthetic_legacy",
                }

            return model, calibrator, metrics

        model, calibrator, metrics = await asyncio.to_thread(_train_blocking)

        self._set_model(model_type, model)
        self._set_calibrator(model_type, calibrator)

        # Store feature names for consistent inference
        if feature_names:
            if model_type == "intraday":
                self._intraday_features = feature_names
            elif model_type == "swing":
                self._swing_features = feature_names

        # Version stamp in IST so it matches log timestamps the user
        # reads (and matches the daily-bar timezone used elsewhere).
        # UTC stamping previously caused the model version to read 5h30m
        # earlier than the log line that announced its creation.
        version = f"xgb_{model_type}_v{now_ist().strftime('%Y%m%d_%H%M%S')}"
        self._set_version(model_type, version)

        logger.info(
            "Trained %s model %s: sharpe=%.4f, win_rate=%.4f, max_dd=%.4f",
            model_type,
            version,
            metrics["sharpe"],
            metrics["win_rate"],
            metrics["max_drawdown_pct"],
        )

        return metrics

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def save_model(self, model_type: str, metrics: dict[str, Any]) -> str:
        """Serialize model + calibrator to disk with joblib."""
        model = self._get_model(model_type)
        if model is None:
            raise RuntimeError(f"No {model_type} model to save.")

        timestamp = now_ist().strftime("%Y%m%d_%H%M%S")
        version_str = f"{model_type}_v{timestamp}"
        filename = f"{version_str}.pkl"
        filepath = self.model_dir / filename

        def _save() -> None:
            import joblib

            feature_names = (self._intraday_features if model_type == "intraday"
                             else self._swing_features)
            artifact = {
                "model": model,
                "calibrator": self._get_calibrator(model_type),
                "version": version_str,
                "metrics": metrics,
                "feature_names": feature_names,
                "saved_at": datetime.now(UTC).isoformat(),
            }
            joblib.dump(artifact, filepath)

        await asyncio.to_thread(_save)
        self._set_version(model_type, version_str)
        logger.info("Saved %s model to %s", model_type, filepath)
        return version_str

    async def load_model(
        self, model_type: str, version: str | None = None
    ) -> None:
        """Load model from joblib file.

        If version is None, load the latest file matching the model_type pattern.
        """

        def _load() -> dict[str, Any]:
            import joblib

            if version:
                filepath = self.model_dir / f"{version}.pkl"
            else:
                # Find latest matching file
                pattern = f"{model_type}_v*.pkl"
                matches = sorted(self.model_dir.glob(pattern))
                if not matches:
                    raise FileNotFoundError(
                        f"No saved {model_type} model found in {self.model_dir}"
                    )
                filepath = matches[-1]

            return dict[str, Any](joblib.load(filepath))

        artifact = await asyncio.to_thread(_load)

        self._set_model(model_type, artifact["model"])
        self._set_calibrator(model_type, artifact.get("calibrator"))
        self._set_version(model_type, artifact.get("version", "unknown"))

        # Restore feature names for consistent inference
        feature_names = artifact.get("feature_names")
        if feature_names:
            if model_type == "intraday":
                self._intraday_features = feature_names
            elif model_type == "swing":
                self._swing_features = feature_names

        logger.info(
            "Loaded %s model version %s", model_type, self._get_version(model_type)
        )

    # ------------------------------------------------------------------
    # Production metrics & shadow deployment
    # ------------------------------------------------------------------

    async def get_production_metrics(self, model_type: str) -> dict[str, Any]:
        """Read production metrics from DB model_versions table."""
        if self.db is None:
            return {}

        try:
            rows = await self.db.execute(
                "SELECT * FROM model_versions WHERE model_type = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (model_type,),
            )
            if rows:
                return dict(rows[0]) if rows[0] else {}
        except Exception:
            logger.warning(
                "Could not read production metrics for %s", model_type
            )
        return {}

    async def deploy_shadow(
        self, model_type: str, version: str, days: int
    ) -> None:
        """Mark a model version for shadow deployment in DB."""
        if self.db is None:
            logger.warning("No DB configured, skipping shadow deployment.")
            return

        try:
            await self.db.execute(
                "INSERT OR REPLACE INTO model_versions "
                "(model_type, version, status, shadow_days, created_at) "
                "VALUES (?, ?, 'shadow', ?, ?)",
                (model_type, version, days, datetime.now(UTC).isoformat()),
            )
            logger.info(
                "Deployed %s version %s in shadow mode for %d days",
                model_type,
                version,
                days,
            )
        except Exception:
            logger.warning(
                "Could not deploy shadow for %s %s", model_type, version
            )
