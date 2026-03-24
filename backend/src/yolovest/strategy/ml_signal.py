"""XGBoost-based ML signal model implementation.

Uses XGBoost for classification (BUY/SELL/HOLD) with probability calibration
via Platt scaling (PM G6). Supports intraday and swing model slots.

All blocking ML operations are offloaded via asyncio.to_thread.
XGBoost and sklearn are lazily imported so tests can run without them.
"""

import asyncio
import logging
from datetime import UTC, datetime
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_feature_vector(features: dict[str, Any]) -> list[list[float]]:
        """Build a 2D feature array from a dict, sorted by key for consistency."""
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

    async def predict_intraday(self, symbol: str, features: dict[str, Any]) -> MLPrediction:
        """Generate intraday signal using the intraday model slot."""
        return await self._predict(symbol, features, "intraday")

    async def predict_swing(self, symbol: str, features: dict[str, Any]) -> MLPrediction:
        """Generate swing signal using the swing model slot."""
        return await self._predict(symbol, features, "swing")

    async def _predict(
        self, symbol: str, features: dict[str, Any], model_type: str
    ) -> MLPrediction:
        model = self._get_model(model_type)
        if model is None:
            raise RuntimeError(
                f"No {model_type} model loaded. Call load_model() first."
            )

        feature_vector = self._build_feature_vector(features)

        def _run_inference() -> tuple[int, float]:
            import numpy as np

            X = np.array(feature_vector)  # noqa: N806
            pred_label = int(model.predict(X)[0])
            # Get probability for the predicted class
            probas = model.predict_proba(X)[0]
            confidence = float(probas[pred_label])
            return pred_label, confidence

        pred_label, confidence = await asyncio.to_thread(_run_inference)

        # Calibrate if calibrator available
        calibrator = self._get_calibrator(model_type)
        if calibrator is not None:

            def _calibrate() -> float:
                import numpy as np

                X = np.array(feature_vector)  # noqa: N806
                cal_probas = calibrator.predict_proba(X)[0]
                return float(cal_probas[pred_label])

            confidence = await asyncio.to_thread(_calibrate)

        signal_type_str = _LABEL_MAP.get(pred_label, "HOLD")

        # Use ATR from features for target/SL computation
        entry_price = features.get("close", features.get("ltp", 100.0))
        atr = features.get("atr", entry_price * 0.02)  # fallback: 2% of price

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

        holding_period = "intraday" if model_type == "intraday" else "3d"

        # Cast to Literal type expected by MLPrediction
        from typing import Literal, cast
        signal_type = cast(Literal["BUY", "SELL", "HOLD"], signal_type_str)

        return MLPrediction(
            signal_type=signal_type,
            entry_price=round(entry_price, 2),
            target_price=round(target_price, 2),
            stop_loss_price=round(stop_loss_price, 2),
            position_size=1,  # risk-check skill determines actual sizing
            holding_period=holding_period,
            confidence=round(confidence, 4),
            model_version=self._get_version(model_type),
        )

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    async def train(
        self, model_type: str, X: Any, y: Any, params: dict[str, Any]  # noqa: N803
    ) -> dict[str, Any]:
        """Train an XGBoost classifier with walk-forward validation.

        Args:
            model_type: "intraday" or "swing"
            X: Feature matrix (numpy array or list of lists)
            y: Label array (numpy array or list)
            params: XGBoost params + optional min_training_samples

        Returns:
            Metrics dict with sharpe, drawdown, win_rate, profit_factor.
        """
        min_samples = params.pop(
            "min_training_samples", _MIN_TRAINING_SAMPLES_DEFAULT
        )

        import numpy as np

        y_arr = np.asarray(y)
        if len(y_arr) < min_samples:
            raise ValueError(
                f"Insufficient training data: {len(y_arr)} samples "
                f"(minimum {min_samples} required) — PM G5"
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

            X_arr = np.asarray(X)  # noqa: N806
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
                "use_label_encoder": False,
                "random_state": 42,
            }

            # Train on full data first
            model = xgb.XGBClassifier(**xgb_params)

            # Collect walk-forward metrics
            all_returns: list[float] = []
            wins = 0
            losses = 0
            gross_profit = 0.0
            gross_loss = 0.0

            for train_idx, test_idx in tscv.split(X_arr):
                X_train, X_test = X_arr[train_idx], X_arr[test_idx]  # noqa: N806
                y_train, y_test = y_arr[train_idx], y_arr[test_idx]

                fold_model = xgb.XGBClassifier(**xgb_params)
                fold_model.fit(X_train, y_train, verbose=False)

                preds = fold_model.predict(X_test)
                # Simulated returns: correct direction = +1%, wrong = -0.5%
                for pred, actual in zip(preds, y_test, strict=False):
                    if pred == actual and pred != 1:  # non-HOLD correct
                        ret = 0.01
                        wins += 1
                        gross_profit += ret
                    elif pred != actual and pred != 1:  # non-HOLD wrong
                        ret = -0.005
                        losses += 1
                        gross_loss += abs(ret)
                    else:
                        ret = 0.0  # HOLD
                    all_returns.append(ret)

            # Final model trained on all data
            model.fit(X_arr, y_arr, verbose=False)

            # Calibrate probabilities (PM G6 — Platt scaling)
            calibrator = CalibratedClassifierCV(
                model, method="sigmoid", cv=min(3, len(y_arr) // 50 or 2)
            )
            calibrator.fit(X_arr, y_arr)

            # Compute metrics
            returns_arr = np.array(all_returns)
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

            total_trades = wins + losses
            win_rate = wins / total_trades if total_trades > 0 else 0.0
            profit_factor = (
                gross_profit / gross_loss if gross_loss > 0 else float("inf")
            )

            metrics = {
                "sharpe": round(sharpe, 4),
                "max_drawdown_pct": round(max_dd, 4),
                "win_rate": round(win_rate, 4),
                "profit_factor": round(profit_factor, 4),
                "total_trades": total_trades,
                "total_samples": len(y_arr),
            }

            return model, calibrator, metrics

        model, calibrator, metrics = await asyncio.to_thread(_train_blocking)

        self._set_model(model_type, model)
        self._set_calibrator(model_type, calibrator)

        version = f"xgb_{model_type}_v{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
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

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        version_str = f"{model_type}_v{timestamp}"
        filename = f"{version_str}.pkl"
        filepath = self.model_dir / filename

        def _save() -> None:
            import joblib

            artifact = {
                "model": model,
                "calibrator": self._get_calibrator(model_type),
                "version": version_str,
                "metrics": metrics,
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
