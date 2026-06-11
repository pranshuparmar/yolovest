"""Per-symbol signal evaluation — the single source of truth.

Used by BOTH the production heartbeat (``GenerateSignalsSkill.execute``)
and the dashboard's dry-run preview endpoint (``/api/dry-run``). Pure
compute: no DB writes, no event emissions, no order placement. The
caller owns dedup, persistence, risk-check, LLM review, etc.

Anything that decides "given these features, what would we trade?"
lives here. Anything that decides "should we even consider this
symbol right now?" (dedup, cooldown, locked, recently-traded) is
upstream of the call. Anything that decides "given this signal,
should we execute?" (risk gates, LLM review) is downstream.

The point: ANY divergence between heartbeat and dry-run is a bug.
Adding a new check (e.g. an additional eligibility filter) goes
here so both code paths get it. Tweaking gate behaviour goes
here so both reflect the change.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, Literal

from yolovest.data.features import (
    IndicatorConfig,
    compute_features,
    compute_session_features,
)
from yolovest.strategy.holding_period import (
    adjust_sell_for_holdings,
    apply_session_caps,
    decide_holding_period,
    interpolate_atr_multipliers,
    interpolate_atr_pct_cap,
)
from yolovest.timezone import IST

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.models.schemas import MLPrediction


# Intraday inference window — mirrors model_retrain's intraday window_size
# (200 five-min bars, the longest EMA period) so the live feature window
# matches the one the intraday model trained on. Fetch a few extra sessions
# of 5-min bars so holidays/half-days can't leave us short of the window.
_INTRADAY_WINDOW_SIZE = 200
_INTRADAY_INFERENCE_DAYS = 6


def _intraday_indicator_cfg(ctx: AppContext) -> IndicatorConfig:
    """IndicatorConfig from the live strategy config — same fields
    generate_signals + model_retrain build from, so the 5-min features
    computed here match the intraday model's training set."""
    s = ctx.config.strategy
    return IndicatorConfig(
        ema_periods=s.ema_periods,
        rsi=s.indicators.rsi,
        macd=s.indicators.macd,
        bollinger_bands=s.indicators.bollinger_bands,
        vwap=s.indicators.vwap,
        atr=s.indicators.atr,
        volume_profile=s.indicators.volume_profile,
        obv=s.indicators.obv,
        supertrend=s.indicators.supertrend,
        # 5-min intraday features → extended (daily-horizon) momentum off.
        extended_momentum=False,
    )

logger = logging.getLogger(__name__)


def _capped_days_range(
    days_range: tuple[int, int] | None, cap: int,
) -> tuple[int, int] | None:
    """Clamp a (lo, hi) holding-days range to the swing horizon cap
    (`strategy.swing_horizon_cap_days`). The swing model's label measures
    a ~10-bar window; horizons far beyond it ride an edge the label never
    measured. cap <= 0 disables."""
    if not days_range or cap <= 0:
        return days_range
    lo, hi = days_range
    return (min(lo, cap), min(hi, cap))


OutcomeT = Literal[
    "passed",                    # signal cleared all checks
    "hold_signal",               # model said HOLD
    "low_confidence",            # below per-mode min_confidence floor
    "locked_holding",            # SELL on a user-locked symbol
    "sell_on_holding",           # SELL skipped — position-monitor owns exits
    "short_on_swing_horizon",    # non-held SELL with swing decision
    "intraday_cutoff",           # intraday signal after configured cutoff
    "intraday_atr_ineligible",   # ATR exceeds intraday eligibility cap
    "implausible_atr",           # ATR% above sanity ceiling — corrupt OHLCV
]


@dataclass
class SignalEvaluation:
    """Outcome of evaluating one symbol's features through the chooser,
    threshold gate, and post-prediction adjustments.

    The struct is uniform regardless of outcome: every field is set,
    even when the signal is rejected. `outcome != "passed"` means the
    signal didn't survive — the prediction / prices are still useful
    for diagnostics.
    """
    symbol: str
    outcome: OutcomeT
    detail: str
    # Prediction-derived (None only when model failed before producing anything)
    prediction: MLPrediction | None
    signal_type: Literal["BUY", "SELL", "HOLD"]
    holding_period: str       # "intraday" / "swing" / "positional" / "long_term"
    product: str              # "MIS" / "CNC"
    expected_days: int
    entry_price: float
    target_price: float
    stop_loss_price: float
    confidence: float
    effective_min_confidence: float | None
    model_version: str | None
    class_probabilities: dict[str, float] | None = field(default=None)


async def _predict_with_chooser(
    ctx: AppContext,
    symbol: str,
    features: dict[str, Any],
    *,
    current_price: float | None,
    effective_mode: str,
    intraday_features: dict[str, Any] | None,
    holding_period: str,
    product: str,
    expected_days: int,
    now_time: time,
) -> tuple[Any, str, str, int]:
    """Run the right model(s) for the effective mode and return
    (prediction, holding_period, product, expected_days).

    For ``balanced`` mode this runs intraday + swing concurrently and
    picks the model with the higher margin-above-effective-threshold.
    Margin (not raw confidence) is the right comparator because the
    swing model is trained on a HOLD-dominated label distribution
    (~73% HOLD) while intraday is balanced (~43% HOLD) — raw
    confidence systematically favours intraday for the wrong reason.
    """
    is_balanced = effective_mode == "balanced"
    use_intraday = holding_period == "intraday"

    if is_balanced:
        # Check intraday cutoff — past cutoff, skip the intraday model
        cutoff_str = ctx.config.market_hours.intraday_cutoff
        cutoff_parts = cutoff_str.split(":")
        past_cutoff = now_time >= time(int(cutoff_parts[0]), int(cutoff_parts[1]))

        if past_cutoff:
            try:
                swing_pred = await ctx.ml.predict_swing(
                    symbol, features, current_price=current_price,
                )
            except Exception as e:
                logger.debug("Balanced: swing model failed for %s: %s", symbol, e)
                swing_pred = None
            if swing_pred and swing_pred.signal_type != "HOLD":
                from yolovest.config import _MODE_HOLDING_DAYS
                mode_days = _MODE_HOLDING_DAYS.get("balanced", (0, 15))
                _, product, expected_days = decide_holding_period(
                    features, ["short_term", "long_term"],
                    ctx.config.strategy.volatility, now_time,
                    mode_days_range=_capped_days_range(
                        (max(1, mode_days[0]), mode_days[1]),
                        ctx.config.strategy.swing_horizon_cap_days,
                    ),
                )
                label = (
                    "swing" if expected_days <= 5
                    else "positional" if expected_days <= 15
                    else "long_term"
                )
                return swing_pred, label, "CNC", expected_days
            return swing_pred, holding_period, product, expected_days

        intra_feat = intraday_features or features
        intra_pred, swing_pred = await asyncio.gather(
            ctx.ml.predict_intraday(symbol, intra_feat, current_price=current_price),
            ctx.ml.predict_swing(symbol, features, current_price=current_price),
            return_exceptions=True,
        )
        if isinstance(intra_pred, BaseException):
            logger.debug("Balanced: intraday model failed for %s: %s", symbol, intra_pred)
            intra_pred = None
        if isinstance(swing_pred, BaseException):
            logger.debug("Balanced: swing model failed for %s: %s", symbol, swing_pred)
            swing_pred = None

        def _margin(pred: Any, model_type: str) -> float:
            if pred is None or pred.signal_type == "HOLD":
                return -1.0
            thresholds = ctx.ml.get_effective_thresholds(model_type)
            if not thresholds:
                return float(pred.confidence)
            key = pred.signal_type.lower()
            return float(pred.confidence) - float(thresholds.get(key, 0.5))

        intra_margin = _margin(intra_pred, "intraday")
        swing_margin = _margin(swing_pred, "swing")

        if intra_margin < 0 and swing_margin < 0:
            return swing_pred or intra_pred, holding_period, product, expected_days
        if intra_margin >= swing_margin:
            return intra_pred, "intraday", "MIS", 0

        # Swing wins — recompute expected_days for the swing horizon
        from yolovest.config import _MODE_HOLDING_DAYS
        mode_days = _MODE_HOLDING_DAYS.get("balanced", (0, 15))
        _, product, expected_days = decide_holding_period(
            features, ["short_term", "long_term"],
            ctx.config.strategy.volatility, now_time,
            mode_days_range=_capped_days_range(
                (max(1, mode_days[0]), mode_days[1]),
                ctx.config.strategy.swing_horizon_cap_days,
            ),
        )
        label = (
            "swing" if expected_days <= 5
            else "positional" if expected_days <= 15
            else "long_term"
        )
        return swing_pred, label, "CNC", expected_days

    if use_intraday:
        feat = intraday_features or features
        prediction = await ctx.ml.predict_intraday(
            symbol, feat, current_price=current_price,
        )
    else:
        prediction = await ctx.ml.predict_swing(
            symbol, features, current_price=current_price,
        )
    return prediction, holding_period, product, expected_days


async def _reality_check_intraday_target(
    ctx: AppContext,
    symbol: str,
    signal_type: str,
    target: float,
    stop_loss: float,
) -> tuple[float, float]:
    """Cap intraday target/SL against today's circuit limits via a
    live quote. Failure is non-fatal — original target/SL returned.
    """
    try:
        quote = await ctx.market_data.get_quote(symbol)
    except Exception:
        logger.debug("reality-check: quote fetch failed for %s", symbol, exc_info=True)
        return target, stop_loss
    new_target, new_sl, adjustments = apply_session_caps(
        signal_type, target, stop_loss, quote,
    )
    if adjustments:
        logger.info(
            "reality-check %s %s: %s",
            signal_type, symbol, "; ".join(adjustments),
        )
    return new_target, new_sl


def _format_class_probs(prediction: Any) -> str:
    """Format MLPrediction.class_probabilities for log lines.

    Falls back to a plain confidence read for legacy predictions
    without the class_probabilities field (older shadow models,
    defensive paths).
    """
    probs = getattr(prediction, "class_probabilities", None)
    if probs:
        return " ".join(
            f"{k}={v:.2f}" for k, v in probs.items()
        )
    return f"confidence={prediction.confidence:.2f}"


def _evaluation_with_outcome(
    symbol: str, outcome: OutcomeT, detail: str,
    *, prediction: Any = None,
    holding_period: str = "intraday", product: str = "MIS", expected_days: int = 0,
    entry_price: float = 0.0, target_price: float = 0.0, stop_loss_price: float = 0.0,
    effective_min_confidence: float | None = None,
) -> SignalEvaluation:
    """Build a SignalEvaluation for a non-passed outcome. Fills the
    fields we know about; the rest are sensible defaults.
    """
    if prediction is not None:
        signal_type = prediction.signal_type
        confidence = prediction.confidence
        model_version = prediction.model_version
        class_probabilities = getattr(prediction, "class_probabilities", None)
    else:
        signal_type = "HOLD"
        confidence = 0.0
        model_version = None
        class_probabilities = None
    return SignalEvaluation(
        symbol=symbol,
        outcome=outcome,
        detail=detail,
        prediction=prediction,
        signal_type=signal_type,
        holding_period=holding_period,
        product=product,
        expected_days=expected_days,
        entry_price=entry_price,
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        confidence=confidence,
        effective_min_confidence=effective_min_confidence,
        model_version=model_version,
        class_probabilities=class_probabilities,
    )


async def evaluate_symbol_signal(
    ctx: AppContext,
    symbol: str,
    features: dict[str, Any],
    *,
    current_price: float | None,
    held_symbols: set[str],
    locked_symbols: set[str],
    now_time: time | None = None,
    effective_mode: str | None = None,
    allowed_periods: list[str] | None = None,
    mode_days_range: tuple[int, int] | None = None,
    intraday_features: dict[str, Any] | None = None,
    existing_positions: list[dict[str, Any]] | None = None,
    market_regime: str | None = None,
    bypass_time_gates: bool = False,
) -> SignalEvaluation:
    """Per-symbol signal evaluation — see module docstring.

    Pre-conditions (caller's responsibility):
    - ``features`` has been computed via ``compute_features``
    - ``current_price`` is the latest LTP (or None if fetch failed —
      the prediction layer will fall back to bar close)
    - ``held_symbols`` / ``locked_symbols`` are mode-scoped sets
    - Any dedup / cooldown / recently-traded checks have already run

    ``effective_mode`` overrides ``ctx.config.strategy.mode`` for this
    call (used by the dry-run endpoint's mode override query param).
    None means use the live config.

    ``allowed_periods`` / ``mode_days_range`` override the
    config-derived defaults — supply both or neither.

    ``market_regime`` and ``existing_positions`` flow into
    ``decide_holding_period`` for production-only knobs; the dry-run
    can pass ``None`` and accept the same default behaviour.

    Returns a ``SignalEvaluation`` with ``outcome`` indicating
    pass/reject reason. The caller logs / persists / dedups based on
    the outcome — this function only computes.
    """
    cfg = ctx.config
    if now_time is None:
        now_time = datetime.now(IST).time()
    if bypass_time_gates:
        # Dry-run / preview: evaluate as if at session open so the
        # time-of-day EXECUTION gates don't suppress signals the model
        # would genuinely produce earlier in the day. Those gates exist
        # to stop the live engine OPENING positions too late to manage
        # (intraday cutoff, _is_intraday_viable's 14:30 cap, balanced-
        # mode swing-only-after-cutoff) — they're not model-quality
        # gates, so a preview run at 16:00 should still surface the
        # intraday signals that a 09:30 heartbeat would have. Pinning
        # now_time to market_hours.open makes all three downstream
        # checks read "early session".
        try:
            oh, om = cfg.market_hours.open.split(":")
            now_time = time(int(oh), int(om))
        except Exception:
            now_time = time(9, 15)
    if effective_mode is None:
        effective_mode = cfg.strategy.mode
    if allowed_periods is None:
        from yolovest.config import _MODE_HOLDING_PERIODS
        allowed_periods = _MODE_HOLDING_PERIODS.get(
            effective_mode,
            cfg.strategy.allowed_holding_periods
                or ["intraday", "short_term", "long_term"],
        )
    if mode_days_range is None:
        from yolovest.config import _MODE_HOLDING_DAYS
        mode_days_range = _MODE_HOLDING_DAYS.get(effective_mode)
    mode_days_range = _capped_days_range(
        mode_days_range, cfg.strategy.swing_horizon_cap_days,
    )

    # Step 1: decide holding period from features + mode
    bear_max = None
    if cfg.strategy.market_regime.enabled:
        bear_max = cfg.strategy.market_regime.bear_max_holding_days
    holding_period, product, expected_days = decide_holding_period(
        features, allowed_periods, cfg.strategy.volatility, now_time,
        mode_days_range=mode_days_range,
        existing_positions=existing_positions,
        market_regime=market_regime,
        bear_max_holding_days=bear_max,
    )

    # Step 2: intraday ATR-eligibility cap. A 5%+ daily ATR can't be
    # squared off reliably in a half-day session.
    if holding_period == "intraday":
        elig_cap = float(getattr(
            cfg.strategy.volatility,
            "max_atr_pct_for_intraday_eligibility",
            0.0,
        ))
        sym_atr_pct = float(features.get("atr_pct", 0.0))
        if elig_cap > 0 and sym_atr_pct > elig_cap:
            return _evaluation_with_outcome(
                symbol, "intraday_atr_ineligible",
                (
                    f"atr_pct {sym_atr_pct * 100:.2f}% > "
                    f"intraday eligibility cap {elig_cap * 100:.2f}% — "
                    f"stock too volatile to square off in a half-day session"
                ),
                holding_period=holding_period, product=product,
                expected_days=expected_days,
            )

    # Build intraday features when the chooser will need them (pure
    # intraday mode or balanced mode). The intraday model trains on
    # features computed over a 200-bar 5-min window, so inference must do
    # the same — feeding it the daily-derived `features` would be a
    # train/serve mismatch. The broadcast features (news/vix/fno/sector/
    # institutional) are per-symbol-per-day and identical across bar
    # intervals, so we overlay the freshly-computed 5-min technicals (and
    # the time-of-day features compute_features derives from the last bar)
    # onto the already-enriched daily dict rather than recomputing them.
    is_balanced = effective_mode == "balanced"
    use_intraday = holding_period == "intraday"
    if intraday_features is None and (use_intraday or is_balanced):
        try:
            intraday_bars = await ctx.db.get_ohlcv(
                symbol, "5minute", days=_INTRADAY_INFERENCE_DAYS,
            )
            if len(intraday_bars) >= _INTRADAY_WINDOW_SIZE:
                window = intraday_bars[-(_INTRADAY_WINDOW_SIZE + 1):]
                tech = compute_features(window, _intraday_indicator_cfg(ctx))
                if tech:
                    tech.update(compute_session_features(window))
                    intraday_features = {**features, **tech}
        except Exception:
            logger.debug(
                "intraday-feature build failed for %s", symbol, exc_info=True,
            )

    # Step 3: run the model(s) via the right chooser
    prediction, holding_period, product, expected_days = await _predict_with_chooser(
        ctx, symbol, features,
        current_price=current_price,
        effective_mode=effective_mode,
        intraday_features=intraday_features,
        holding_period=holding_period,
        product=product,
        expected_days=expected_days,
        now_time=now_time,
    )

    if prediction is None:
        return _evaluation_with_outcome(
            symbol, "hold_signal", "Model returned no prediction",
            holding_period=holding_period, product=product,
            expected_days=expected_days,
        )

    # Step 4: HOLD check
    if prediction.signal_type == "HOLD":
        logger.info("HOLD signal for %s (%s)", symbol, _format_class_probs(prediction))
        return _evaluation_with_outcome(
            symbol, "hold_signal",
            f"HOLD @ confidence {prediction.confidence:.2f}",
            prediction=prediction,
            holding_period=holding_period, product=product,
            expected_days=expected_days,
        )

    # Step 5: locked-holding SELL skip
    if prediction.signal_type == "SELL" and symbol in locked_symbols:
        logger.info("Locked holding: skipping SELL for %s", symbol)
        return _evaluation_with_outcome(
            symbol, "locked_holding",
            f"SELL blocked — {symbol} is locked",
            prediction=prediction,
            holding_period=holding_period, product=product,
            expected_days=expected_days,
        )

    # Step 6: held-symbol SELL skip (position-monitor owns exits)
    if (
        cfg.risk.skip_sell_on_holdings
        and prediction.signal_type == "SELL"
        and symbol in held_symbols
    ):
        logger.info(
            "Held symbol: skipping SELL for %s (monitor owns exits)", symbol,
        )
        return _evaluation_with_outcome(
            symbol, "sell_on_holding",
            f"SELL skipped — position-monitor handles exit for {symbol}",
            prediction=prediction,
            holding_period=holding_period, product=product,
            expected_days=expected_days,
        )

    # Step 7: adjust SELL for non-held → MIS short (or drop if swing)
    adjusted = adjust_sell_for_holdings(
        prediction.signal_type, holding_period, product,
        symbol, held_symbols, expected_days,
    )
    if adjusted is None:
        logger.info(
            "Skipping SELL for non-held %s — would mix swing geometry "
            "(%s, %dd) with intraday MIS short",
            symbol, holding_period, expected_days,
        )
        return _evaluation_with_outcome(
            symbol, "short_on_swing_horizon",
            (
                f"SELL on non-held {symbol} with "
                f"holding_period='{holding_period}' would require "
                f"intraday/MIS — dropped"
            ),
            prediction=prediction,
            holding_period=holding_period, product=product,
            expected_days=expected_days,
        )
    holding_period, product, expected_days = adjusted

    # Step 8: intraday cutoff (skip intraday signals after configured time)
    if holding_period == "intraday":
        cutoff_str = cfg.market_hours.intraday_cutoff
        cutoff_parts = cutoff_str.split(":")
        cutoff_time = time(int(cutoff_parts[0]), int(cutoff_parts[1]))
        if now_time >= cutoff_time:
            logger.info(
                "Intraday cutoff: skipping %s %s (after %s)",
                prediction.signal_type, symbol, cutoff_str,
            )
            return _evaluation_with_outcome(
                symbol, "intraday_cutoff",
                f"intraday signal after {cutoff_str} cutoff",
                prediction=prediction,
                holding_period=holding_period, product=product,
                expected_days=expected_days,
            )

    # Step 9: ATR-based target/SL with sanity cap + interpolation.
    # Intraday geometry must use the 5-min ATR (what the intraday model's
    # target/SL labels were built from) — the daily ATR is ~5-10× larger
    # and would blow the SL/target out to swing scale on a same-day trade.
    geom_features = (
        intraday_features
        if (holding_period == "intraday" and intraday_features)
        else features
    )
    entry = prediction.entry_price
    atr = geom_features.get("atr_14", entry * 0.02)
    atr_pct = atr / entry if entry > 0 else 0.0

    # Hard sanity reject: an ATR% above the ceiling is implausible for an
    # NSE equity (real ATRs are ~1-8%) and almost always means corrupt
    # OHLCV (e.g. a wrong-symbol bar). Sizing off it yields nonsense
    # target/SL (a +189% target / -94% SL was the live symptom), so reject
    # rather than emit a tradeable signal.
    hard_reject = float(getattr(cfg.strategy, "max_atr_pct_hard_reject", 0.0))
    if hard_reject > 0 and atr_pct > hard_reject:
        return _evaluation_with_outcome(
            symbol, "implausible_atr",
            f"ATR% {atr_pct * 100:.1f}% exceeds sanity ceiling "
            f"{hard_reject * 100:.0f}% (atr={atr:.2f}, entry={entry:.2f}) — "
            "likely corrupt OHLCV",
            prediction=prediction, holding_period=holding_period,
            product=product, expected_days=expected_days,
        )

    # Cap the ATR used for geometry per the (interpolated) holding bucket
    # so a high or noisy ATR can't produce an unreachable target. Applies
    # to every bucket now — previously only intraday was clamped, leaving
    # swing/CNC signals exposed.
    max_atr_pct = interpolate_atr_pct_cap(expected_days, cfg.strategy.holding_periods)
    if max_atr_pct > 0:
        atr_cap = entry * max_atr_pct
        if atr > atr_cap:
            logger.info(
                "Clamping ATR for %s (%s): %.2f → %.2f (entry=%.2f, max_atr_pct=%.3f)",
                symbol, holding_period, atr, atr_cap, entry, max_atr_pct,
            )
            atr = atr_cap
    target_mult, sl_mult = interpolate_atr_multipliers(
        expected_days, cfg.strategy.holding_periods,
    )

    if prediction.signal_type == "BUY":
        target_price = entry + target_mult * atr
        stop_loss_price = entry - sl_mult * atr
    else:  # SELL — HOLD already returned earlier
        target_price = entry - target_mult * atr
        stop_loss_price = entry + sl_mult * atr

    target_price = max(target_price, 0.01)
    stop_loss_price = max(stop_loss_price, 0.01)

    # Step 10: reality-check intraday targets against today's circuit limits
    if holding_period == "intraday" and cfg.market_data.kite_data_enabled:
        target_price, stop_loss_price = await _reality_check_intraday_target(
            ctx, symbol, prediction.signal_type,
            target_price, stop_loss_price,
        )

    # Snap to the per-symbol tick grid. Without this, a signal on a
    # 0.05-tick stock would show "target 34.43" in the DB / UI, but
    # the order placed at the broker would round to 34.45 — a confusing
    # discrepancy between the displayed and actual exit. Falls back to
    # plain 2-decimal rounding when the broker doesn't expose the
    # method (older mocks in tests).
    rounder = getattr(ctx.broker, "round_to_tick", None)
    if callable(rounder):
        target_price = rounder(symbol, target_price)
        stop_loss_price = rounder(symbol, stop_loss_price)
    else:
        target_price = round(target_price, 2)
        stop_loss_price = round(stop_loss_price, 2)

    # Step 11: confidence floor (per-mode min_confidence)
    effective_min = cfg.risk.resolve_min_confidence(
        holding_period, prediction.signal_type,
    )
    if prediction.confidence < effective_min:
        logger.info(
            "Low confidence for %s: %s @ %.2f < %.2f (%s)",
            symbol, prediction.signal_type, prediction.confidence,
            effective_min, _format_class_probs(prediction),
        )
        return SignalEvaluation(
            symbol=symbol,
            outcome="low_confidence",
            detail=(
                f"{prediction.signal_type} @ confidence "
                f"{prediction.confidence:.2f} < {effective_min:.2f}"
            ),
            prediction=prediction,
            signal_type=prediction.signal_type,
            holding_period=holding_period,
            product=product,
            expected_days=expected_days,
            entry_price=entry,
            target_price=target_price,
            stop_loss_price=stop_loss_price,
            confidence=prediction.confidence,
            effective_min_confidence=effective_min,
            model_version=prediction.model_version,
            class_probabilities=getattr(prediction, "class_probabilities", None),
        )

    # Passed all gates
    return SignalEvaluation(
        symbol=symbol,
        outcome="passed",
        detail=f"{prediction.signal_type} @ {prediction.confidence:.2f}",
        prediction=prediction,
        signal_type=prediction.signal_type,
        holding_period=holding_period,
        product=product,
        expected_days=expected_days,
        entry_price=entry,
        target_price=target_price,
        stop_loss_price=stop_loss_price,
        confidence=prediction.confidence,
        effective_min_confidence=effective_min,
        model_version=prediction.model_version,
        class_probabilities=getattr(prediction, "class_probabilities", None),
    )
