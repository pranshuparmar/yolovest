"""XGBoost-based ML signal model implementation.

Uses XGBoost for classification (BUY/SELL/HOLD) with probability calibration
via Platt scaling. Supports intraday and swing model slots.

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
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)


def _lib_version(module: str) -> str:
    """Best-effort library version string for artifact compatibility
    stamps. Returns 'unknown' if the package can't be queried."""
    try:
        from importlib.metadata import version
        return version(module)
    except Exception:
        return "unknown"


def _warn_on_lib_skew(artifact: dict[str, Any], label: str) -> None:
    """Loud (fail-open) warning when a model artifact was trained under a
    different xgboost/sklearn than the running image.

    Pickled estimators usually survive minor version bumps, but sklearn
    explicitly does not guarantee it — and a silently-shifted calibrator
    changes the probabilities every signal gate reads. The cross-machine
    import endpoint hard-gates on these stamps; startup loads stay
    fail-open (the model still loads) but tell the user to retrain so the
    artifact re-pins to the running versions. Pre-stamp artifacts (no
    version fields) are skipped.
    """
    for lib, key in (("xgboost", "xgboost_version"), ("scikit-learn", "sklearn_version")):
        stamped = artifact.get(key)
        if not stamped or stamped == "unknown":
            continue
        running = _lib_version(lib)
        if running != "unknown" and running != stamped:
            logger.warning(
                "%s: model %s was trained under %s %s but this image runs %s — "
                "calibrated probabilities may shift. Run the model-retrain "
                "skill to re-pin the artifact to the current libraries.",
                label, artifact.get("version", "?"), lib, stamped, running,
            )


def _purge_boundary(
    bars_meta_raw: list[dict[str, Any]],
    cut: int,
    lookahead_bars: int,
    min_keep: int,
    embargo_days: int = 0,
) -> int:
    """Largest index ≤ `cut` a tuning model may train up to without its
    label window peeking into the holdout that begins at `cut`.

    A training sample at date d carries a label computed from ~lookahead
    future bars; if that reaches the holdout's first date the label saw
    holdout-period data → leakage into the tuning model. Walk back from
    `cut` past any sample within the lookahead (converted to calendar
    days) plus `embargo_days` of the holdout start. The embargo widens the
    gap beyond label overlap to absorb serial-correlation leakage between
    the train tail and the holdout head. Floored at `min_keep` so the
    tuning fit is never starved. Returns `cut` unchanged when there's no
    lookahead/embargo or dates are unusable.
    """
    if (lookahead_bars <= 0 and embargo_days <= 0) or not bars_meta_raw or cut <= 0:
        return cut
    from datetime import date as _date
    from datetime import timedelta as _td

    def _md(i: int) -> "_date | None":
        try:
            return _date.fromisoformat(str(bars_meta_raw[i].get("entry_date", ""))[:10])
        except (ValueError, TypeError, IndexError, AttributeError):
            return None

    ho_start = _md(cut) if cut < len(bars_meta_raw) else None
    if ho_start is None:
        return cut
    boundary = ho_start - _td(days=int(lookahead_bars * 7 / 5) + 2 + max(0, embargo_days))
    j = cut
    while j > min_keep:
        d = _md(j - 1)
        if d is None or d < boundary:
            break
        j -= 1
    return max(j, min_keep)



# Label mapping for model output
_LABEL_MAP = {0: "SELL", 1: "HOLD", 2: "BUY"}
_LABEL_SELL = 0
_LABEL_HOLD = 1
_LABEL_BUY = 2
_MIN_TRAINING_SAMPLES_DEFAULT = 200


class XGBoostSignalModel(MLBase):
    """XGBoost/LightGBM signal model with probability calibration.

    Maintains two model slots (intraday, swing) and an optional calibrator
    for each. Models are serialized with joblib.
    """

    def __init__(
        self,
        model_dir: str = "./models",
        db: Any = None,
        config: Any = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.db = db
        # Held weakly — only used to read the configured
        # risk.tuned_threshold_max_diff at inference time. Hot-reload
        # of config updates ctx.config in-place which this still
        # tracks via the same reference.
        self._config = config

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

        # PnL-tuned class thresholds from the post-CV sweep. When set,
        # _predict applies them on the calibrated probability vector
        # instead of using argmax. None → argmax baseline (legacy
        # models without tuning saved).
        self._intraday_thresholds: dict[str, float] | None = None
        self._swing_thresholds: dict[str, float] | None = None

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

    def _get_thresholds(self, model_type: str) -> dict[str, float] | None:
        if model_type == "intraday":
            return self._intraday_thresholds
        if model_type == "swing":
            return self._swing_thresholds
        return None

    def get_effective_thresholds(
        self, model_type: str,
    ) -> dict[str, float] | None:
        """Public override of MLBase.get_effective_thresholds — see
        base for semantics. Alias for the internal helper.
        """
        return self._get_effective_thresholds(model_type)

    def _get_effective_thresholds(
        self, model_type: str,
    ) -> dict[str, float] | None:
        """Return the model's tuned thresholds with config-driven
        overrides + the max-diff cap applied. Resolution order:

        1. If `risk.{buy,sell}_threshold_override` is set, that value
           wins outright for the corresponding class. Use this when
           the saved tuned thresholds are unreachable in production
           (e.g. tuner saved buy=0.80 but the model's calibrated
           P(BUY) rarely exceeds 0.50, so no BUY ever fires).
        2. Otherwise start from the saved tuned values.
        3. Apply the `tuned_threshold_max_diff` symmetry cap so a
           wildly asymmetric saved pair can't class-collapse the model.

        Public-ish so the balanced-mode chooser in generate_signals
        can use the same numbers for its margin-above-threshold
        comparison.
        """
        thresholds = self._get_thresholds(model_type)
        if not thresholds:
            return None
        try:
            buy = float(thresholds.get("buy", 0.5))
            sell = float(thresholds.get("sell", 0.5))
        except (TypeError, ValueError):
            return thresholds

        risk_cfg = getattr(self._config, "risk", None) if self._config else None
        # Explicit overrides win — and they suppress the symmetry cap
        # too, since the user has explicitly chosen these values.
        buy_override = getattr(risk_cfg, "buy_threshold_override", None)
        sell_override = getattr(risk_cfg, "sell_threshold_override", None)
        if buy_override is not None or sell_override is not None:
            out_buy = float(buy_override) if buy_override is not None else buy
            out_sell = float(sell_override) if sell_override is not None else sell
            logger.debug(
                "Threshold override applied to %s: buy %.3f→%.3f, "
                "sell %.3f→%.3f",
                model_type, buy, out_buy, sell, out_sell,
            )
            return {"buy": round(out_buy, 4), "sell": round(out_sell, 4)}

        # No override — apply the symmetry cap first.
        max_diff = float(
            getattr(risk_cfg, "tuned_threshold_max_diff", 0.05)
            if risk_cfg is not None else 0.05
        )
        diff = abs(buy - sell)
        if diff > max_diff:
            # Shrink both toward midpoint so the gap is exactly
            # max_diff while preserving the direction the model
            # learned (i.e. if tuned buy was higher, it stays higher).
            midpoint = (buy + sell) / 2.0
            half_gap = max_diff / 2.0
            if buy > sell:
                new_buy = midpoint + half_gap
                new_sell = midpoint - half_gap
            else:
                new_buy = midpoint - half_gap
                new_sell = midpoint + half_gap
            logger.debug(
                "Threshold-diff cap applied to %s: buy %.3f→%.3f, "
                "sell %.3f→%.3f (max_diff=%.2f)",
                model_type, buy, new_buy, sell, new_sell, max_diff,
            )
            buy, sell = new_buy, new_sell

        # Then the absolute-ceiling cap. Pulls thresholds above the
        # configured ceiling back down so a sweep that landed on
        # (0.70, 0.70) can't class-collapse the live model when the
        # calibrated probabilities rarely reach that high.
        max_value = float(
            getattr(risk_cfg, "tuned_threshold_max_value", 0.60)
            if risk_cfg is not None else 0.60
        )
        if buy > max_value or sell > max_value:
            new_buy = min(buy, max_value)
            new_sell = min(sell, max_value)
            logger.debug(
                "Threshold-ceiling cap applied to %s: buy %.3f→%.3f, "
                "sell %.3f→%.3f (max_value=%.2f)",
                model_type, buy, new_buy, sell, new_sell, max_value,
            )
            buy, sell = new_buy, new_sell

        return {"buy": round(buy, 4), "sell": round(sell, 4)}

    def _set_thresholds(
        self, model_type: str, thresholds: dict[str, float] | None,
    ) -> None:
        if model_type == "intraday":
            self._intraday_thresholds = thresholds
        elif model_type == "swing":
            self._swing_thresholds = thresholds

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
        _warn_on_lib_skew(artifact, f"load_shadow_model[{model_type}]")

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

            X = np.array(feature_vector)
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

                X = np.array(feature_vector)
                cal_label = int(calibrator.predict(X)[0])
                cal_probas = calibrator.predict_proba(X)[0]
                cal_confidence = float(cal_probas[cal_label])
                return cal_label, cal_confidence, [float(p) for p in cal_probas]

            cal_label, cal_confidence, cal_probas = await asyncio.to_thread(_calibrate)

            # Only adopt calibrated probabilities when the calibrator
            # AGREES with the raw model on the argmax class. The
            # CalibratedClassifierCV uses sigmoid Platt scaling that,
            # on a HOLD-dominated label distribution (e.g. the swing
            # model's ~73% HOLD), systematically compresses directional
            # predictions back toward the HOLD prior — even when the
            # class-weighted XGBoost has clear conviction. When raw
            # says BUY/SELL but calibrator pulls it to HOLD, that's
            # the over-correction kicking in; trust the trained model.
            # When they agree on direction, use the higher-confidence
            # version (calibration is doing its job refining the
            # probability magnitude). The old "always use higher
            # confidence" rule silently flipped most swing
            # directional argmaxes into HOLD.
            if cal_label == pred_label:
                if cal_confidence > raw_confidence:
                    confidence = cal_confidence
                    chosen_probas = cal_probas
                else:
                    logger.debug(
                        "Calibration compressed %s confidence from %.4f to %.4f, using raw",
                        symbol, raw_confidence, cal_confidence,
                    )
            else:
                logger.debug(
                    "Calibrator disagrees with raw on %s: raw=%s@%.3f cal=%s@%.3f, keeping raw probas",
                    symbol,
                    _LABEL_MAP.get(pred_label, "?"), raw_confidence,
                    _LABEL_MAP.get(cal_label, "?"), cal_confidence,
                )

        # Apply tuned class thresholds when the model was trained with the
        # PnL-tuned threshold sweep. Replaces argmax with: BUY if
        # P(BUY) >= buy_thresh AND >= P(SELL); SELL if P(SELL) >=
        # sell_thresh AND > P(BUY); else HOLD. Legacy models without
        # tuned thresholds keep their argmax label.
        # `_get_effective_thresholds` applies the configured
        # tuned_threshold_max_diff cap so a wildly asymmetric (buy,
        # sell) pair from the threshold sweep can't class-collapse
        # the model in production.
        thresholds = self._get_effective_thresholds(model_type)
        if thresholds and len(chosen_probas) >= 3:
            buy_prob = chosen_probas[_LABEL_BUY]
            sell_prob = chosen_probas[_LABEL_SELL]
            if buy_prob >= thresholds["buy"] and buy_prob >= sell_prob:
                pred_label = _LABEL_BUY
            elif sell_prob >= thresholds["sell"] and sell_prob > buy_prob:
                pred_label = _LABEL_SELL
            else:
                pred_label = _LABEL_HOLD
            confidence = chosen_probas[pred_label]

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

    def predict_labels_batch(self, X: Any, model_type: str) -> list[int]:  # noqa: N803
        """Production-path class labels for a batch of feature vectors.

        Mirrors `_predict`'s decision exactly — raw probabilities, then
        calibrated probabilities adopted ONLY when the calibrator agrees on
        argmax AND is more confident, then the tuned-threshold gate — but
        vectorised over a batch with none of the per-symbol entry-price /
        attribution work. Used by the post-train guard to verify the
        *deployed* model (calibration + thresholds), not the raw booster
        argmax, actually produces non-HOLD signals at its thresholds.

        Returns a list of int labels (0=SELL, 1=HOLD, 2=BUY).
        """
        import numpy as np

        model = self._get_model(model_type)
        if model is None:
            return []
        Xa = np.asarray(X)
        raw = model.predict_proba(Xa)
        calibrator = self._get_calibrator(model_type)
        cal = calibrator.predict_proba(Xa) if calibrator is not None else None
        thresholds = self._get_effective_thresholds(model_type)
        labels: list[int] = []
        for i in range(len(raw)):
            rp = raw[i]
            raw_label = int(np.argmax(rp))
            chosen = rp
            if cal is not None:
                cp = cal[i]
                cal_label = int(np.argmax(cp))
                if cal_label == raw_label and float(cp[cal_label]) > float(rp[raw_label]):
                    chosen = cp
            if thresholds and len(chosen) >= 3:
                buy_p = float(chosen[_LABEL_BUY])
                sell_p = float(chosen[_LABEL_SELL])
                if buy_p >= thresholds["buy"] and buy_p >= sell_p:
                    labels.append(_LABEL_BUY)
                elif sell_p >= thresholds["sell"] and sell_p > buy_p:
                    labels.append(_LABEL_SELL)
                else:
                    labels.append(_LABEL_HOLD)
            else:
                labels.append(int(np.argmax(chosen)))
        return labels

    @staticmethod
    def _compute_attribution(
        model: Any,
        feature_vector: list[Any],
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
        # Label lookahead in trading days — used to purge train samples
        # whose label window overlaps the test fold (cross-sectional
        # leakage). 0 disables purging.
        lookahead_bars = int(params.pop("lookahead_bars", 0))

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
            X_arr = np.asarray(X, dtype=np.float32)
            X.clear()
            import gc as _gc
            _gc.collect()
            y_arr = np.asarray(y)

            # Walk-forward split via TimeSeriesSplit
            tscv = TimeSeriesSplit(n_splits=min(5, len(y_arr) // 50 or 2))

            # Hyperparameter resolution precedence: explicit `params`
            # (caller / test override) > config.retraining.xgb > literal
            # default. The literals mirror the XGBoostConfig defaults so a
            # config-less model (config=None, or a config without a
            # retraining section) still trains with the same regularized
            # setup. Subsample / colsample / min_child_weight / gamma /
            # reg_lambda are the core variance-reduction knobs for noisy
            # financial features — fixed n_estimators=100 / max_depth=6 with
            # no regularization over-fits daily and under-fits the much
            # larger 5-min corpus.
            _xgbc = getattr(getattr(self._config, "retraining", None), "xgb", None)

            def _hp(name: str, literal: Any) -> Any:
                if name in params:
                    return params[name]
                if _xgbc is not None:
                    return getattr(_xgbc, name, literal)
                return literal

            xgb_params = {
                "n_estimators": _hp("n_estimators", 400),
                "max_depth": _hp("max_depth", 6),
                "learning_rate": _hp("learning_rate", 0.05),
                "min_child_weight": _hp("min_child_weight", 5.0),
                "subsample": _hp("subsample", 0.8),
                "colsample_bytree": _hp("colsample_bytree", 0.8),
                "gamma": _hp("gamma", 0.0),
                "reg_lambda": _hp("reg_lambda", 1.0),
                "reg_alpha": _hp("reg_alpha", 0.0),
                "objective": "multi:softprob",
                "num_class": 3,
                "eval_metric": "mlogloss",
                "random_state": 42,
                # Histogram tree method: bins continuous features into a
                # fixed number of buckets, avoiding the full sorted matrix.
                "tree_method": params.get("tree_method", "hist"),
                # Pin to a single thread. Inference runs inside
                # asyncio.to_thread, and signal generation predicts
                # across hundreds of symbols concurrently — letting
                # XGBoost default to "all cores per call" oversubscribes
                # the box (8 cores × 8 concurrent predicts = 64 OS
                # threads thrashing each other). Override via params
                # if you're training offline and want full parallelism.
                "n_jobs": params.get("n_jobs", 1),
            }
            # Early-stopping knobs (read here so the final-fit block can use
            # them). 0 rounds = off; small corpora below the min-samples
            # floor also skip it.
            _es_rounds = int(_hp("early_stopping_rounds", 0))
            _es_min_samples = int(_hp("early_stopping_min_samples", 2000))

            # Train on full data first
            model = xgb.XGBClassifier(**xgb_params)

            # Walk-forward predictions accumulator. We collect every test
            # fold's predictions (with the matching bars_meta when
            # available) and feed them through the real-PnL backtest at
            # the end so the metrics reflect actual costs / sizing /
            # slippage rather than the legacy +1%/-0.5% fiction.
            from yolovest.strategy.walk_forward_backtest import (
                BacktestConfig,
                BarMeta,
                _bootstrap_sharpe_lower_bound,
                backtest_by_period,
                run_walk_forward_backtest,
                sweep_thresholds,
            )
            from yolovest.strategy.walk_forward_backtest import (
                apply_thresholds as _apply_thresholds,
            )

            collected_preds: list[int] = []
            collected_meta: list[BarMeta] = []
            # Per-fold class probabilities — only used when bars_meta is
            # provided; needed for the post-CV threshold sweep that finds
            # the (buy, sell) cutoff pair maximising backtest Sharpe.
            collected_probas: list[list[float]] = []
            # Legacy synthetic accumulators — kept so trainings without
            # bars_meta (older callers, focused unit tests) still emit
            # comparable metrics.
            synthetic_returns: list[float] = []
            synthetic_wins = 0
            synthetic_losses = 0
            synthetic_gross_profit = 0.0
            synthetic_gross_loss = 0.0

            # Threshold tuning + metrics use a final-scale chronological
            # holdout (see the metrics block below) when there's enough
            # data + bars_meta: a tuning model trained only on the early
            # data scores a strict-future holdout, so its probabilities
            # are at ~the deployed full-data model's scale. The per-fold
            # OOF probabilities collected here come from models trained on
            # less data — more overfit, more over-confident — so tuned
            # thresholds came out unreachable at inference (every signal
            # collapsed to HOLD). That path doesn't use collected_*, so
            # skip the (expensive) K-fold loop when it applies.
            _holdout_frac = 0.30
            _min_each_side = 200
            n_samples = len(X_arr)
            use_final_holdout = (
                bars_meta_raw is not None
                and int(n_samples * (1.0 - _holdout_frac)) >= _min_each_side
                and int(n_samples * _holdout_frac) >= 2 * _min_each_side
            )

            # Embargo (López de Prado): an extra purge buffer beyond label
            # overlap, absorbing serial-correlation / delayed-reaction
            # leakage between the train tail and the test/holdout head.
            # Sized as a fraction of the data's calendar span; bars_meta_raw
            # is chronologically sorted so index 0/-1 are the span ends.
            _embargo_days = 0
            _embargo_frac = (
                float(getattr(getattr(self._config, "retraining", None),
                              "cv_embargo_frac", 0.0) or 0.0)
                if self._config is not None else 0.0
            )
            if _embargo_frac > 0 and bars_meta_raw:
                from datetime import date as _edate

                def _span_date(i: int) -> "_edate | None":
                    try:
                        return _edate.fromisoformat(
                            str(bars_meta_raw[i].get("entry_date", ""))[:10]
                        )
                    except (ValueError, TypeError, IndexError, AttributeError):
                        return None

                _first_d = _span_date(0)
                _last_d = _span_date(len(bars_meta_raw) - 1)
                if _first_d and _last_d and _last_d > _first_d:
                    _embargo_days = int((_last_d - _first_d).days * _embargo_frac)

            for train_idx, test_idx in tscv.split(X_arr):
                if use_final_holdout:
                    break
                # Purge: drop train samples whose label window overlaps
                # the test fold. A train sample at date d has a label
                # computed from bars up to ~d + lookahead trading days;
                # if that reaches the test fold's date range the label
                # peeked at test-period data → leakage. Samples are a
                # cross-sectional panel (many symbols per day) so a
                # fixed sample-count gap can't express a day-gap — we
                # purge by date explicitly. Over-purge slightly
                # (calendar-day conversion of trading days) rather than
                # risk leaving any overlap.
                if (
                    lookahead_bars > 0
                    and bars_meta_raw is not None
                    and len(test_idx) > 0
                ):
                    from datetime import date as _date
                    from datetime import timedelta as _td

                    def _meta_date(i: int) -> "_date | None":
                        raw = bars_meta_raw[int(i)].get("entry_date", "")
                        try:
                            return _date.fromisoformat(str(raw)[:10])
                        except (ValueError, TypeError):
                            return None

                    test_dates = [d for d in (_meta_date(i) for i in test_idx) if d]
                    if test_dates:
                        test_min = min(test_dates)
                        # trading days → calendar days (×7/5) + slack + embargo
                        purge_calendar_days = (
                            int(lookahead_bars * 7 / 5) + 2 + _embargo_days
                        )
                        cutoff = test_min - _td(days=purge_calendar_days)
                        kept = [
                            i for i in train_idx
                            if (_meta_date(i) is None or _meta_date(i) < cutoff)  # type: ignore[operator]  # None short-circuits
                        ]
                        purged = len(train_idx) - len(kept)
                        if kept and purged > 0:
                            train_idx = np.asarray(kept, dtype=train_idx.dtype)
                            logger.debug(
                                "CV purge: dropped %d train samples within "
                                "%dd of test fold start %s",
                                purged, purge_calendar_days, test_min,
                            )

                X_train, X_test = X_arr[train_idx], X_arr[test_idx]
                y_train, y_test = y_arr[train_idx], y_arr[test_idx]
                w_train = weights_arr[train_idx] if weights_arr is not None else None

                fold_model = xgb.XGBClassifier(**xgb_params)
                fold_model.fit(X_train, y_train, sample_weight=w_train, verbose=False)

                preds = fold_model.predict(X_test)

                if bars_meta_raw is not None:
                    fold_probas = fold_model.predict_proba(X_test)
                    for pred, idx, probs in zip(
                        preds, test_idx, fold_probas, strict=False,
                    ):
                        meta = bars_meta_raw[int(idx)]
                        collected_preds.append(int(pred))
                        collected_probas.append([float(p) for p in probs])
                        collected_meta.append(BarMeta(
                            symbol=str(meta.get("symbol", "")),
                            entry_close=float(meta.get("entry_close") or 0.0),
                            exit_close=float(meta.get("exit_close") or 0.0),
                            path_highs=list(meta.get("path_highs") or []),
                            path_lows=list(meta.get("path_lows") or []),
                            target_pct=float(meta.get("target_pct") or 0.0),
                            sl_pct=float(meta.get("sl_pct") or 0.0),
                            entry_date=str(meta.get("entry_date") or ""),
                            buy_exit=meta.get("buy_exit"),
                            sell_exit=meta.get("sell_exit"),
                            hold_days=meta.get("hold_days"),
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
            # Early stopping: a fixed n_estimators either over-fits (too
            # many trees on noise) or under-fits. Probe the right tree count
            # on a purged chronological validation TAIL, then refit the
            # deployed model on ALL data at that count — so it still sees the
            # full history but stops boosting where validation logloss
            # plateaus. Gated on a min sample count; small corpora keep the
            # configured n_estimators.
            n_est_final = xgb_params["n_estimators"]
            if _es_rounds > 0 and n_samples >= _es_min_samples:
                _es_cut = int(n_samples * 0.85)
                _es_train_end = (
                    _purge_boundary(
                        bars_meta_raw, _es_cut, lookahead_bars,
                        min_keep=max(50, int(n_samples * 0.4)),
                        embargo_days=_embargo_days,
                    )
                    if bars_meta_raw is not None else _es_cut
                )
                if _es_train_end > 50 and (n_samples - _es_cut) >= 50:
                    # n_estimators is the UPPER BOUND; early stopping picks
                    # the best count <= it on the validation tail.
                    _es_ceiling = int(xgb_params["n_estimators"])
                    _probe = xgb.XGBClassifier(
                        **{**xgb_params, "n_estimators": _es_ceiling,
                           "early_stopping_rounds": _es_rounds}
                    )
                    _w_es = (
                        weights_arr[:_es_train_end] if weights_arr is not None else None
                    )
                    _probe.fit(
                        X_arr[:_es_train_end], y_arr[:_es_train_end],
                        sample_weight=_w_es,
                        eval_set=[(X_arr[_es_cut:], y_arr[_es_cut:])],
                        verbose=False,
                    )
                    _bi = getattr(_probe, "best_iteration", None)
                    if _bi is not None and 0 < int(_bi) + 1 < _es_ceiling:
                        n_est_final = int(_bi) + 1
                        logger.info(
                            "Early stopping: n_estimators %d → %d (%d rounds)",
                            _es_ceiling, n_est_final, _es_rounds,
                        )
                    del _probe
                    _gc.collect()
            model.set_params(n_estimators=n_est_final)
            # Final model trained on all data (with sample weights if available)
            model.fit(X_arr, y_arr, sample_weight=weights_arr, verbose=False)

            # Calibrate probabilities (Platt scaling). cv MUST be a
            # TimeSeriesSplit — passing an int makes sklearn default to
            # StratifiedKFold, which shuffles. Shuffled CV leaks future
            # data into past calibration on time-series, silently
            # corrupting every downstream probability the system reads.
            calibration_n_splits = min(3, len(y_arr) // 50 or 2)
            calibrator = CalibratedClassifierCV(
                model,
                method="sigmoid",
                cv=TimeSeriesSplit(n_splits=max(2, calibration_n_splits)),
            )
            calibrator.fit(X_arr, y_arr)

            if bars_meta_raw is not None and (use_final_holdout or collected_preds):
                # Real-PnL backtest path
                bt_cfg = BacktestConfig(
                    product=backtest_product,
                    max_concurrent_positions=backtest_max_positions,
                )

                # Bound the sweep to the range production can actually
                # trade. The inference layer (_get_effective_thresholds)
                # clamps tuned thresholds to risk.tuned_threshold_max_value
                # / _max_diff; without mirroring those caps here the sweep
                # can pick a cell (e.g. 0.75/0.80) the live model clamps to
                # 0.60/0.60, so the reported Sharpe/win-rate describe a
                # model that never trades. Caps None when config is absent
                # (tests) → unbounded, preserving prior behaviour.
                _risk_cfg = getattr(self._config, "risk", None) if self._config else None
                _sweep_max_value = (
                    float(getattr(_risk_cfg, "tuned_threshold_max_value", 0.60))
                    if _risk_cfg is not None else None
                )
                _sweep_max_diff = (
                    float(getattr(_risk_cfg, "tuned_threshold_max_diff", 0.05))
                    if _risk_cfg is not None else None
                )
                # Signal-rate floor so the sweep can't pick an unreachable
                # ceiling cell that fires ~never live (the silent-model bug).
                _sweep_min_signal_rate = (
                    float(getattr(_risk_cfg, "tuned_min_signal_rate", 0.0))
                    if _risk_cfg is not None else 0.0
                )

                # Deflated Sharpe of the chosen cell — captured from the
                # sweep (which holds the full grid of trial Sharpes) so the
                # reported metric corrects for selection across the 81-cell
                # search, which the per-cell bootstrap lower bound can't.
                _dsr: float | None = None
                # Out-of-sample discrimination diagnostics — the cleanest
                # threshold/cost-independent read of whether the model has
                # ANY edge (AUC≈0.50 / separation≈0 = none). Hoisted so they
                # persist into `metrics` instead of living only in the log.
                _oos_auc_buy: float | None = None
                _oos_auc_sell: float | None = None
                _oos_logloss: float | None = None
                _oos_buy_sep: float | None = None
                if use_final_holdout:
                    # Final-scale holdout. Train a tuning model on the
                    # chronological early data only, score the strict-
                    # future holdout with it (out-of-sample, at ~the
                    # deployed full-data model's probability scale), then
                    # tune thresholds on the FIRST half of that holdout
                    # and report metrics on the SECOND half (keeps the
                    # threshold choice out of the reported slice). A purge
                    # gap before the holdout stops the tuning model's
                    # labels peeking into it. This is what makes the tuned
                    # thresholds reachable at inference — the per-fold OOF
                    # probabilities are from over-confident sub-models and
                    # don't transfer to the deployed model.
                    _cut = int(n_samples * (1.0 - _holdout_frac))
                    _purge_cut = _purge_boundary(
                        bars_meta_raw, _cut, lookahead_bars, _min_each_side,
                        embargo_days=_embargo_days,
                    )
                    _tw = weights_arr[:_purge_cut] if weights_arr is not None else None
                    _tuning_model = xgb.XGBClassifier(**xgb_params)
                    _tuning_model.fit(
                        X_arr[:_purge_cut], y_arr[:_purge_cut],
                        sample_weight=_tw, verbose=False,
                    )
                    _ho_proba = _tuning_model.predict_proba(X_arr[_cut:])
                    del _tuning_model
                    _gc.collect()
                    _ho_probas = [[float(p) for p in row] for row in _ho_proba]
                    _ho_preds = [int(row.argmax()) for row in _ho_proba]
                    # Discrimination diagnostics on the strict-future
                    # holdout. The tuning model trained only on
                    # X_arr[:_purge_cut] and scored X_arr[_cut:], so this is
                    # a clean out-of-sample read of whether the model can
                    # RANK winners above losers at all — independent of any
                    # threshold. AUC≈0.50 = no edge (no cutoff/label tweak
                    # fixes that); >0.55 = a real signal worth tuning.
                    # BUY-separation is mean P(BUY) on true-BUY rows minus
                    # the rest: a prior-predictor leaves it ~0. Logged only;
                    # never gates deploy.
                    try:
                        from sklearn.metrics import (
                            log_loss as _log_loss,
                        )
                        from sklearn.metrics import (
                            roc_auc_score as _roc_auc,
                        )
                        _y_ho = y_arr[_cut:]
                        _p_buy = _ho_proba[:, _LABEL_BUY]
                        _p_sell = _ho_proba[:, _LABEL_SELL]

                        def _ovr_auc(pos: int, score: Any) -> float:
                            _yb = (_y_ho == pos).astype(int)
                            if _yb.min() == _yb.max():
                                return float("nan")
                            return float(_roc_auc(_yb, score))

                        _auc_buy = _ovr_auc(_LABEL_BUY, _p_buy)
                        _auc_sell = _ovr_auc(_LABEL_SELL, _p_sell)
                        try:
                            _ll = float(
                                _log_loss(_y_ho, _ho_proba, labels=[0, 1, 2])
                            )
                        except Exception:
                            _ll = float("nan")
                        _m_buy = _y_ho == _LABEL_BUY
                        _sep = (
                            float(_p_buy[_m_buy].mean() - _p_buy[~_m_buy].mean())
                            if _m_buy.any() and (~_m_buy).any()
                            else float("nan")
                        )
                        _q = np.percentile(_p_buy, [50, 90, 99])

                        def _finite(x: float) -> float | None:
                            # NaN (absent class) → None so metrics stay
                            # JSON-serializable for the dashboard.
                            return None if x != x else round(float(x), 4)

                        _oos_auc_buy = _finite(_auc_buy)
                        _oos_auc_sell = _finite(_auc_sell)
                        _oos_logloss = _finite(_ll)
                        _oos_buy_sep = _finite(_sep)
                        logger.info(
                            "Discrimination %s (holdout n=%d): AUC_buy=%.3f "
                            "AUC_sell=%.3f logloss=%.3f | P(BUY) mean=%.3f "
                            "std=%.3f p50=%.3f p90=%.3f p99=%.3f max=%.3f | "
                            "BUY-separation=%.3f",
                            model_type, len(_y_ho), _auc_buy, _auc_sell, _ll,
                            float(_p_buy.mean()), float(_p_buy.std()),
                            float(_q[0]), float(_q[1]), float(_q[2]),
                            float(_p_buy.max()), _sep,
                        )
                    except Exception:
                        logger.debug(
                            "discrimination diagnostics failed", exc_info=True,
                        )
                    _ho_meta = [
                        BarMeta(
                            symbol=str(m.get("symbol", "")),
                            entry_close=float(m.get("entry_close") or 0.0),
                            exit_close=float(m.get("exit_close") or 0.0),
                            path_highs=list(m.get("path_highs") or []),
                            path_lows=list(m.get("path_lows") or []),
                            target_pct=float(m.get("target_pct") or 0.0),
                            sl_pct=float(m.get("sl_pct") or 0.0),
                            entry_date=str(m.get("entry_date") or ""),
                            buy_exit=m.get("buy_exit"),
                            sell_exit=m.get("sell_exit"),
                            hold_days=m.get("hold_days"),
                        )
                        for m in bars_meta_raw[_cut:]
                    ]
                    _sub = len(_ho_probas) // 2
                    tuned_buy, tuned_sell, _tune_bt = sweep_thresholds(
                        probas=_ho_probas[:_sub],
                        bars_meta=_ho_meta[:_sub],
                        config=bt_cfg,
                        max_threshold=_sweep_max_value,
                        max_diff=_sweep_max_diff,
                        min_signal_rate=_sweep_min_signal_rate,
                    )
                    _dsr = _tune_bt.deflated_sharpe
                    _ht_preds = _apply_thresholds(
                        _ho_probas[_sub:], tuned_buy, tuned_sell,
                    )
                    tuned_bt = run_walk_forward_backtest(
                        preds=_ht_preds,
                        bars_meta=_ho_meta[_sub:],
                        config=bt_cfg,
                    )
                    bt = run_walk_forward_backtest(
                        preds=_ho_preds[_sub:],
                        bars_meta=_ho_meta[_sub:],
                        config=bt_cfg,
                    )
                    _holdout_used = True
                else:
                    # Small bars_meta corpus (not enough for the holdout):
                    # fall back to the K-fold OOF collection. Tune on the
                    # chronological first slice, report on the strict-
                    # future slice; if too small to split, tune in-sample
                    # (flagged via threshold_holdout_used).
                    _split = int(len(collected_preds) * (1.0 - _holdout_frac))
                    _can_split = (
                        _split >= _min_each_side
                        and (len(collected_preds) - _split) >= _min_each_side
                    )
                    if _can_split:
                        tuned_buy, tuned_sell, _tune_bt = sweep_thresholds(
                            probas=collected_probas[:_split],
                            bars_meta=collected_meta[:_split],
                            config=bt_cfg,
                            max_threshold=_sweep_max_value,
                            max_diff=_sweep_max_diff,
                            min_signal_rate=_sweep_min_signal_rate,
                        )
                        _dsr = _tune_bt.deflated_sharpe
                        _holdout_tuned_preds = _apply_thresholds(
                            collected_probas[_split:], tuned_buy, tuned_sell,
                        )
                        tuned_bt = run_walk_forward_backtest(
                            preds=_holdout_tuned_preds,
                            bars_meta=collected_meta[_split:],
                            config=bt_cfg,
                        )
                        bt = run_walk_forward_backtest(
                            preds=collected_preds[_split:],
                            bars_meta=collected_meta[_split:],
                            config=bt_cfg,
                        )
                        _holdout_used = True
                    else:
                        bt = run_walk_forward_backtest(
                            preds=collected_preds,
                            bars_meta=collected_meta,
                            config=bt_cfg,
                        )
                        tuned_buy, tuned_sell, tuned_bt = sweep_thresholds(
                            probas=collected_probas,
                            bars_meta=collected_meta,
                            config=bt_cfg,
                            max_threshold=_sweep_max_value,
                            max_diff=_sweep_max_diff,
                            min_signal_rate=_sweep_min_signal_rate,
                        )
                        _dsr = tuned_bt.deflated_sharpe
                        _holdout_used = False
                # When tuned thresholds beat the argmax baseline, report
                # the tuned metrics as the headline numbers — that's what
                # live trading will actually see. Keep the argmax sharpe
                # accessible for comparison.
                use_tuned = tuned_bt.sharpe > bt.sharpe
                headline = tuned_bt if use_tuned else bt
                # Robust decision Sharpe: bootstrap the SAME daily-return
                # series the headline point Sharpe is computed from, and
                # take its p25 lower bound. The headline is a point
                # estimate on a single contiguous holdout slice — high
                # variance and regime-dependent — so deploy/promote
                # decisions compare on this lower bound instead (a Sharpe
                # propped up by a couple of lucky days collapses here,
                # while a consistent edge survives).
                _boot_series = headline.daily_returns or headline.returns
                _boot_annual = 252 if headline.daily_returns else bt_cfg.annualization_factor
                sharpe_lower = _bootstrap_sharpe_lower_bound(
                    _boot_series, annualization=_boot_annual,
                    n_iter=200, percentile=25.0,
                ) if _boot_series else headline.sharpe
                metrics: dict[str, Any] = {
                    "sharpe": headline.sharpe,
                    "sharpe_lower": sharpe_lower,
                    "max_drawdown_pct": headline.max_drawdown_pct,
                    "win_rate": headline.win_rate,
                    "profit_factor": (
                        headline.profit_factor if headline.profit_factor != float("inf")
                        else 999.0
                    ),
                    "total_trades": headline.total_trades,
                    "total_samples": len(y_arr),
                    # Extra real-PnL fields not produced by the legacy
                    # synthetic path — useful on the ML Models dashboard.
                    "net_pnl": headline.net_pnl,
                    "final_capital": headline.final_capital,
                    "backtest_source": (
                        "walk_forward_threshold_tuned" if use_tuned
                        else "walk_forward_real_pnl"
                    ),
                    "signals_skipped_at_cap": headline.signals_skipped_at_cap,
                    "backtest_max_positions": backtest_max_positions,
                    # Tuned thresholds: applied at inference when present.
                    "tuned_buy_threshold": tuned_buy,
                    "tuned_sell_threshold": tuned_sell,
                    "argmax_sharpe": bt.sharpe,
                    "tuned_sharpe": tuned_bt.sharpe,
                    "threshold_holdout_used": _holdout_used,
                    # Selection-bias-adjusted confidence the chosen cell's
                    # edge is real (P(true Sharpe > 0) after correcting for
                    # the number of grid trials + return skew/kurtosis).
                    # None when there weren't enough trials to estimate it.
                    "deflated_sharpe": _dsr,
                    # Threshold/cost-independent discrimination on the
                    # strict-future holdout — AUC≈0.50 / separation≈0 means
                    # the model has no edge no matter how thresholds are
                    # tuned. None on the small-corpus (non-holdout) path.
                    "oos_auc_buy": _oos_auc_buy,
                    "oos_auc_sell": _oos_auc_sell,
                    "oos_logloss": _oos_logloss,
                    "oos_buy_separation": _oos_buy_sep,
                }
                if _dsr is not None and _dsr < 0.95:
                    logger.warning(
                        "%s: deflated Sharpe %.3f < 0.95 — the tuned edge may "
                        "be a selection-bias artifact of the threshold sweep, "
                        "not a real signal.",
                        model_type, _dsr,
                    )
                # Per-calendar-year OOS edge profile at the DEPLOYED
                # thresholds — diagnoses regime shift vs edge decay. We
                # apply the single chosen (buy, sell) cutoff across the
                # whole holdout and bucket realized trades by entry year,
                # so a Sharpe that's positive in older years and negative
                # only recently reads as regime/decay rather than "never
                # worked". Diagnostic only (logged + stashed in metrics);
                # never feeds the deploy/promote decision.
                try:
                    _bp_probas = _ho_probas if use_final_holdout else collected_probas
                    _bp_meta = _ho_meta if use_final_holdout else collected_meta
                    _bp_preds = _apply_thresholds(_bp_probas, tuned_buy, tuned_sell)
                    _by_year: dict[str, Any] = {}
                    for _yr, _res in backtest_by_period(
                        _bp_preds, _bp_meta, bt_cfg,
                    ).items():
                        _by_year[_yr] = {
                            "sharpe": round(_res.sharpe, 3),
                            "win_rate": round(_res.win_rate, 3),
                            "trades": _res.total_trades,
                            "net_pnl": round(_res.net_pnl, 1),
                        }
                        logger.info(
                            "  [%s] %s OOS @ tuned: sharpe=%.2f win=%.2f trades=%d net=%.0f",
                            model_type, _yr, _res.sharpe, _res.win_rate,
                            _res.total_trades, _res.net_pnl,
                        )
                    metrics["per_year_oos"] = _by_year
                except Exception:
                    logger.debug("per-year OOS breakdown failed", exc_info=True)
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
                    # No bootstrap on the synthetic legacy path — mirror
                    # the point Sharpe so the decision metric is always
                    # present for downstream comparisons.
                    "sharpe_lower": round(sharpe, 4),
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

        # DEPLOY tuned thresholds only when the tuned variant actually beat
        # the argmax baseline on the holdout (signalled by backtest_source
        # == "walk_forward_threshold_tuned" — set iff use_tuned). When
        # argmax won, the headline metrics describe argmax, so the deployed
        # model must run argmax too: applying cutoffs the backtest judged
        # WORSE both underperforms and makes the saved Sharpe misrepresent
        # live behaviour. The swept values stay in `metrics` for visibility
        # regardless; this only gates what the live model actually uses.
        tuned_buy = metrics.get("tuned_buy_threshold")
        tuned_sell = metrics.get("tuned_sell_threshold")
        tuning_won = metrics.get("backtest_source") == "walk_forward_threshold_tuned"
        if tuning_won and tuned_buy is not None and tuned_sell is not None:
            self._set_thresholds(
                model_type, {"buy": float(tuned_buy), "sell": float(tuned_sell)},
            )
            logger.info(
                "Tuned %s thresholds DEPLOYED: buy=%.2f sell=%.2f "
                "(tuned_sharpe=%.4f > argmax_sharpe=%.4f)",
                model_type, tuned_buy, tuned_sell,
                metrics.get("tuned_sharpe", 0.0),
                metrics.get("argmax_sharpe", 0.0),
            )
        else:
            # Argmax won (or no sweep) → deploy argmax, clearing any cutoffs.
            self._set_thresholds(model_type, None)
            logger.info(
                "%s deploying ARGMAX (tuned sweep did not beat argmax: "
                "argmax_sharpe=%.4f >= tuned_sharpe=%.4f); swept cutoffs "
                "%s/%s kept in metrics for reference only",
                model_type, metrics.get("argmax_sharpe", 0.0),
                metrics.get("tuned_sharpe", 0.0), tuned_buy, tuned_sell,
            )

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

            from yolovest.data.features import MODEL_SCHEMA_VERSION

            feature_names = (self._intraday_features if model_type == "intraday"
                             else self._swing_features)
            artifact = {
                "model": model,
                "calibrator": self._get_calibrator(model_type),
                "version": version_str,
                "metrics": metrics,
                "feature_names": feature_names,
                "tuned_thresholds": self._get_thresholds(model_type),
                "saved_at": datetime.now(UTC).isoformat(),
                # Compatibility stamps — checked on cross-machine import so
                # a model trained against different code fails loudly.
                "schema_version": MODEL_SCHEMA_VERSION,
                "xgboost_version": _lib_version("xgboost"),
                "sklearn_version": _lib_version("scikit-learn"),
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
        _warn_on_lib_skew(artifact, f"load_model[{model_type}]")

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

        # Restore tuned thresholds when present. Legacy artifacts without
        # this key get None → _predict falls back to argmax.
        tuned = artifact.get("tuned_thresholds")
        if tuned and isinstance(tuned, dict):
            self._set_thresholds(model_type, {
                "buy": float(tuned.get("buy", 0.5)),
                "sell": float(tuned.get("sell", 0.5)),
            })
        else:
            self._set_thresholds(model_type, None)

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
