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

from datetime import datetime, timedelta

from yolovest.data.features import (
    MODEL_FEATURE_EXCLUSIONS,
    IndicatorConfig,
    compute_features,
    merge_feedback_features,
)
from yolovest.data.fno_features import FNO_FEATURE_KEYS, compute_fno_features
from yolovest.data.news_features import NEWS_FEATURE_KEYS, compute_news_features
from yolovest.data.vix_features import VIX_FEATURE_KEYS, compute_vix_features
from yolovest.models.schemas import OHLCVBar
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST

logger = logging.getLogger(__name__)


def _decision_sharpe(
    candidate: dict[str, Any], incumbent: dict[str, Any] | None,
) -> tuple[float, float]:
    """Return (candidate, incumbent) Sharpe on a like-for-like basis for
    deploy/promote comparisons.

    Prefers the bootstrapped lower-bound (`sharpe_lower`) — robust to a
    lucky single-holdout slice — but only when BOTH sides carry it.
    Otherwise falls back to point Sharpe on BOTH sides, so a candidate's
    conservative lower bound is never pitted against a pre-`sharpe_lower`
    incumbent's optimistic point estimate (which would unfairly block
    honest retrains during the transition). Once an incumbent trained on
    the new code reaches production, every later comparison is
    lower-vs-lower automatically.
    """
    inc = incumbent or {}

    def _num(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    c_low = _num(candidate.get("sharpe_lower"))
    i_low = _num(inc.get("sharpe_lower"))
    if c_low is not None and i_low is not None:
        return c_low, i_low

    c_pt = _num(candidate.get("sharpe"))
    if c_pt is None:
        c_pt = _num(candidate.get("sharpe_ratio"))
    i_pt = _num(inc.get("sharpe_ratio"))
    if i_pt is None:
        i_pt = _num(inc.get("sharpe"))
    return (c_pt or 0.0), (i_pt or 0.0)


def passes_edge_gate(
    metrics: dict[str, Any], min_argmax_sharpe: float,
) -> tuple[bool, str]:
    """Honest-edge promotion gate.

    A model may trade live only if its *argmax* walk-forward Sharpe — the
    edge of its natural, untuned decisions — clears `min_argmax_sharpe`.
    The threshold-tuned Sharpe stored as the headline metric is
    selection-biased: a threshold sweep can find a tiny high-probability
    tail that backtests beautifully while the model's argmax actually
    loses money (the real failure that put a -7 argmax Sharpe intraday
    model on a live account). Gating on `argmax_sharpe` blocks that.

    Returns ``(passes, reason)``. When `argmax_sharpe` is absent — legacy
    artifacts and the synthetic-payoff training path don't produce it —
    the gate is skipped (``passes=True``) so honest older models aren't
    spuriously blocked. The gate is disabled entirely when
    `min_argmax_sharpe` is negative.
    """
    if min_argmax_sharpe < 0:
        return True, "edge gate disabled (min_argmax_sharpe < 0)"
    raw = metrics.get("argmax_sharpe")
    if raw is None:
        return True, "no argmax_sharpe in metrics — edge gate skipped"
    try:
        argmax = float(raw)
    except (TypeError, ValueError):
        return True, "argmax_sharpe unparseable — edge gate skipped"
    if argmax < min_argmax_sharpe:
        return False, (
            f"argmax Sharpe {argmax:.2f} < required {min_argmax_sharpe:.2f}: "
            f"the model has no honest edge — its backtest profit relies on a "
            f"threshold-selected tail and it must not trade live"
        )
    return True, f"argmax Sharpe {argmax:.2f} >= {min_argmax_sharpe:.2f}"


def intraday_path_aware_label(
    *,
    bars: list["OHLCVBar"],
    start_idx: int,
    lookahead: int,
    entry: float,
    target_pct: float,
    sl_pct: float,
) -> int:
    """Path-aware label for an intraday (same-session) trade.

    Same target-before-SL geometry as the daily ``_path_aware_label`` but
    with a HARD same-day close-out: the forward walk stops at the session
    boundary — the first bar whose calendar date differs from the entry
    bar's. There is no overnight carry for MIS, so a move that only
    materialises in a later session must not count toward the label
    (the contamination the daily labels suffer). A trade that hits
    neither barrier within ``lookahead`` bars OR before the session ends
    is a no-trade → HOLD.

    `entry` is the fill price (the caller passes ``bars[start_idx+1].open``
    — the next-bar open, the earliest an intraday signal computed at
    ``bars[start_idx].close`` can actually fill). Barriers are checked
    from the entry bar onward.

    Returns: 2 BUY, 0 SELL, 1 HOLD.
    """
    if start_idx + 1 >= len(bars):
        return 1
    session_date = bars[start_idx + 1].timestamp.date()

    buy_target = entry * (1 + target_pct)
    buy_sl = entry * (1 - sl_pct)
    sell_target = entry * (1 - target_pct)
    sell_sl = entry * (1 + sl_pct)

    buy_outcome: str | None = None
    sell_outcome: str | None = None
    buy_win_bar: int | None = None
    sell_win_bar: int | None = None

    end_idx = min(start_idx + lookahead, len(bars) - 1)
    for k in range(start_idx + 1, end_idx + 1):
        bar = bars[k]
        # Hard same-session close-out — never look across the day boundary.
        if bar.timestamp.date() != session_date:
            break
        hi, lo = bar.high, bar.low

        if buy_outcome is None:
            target_now = hi >= buy_target
            sl_now = lo <= buy_sl
            if target_now and sl_now:
                buy_outcome = "ambiguous"
            elif target_now:
                buy_outcome = "win"
                buy_win_bar = k
            elif sl_now:
                buy_outcome = "loss"

        if sell_outcome is None:
            target_now = lo <= sell_target
            sl_now = hi >= sell_sl
            if target_now and sl_now:
                sell_outcome = "ambiguous"
            elif target_now:
                sell_outcome = "win"
                sell_win_bar = k
            elif sl_now:
                sell_outcome = "loss"

        if buy_outcome is not None and sell_outcome is not None:
            break

    buy_won = buy_outcome == "win"
    sell_won = sell_outcome == "win"

    if buy_won and sell_won:
        # First-winner disambiguation; same-bar cross-direction → HOLD.
        if buy_win_bar is not None and sell_win_bar is not None:
            if buy_win_bar < sell_win_bar:
                return 2
            if sell_win_bar < buy_win_bar:
                return 0
        return 1
    if buy_won:
        return 2
    if sell_won:
        return 0
    return 1


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

        # Step 1-2: Load training data and feedback. max_training_days
        # caps history so the feature matrix fits in RAM on small
        # hosts (a 2 GB EC2 instance OOMs on 5 years × ~500 symbols).
        training_data = await self.ctx.db.get_training_dataset(
            max_days=cfg.max_training_days,
        )
        predictions_vs_actual = await self.ctx.db.get_prediction_outcomes()

        # Sector map for sector-relative momentum features. A stock
        # outperforming its sector is a stronger signal than just
        # outperforming the universe; the model gets both.
        unique_symbols = sorted({
            row.get("symbol", "") for row in training_data.get("bars", [])
            if row.get("symbol")
        })
        try:
            sector_map = await self.ctx.db.get_symbol_sectors_map(unique_symbols)
        except Exception:
            logger.warning("Failed to load sector map; sector features will be neutral", exc_info=True)
            sector_map = {}

        # Bulk-deal lookup for ML features. We pre-build once because
        # _prepare_training_data is sync and one DB query per sample
        # would be prohibitively slow on a year of training data.
        bulk_deal_lookup: dict[tuple[str, str], dict[str, int]] = {}
        try:
            deals_timeline = await self.ctx.db.get_bulk_deals_timeline()
            for d in deals_timeline:
                key = (d["symbol"], d["deal_date"])
                counts = bulk_deal_lookup.setdefault(key, {"buy": 0, "sell": 0})
                bs = str(d.get("buy_sell", "")).upper()
                if bs == "BUY":
                    counts["buy"] += 1
                elif bs == "SELL":
                    counts["sell"] += 1
        except Exception:
            logger.warning(
                "Failed to load bulk-deals timeline; bulk-deal features will be 0",
                exc_info=True,
            )

        # News-sentiment lookup: symbol → list of (headline, published_at_iso),
        # sorted by published_at. Built once so per-sample VADER aggregation
        # avoids the N+1 query trap. Window is max_training_days + 7 so the
        # earliest training sample still has a full 7d news window behind it.
        news_lookup: dict[str, list[tuple[str, str]]] = {}
        try:
            news_from = (
                datetime.now(IST) - timedelta(days=cfg.max_training_days + 7)
            ).strftime("%Y-%m-%d")
            news_lookup = await self.ctx.db.get_news_timeline(date_from=news_from)
            logger.info(
                "News timeline: %d symbols with headlines since %s",
                len(news_lookup), news_from,
            )
        except Exception:
            logger.warning(
                "Failed to load news timeline; news features will be neutral",
                exc_info=True,
            )

        # India VIX timeline: single broadcast series shared across every
        # symbol on a given date. Window extends 30 calendar days past the
        # earliest training sample so the trailing-20d z-score has full
        # history at every sample. Empty list → neutral features at
        # compute time; no crash.
        vix_timeline: list[tuple[str, float]] = []
        try:
            vix_from = (
                datetime.now(IST) - timedelta(days=cfg.max_training_days + 30)
            ).strftime("%Y-%m-%d")
            vix_timeline = await self.ctx.db.get_vix_timeline(date_from=vix_from)
            logger.info(
                "VIX timeline: %d daily bars since %s",
                len(vix_timeline), vix_from,
            )
        except Exception:
            logger.warning(
                "Failed to load VIX timeline; VIX features will be neutral",
                exc_info=True,
            )

        # F&O option-chain timeline. Per-symbol lookup → list of
        # (date_str, agg_row). Forward-only (no historical backfill
        # available from Kite), so older training rows return
        # is_fno_stock=0 / others=0 and the model learns to weight
        # these features only when present.
        fno_lookup: dict[str, list[tuple[str, dict[str, float]]]] = {}
        try:
            fno_from = (
                datetime.now(IST) - timedelta(days=cfg.max_training_days + 2)
            ).strftime("%Y-%m-%d")
            fno_lookup = await self.ctx.db.get_fno_timeline(date_from=fno_from)
            logger.info(
                "F&O timeline: %d underlyings with daily aggregates since %s",
                len(fno_lookup), fno_from,
            )
        except Exception:
            logger.warning(
                "Failed to load F&O timeline; F&O features will be neutral",
                exc_info=True,
            )

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

        # Lookahead periods: intraday uses 1-bar, swing uses 10-bar
        # returns. 10 bars (~2 weeks) gives genuine swing setups
        # enough room for the 1.5×ATR target to develop without the
        # 0.75×ATR SL noise-tripping on the same window — at 5 bars
        # the SL fires constantly and the labeler classes most
        # outcomes as HOLD even after the first-winner disambiguation.
        lookahead_map = {"intraday": 1, "swing": 10}

        # Match each model's path-aware label geometry to the holding
        # bucket it actually trades at runtime: intraday uses the tight
        # MIS multipliers (0.6 / 0.3 by default), swing uses the wider
        # short-swing CNC multipliers (1.5 / 0.75). Keeping label
        # geometry in sync with runtime geometry is the whole point of
        # path-aware labels — otherwise the model learns one game and
        # plays a different one.
        hp = self.ctx.config.strategy.holding_periods
        atr_mult_map = {
            "intraday": (hp.intraday.target, hp.intraday.stop_loss),
            "swing": (hp.short_swing.target, hp.short_swing.stop_loss),
        }

        # Imported once here (not inside the loop) — an early `continue`
        # on insufficient features used to skip the in-loop import,
        # leaving _gc unbound for the post-loop collect() below.
        import gc as _gc

        for model_type in ("intraday", "swing"):
            # Build feature matrix with model-specific labeling + feedback features
            lookahead = lookahead_map[model_type]
            target_mult, sl_mult = atr_mult_map[model_type]
            X, y, feat_names, sample_weights, bars_meta = self._prepare_training_data(
                training_data, lookahead_bars=lookahead, feedback_data=feedback_data,
                target_atr_mult=target_mult, sl_atr_mult=sl_mult,
                sector_map=sector_map,
                bulk_deal_lookup=bulk_deal_lookup,
                news_lookup=news_lookup,
                vix_timeline=vix_timeline,
                fno_lookup=fno_lookup,
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

            # Label distribution — settles "is BUY even represented in
            # training?" when the live model is producing zero BUYs.
            # Class ints: 0=SELL, 1=HOLD, 2=BUY (per _path_aware_label).
            label_counts = {"SELL": 0, "HOLD": 0, "BUY": 0}
            for label in y:
                if label == 0:
                    label_counts["SELL"] += 1
                elif label == 1:
                    label_counts["HOLD"] += 1
                elif label == 2:
                    label_counts["BUY"] += 1
            total = sum(label_counts.values()) or 1
            label_pct = {
                k: round(v / total * 100, 1) for k, v in label_counts.items()
            }
            logger.info(
                "Label distribution for %s: BUY=%d (%.1f%%), HOLD=%d (%.1f%%), SELL=%d (%.1f%%) [n=%d]",
                model_type,
                label_counts["BUY"], label_pct["BUY"],
                label_counts["HOLD"], label_pct["HOLD"],
                label_counts["SELL"], label_pct["SELL"],
                total,
            )

            # Train-time guard: refuse to save a model trained on a
            # corpus where any class is functionally extinct. Catches
            # the "BUYs ≈ never" failure mode before it ships.
            min_pct = self.ctx.config.strategy.class_balance_min_pct
            if min_pct > 0:
                rare = {k: pct for k, pct in label_pct.items() if pct < min_pct}
                if rare:
                    msg = (
                        f"Refusing to train {model_type}: class(es) "
                        f"{', '.join(f'{k}={pct:.1f}%' for k, pct in rare.items())} "
                        f"< min {min_pct:.1f}%. Tune target/SL ATR multipliers "
                        f"or extend max_training_days to capture more setups."
                    )
                    logger.warning(msg)
                    try:
                        await self.ctx.notify.send(
                            f"Retrain skipped for {model_type}: " + msg,
                            alert_type="errors",
                        )
                    except Exception:
                        logger.debug("Failed to notify on label guard", exc_info=True)
                    results[model_type] = {"error": msg, "label_pct": label_pct}
                    continue

            # Class balancing: inverse-frequency weights so rare classes
            # (typically BUY under path-aware 2:1 R/R labelling) aren't
            # buried under the HOLD majority. Multiplies into the
            # existing feedback-driven sample_weights. Sklearn's
            # "balanced" formula: w[c] = N / (K * count[c]).
            class_weights: dict[int, float] = {}
            if self.ctx.config.strategy.class_balance_enabled:
                # Map: label int → BUY/HOLD/SELL key for count lookup.
                key_for = {0: "SELL", 1: "HOLD", 2: "BUY"}
                K = sum(1 for k in key_for.values() if label_counts.get(k, 0) > 0)
                for lbl, key in key_for.items():
                    c = label_counts.get(key, 0)
                    class_weights[lbl] = (total / (K * c)) if c > 0 else 0.0

                if not sample_weights:
                    sample_weights = [1.0] * len(y)
                sample_weights = [
                    sw * class_weights.get(int(lbl), 1.0)
                    for sw, lbl in zip(sample_weights, y, strict=False)
                ]
                logger.info(
                    "Class weights for %s: BUY=%.2f, HOLD=%.2f, SELL=%.2f",
                    model_type,
                    class_weights.get(2, 0.0),
                    class_weights.get(1, 0.0),
                    class_weights.get(0, 0.0),
                )

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
                # Lookahead window (in trading days) so the CV can purge
                # train samples whose label window overlaps the test
                # fold — without it the multi-bar label leaks across the
                # train/test boundary.
                train_params["lookahead_bars"] = lookahead
                train_params["backtest_product"] = (
                    "MIS" if model_type == "intraday" else "CNC"
                )
                # Bound the backtest's concurrent-positions count to
                # the same cap the live engine enforces. Without
                # this, the simulator treats every signal as
                # independently fillable and inflates Sharpe (e.g.
                # 12.98 intraday on the user's last run).
                train_params["backtest_max_positions"] = (
                    self.ctx.config.risk.max_open_positions
                )
                metrics = await self.ctx.ml.train(
                    model_type, X, y, train_params, feature_names=feat_names,
                )
                # Stash label distribution + class weights so
                # MLModelsPage can show whether a given checkpoint was
                # trained on a class-balanced sample or a heavily-skewed
                # one. Lives next to the existing numeric metrics in
                # metrics_json.
                metrics["label_counts"] = label_counts
                metrics["label_pct"] = label_pct
                if class_weights:
                    metrics["class_weights"] = {
                        "BUY": round(class_weights.get(2, 0.0), 4),
                        "HOLD": round(class_weights.get(1, 0.0), 4),
                        "SELL": round(class_weights.get(0, 0.0), 4),
                    }
                # Post-train class check: run the fresh model on the
                # most recent N training rows and verify all three
                # classes are reachable. Catches calibration-collapse
                # or feature-dominance cases where the label balance
                # was fine but the model still never picks a class.
                if self.ctx.config.strategy.post_train_class_check_enabled:
                    try:
                        import numpy as np  # noqa: PLC0415

                        booster = self.ctx.ml._get_model(model_type)  # noqa: SLF001
                        # Sample the freshest N rows — that's what the
                        # production model will see first in live use.
                        n_check = min(1000, len(X))
                        X_check = np.asarray(X[-n_check:])
                        preds = booster.predict(X_check)
                        pred_counts = {0: 0, 1: 0, 2: 0}
                        for p in preds:
                            pred_counts[int(p)] = pred_counts.get(int(p), 0) + 1
                        # Map: 0=SELL, 1=HOLD, 2=BUY.
                        pred_dist = {
                            "SELL": pred_counts.get(0, 0),
                            "HOLD": pred_counts.get(1, 0),
                            "BUY": pred_counts.get(2, 0),
                        }
                        logger.info(
                            "Post-train prediction distribution for %s "
                            "(n=%d): BUY=%d, HOLD=%d, SELL=%d",
                            model_type, n_check,
                            pred_dist["BUY"], pred_dist["HOLD"], pred_dist["SELL"],
                        )
                        metrics["post_train_pred_dist"] = pred_dist

                        missing = [k for k, v in pred_dist.items() if v == 0]
                        if missing:
                            msg = (
                                f"Refusing to save {model_type}: trained "
                                f"booster never predicts class(es) "
                                f"{', '.join(missing)} on the most recent "
                                f"{n_check} samples. Production would see "
                                f"zero of those signals."
                            )
                            logger.warning(msg)
                            try:
                                await self.ctx.notify.send(
                                    f"Retrain skipped for {model_type}: " + msg,
                                    alert_type="errors",
                                )
                            except Exception:
                                logger.debug(
                                    "Failed to notify on post-train guard",
                                    exc_info=True,
                                )
                            results[model_type] = {
                                "error": msg,
                                "post_train_pred_dist": pred_dist,
                                "label_pct": label_pct,
                            }
                            continue
                    except Exception:
                        # Inference inside the guard shouldn't crash
                        # the retrain — fall through and save the model.
                        logger.debug(
                            "Post-train class check failed; saving anyway",
                            exc_info=True,
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

                # Step 5: Compare with production on the robust
                # (bootstrapped lower-bound) Sharpe, not the noisy
                # single-holdout point estimate.
                current = await self.ctx.db.get_production_model(model_type)
                cand_sharpe, current_sharpe = _decision_sharpe(metrics, current)

                improved = cand_sharpe > current_sharpe
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
            # Free per-model scratch (feature matrix + bars_meta) before
            # the next model's _prepare_training_data allocates its
            # own copy. Without this the intraday and swing matrices
            # would briefly coexist and OOM the process on a 2 GB host.
            X = y = feat_names = sample_weights = bars_meta = None  # type: ignore[assignment]
            _gc.collect()

        # Free training_data eagerly — _check_shadow_promotions doesn't
        # need it and it's the largest single resident structure
        # (~365K rows × 8 fields at the default 730-day cap, much more
        # if the user raised retraining.max_training_days).
        training_data = None  # type: ignore[assignment]
        _gc.collect()

        # Step 7: Check shadow promotions
        promotions = await self._check_shadow_promotions()

        # Clear drift-watch suspension if any model successfully
        # retrained. The next signal-gen cycle will then run normally;
        # drift-watch will re-evaluate at 16:30 IST and re-suspend
        # only if the new model still shows the same decay.
        any_success = any(
            isinstance(r, dict) and "error" not in r and "version" in r
            for r in results.values()
        )
        if any_success:
            try:
                cur = await self.ctx.db.get_system_state(
                    "signal_gen_suspended_by_drift",
                )
                if cur:
                    await self.ctx.db.set_system_state(
                        "signal_gen_suspended_by_drift", "",
                    )
                    logger.info(
                        "model-retrain: cleared drift-watch suspension "
                        "(was: %s) — signal generation resumes next cycle",
                        cur,
                    )
            except Exception:
                logger.debug(
                    "model-retrain: failed to clear drift suspension",
                    exc_info=True,
                )

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
        target_atr_mult: float = 1.5,
        sl_atr_mult: float = 0.75,
        sector_map: dict[str, str] | None = None,
        bulk_deal_lookup: dict[tuple[str, str], dict[str, int]] | None = None,
        news_lookup: dict[str, list[tuple[str, str]]] | None = None,
        vix_timeline: list[tuple[str, float]] | None = None,
        fno_lookup: dict[str, list[tuple[str, dict[str, float]]]] | None = None,
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

        # Minimum window size for feature computation. Must match the
        # longest-lookback indicator (EMA-200) so every emitted sample
        # carries the full feature set from its very first iteration.
        # The previous value of 50 caused samples 50-199 to lack
        # ema_200 → the discovery-and-backfill loop below would
        # backfill them with 0.0, training the model to associate
        # ema_200=0 with "early history" when at inference ema_200 is
        # always non-zero. Inference distribution didn't match training.
        # Bumping to 200 eliminates the train-inference mismatch.
        # `window` is a per-iteration slice that's GC'd after
        # compute_features returns, so the larger window doesn't
        # accumulate memory across samples.
        window_size = 200
        X: list[list[float]] = []
        y: list[int] = []
        sample_weights: list[float] = []
        feature_names: list[str] = []
        feature_names_set: set[str] = set()
        # Parallel to X/y — used by the walk-forward backtest to
        # simulate real PnL instead of the legacy +1%/-0.5% fiction.
        bars_meta: list[dict[str, Any]] = []

        # Cross-sectional market-regime features. For each timestamp
        # (date for daily bars, datetime for intraday) compute the
        # universe-wide breadth: fraction of stocks up vs prior close
        # and the average %-return. This proxies the "is today
        # broadly trending or chopping" context that the per-stock
        # features can't see, without requiring a separate NIFTY
        # ingest. Built once up-front, then looked up per-sample.
        regime_by_ts: dict[str, dict[str, float]] = self._compute_regime_index(by_symbol)

        # Sector-relative features. Compute per-(sector, ts) breadth
        # and avg-return plus a per-(symbol, ts) return so the sample
        # build can derive `relative_momentum` = stock_return -
        # sector_avg_return. A stock outperforming its sector index
        # is a stronger signal than just outperforming the universe.
        sector_map = sector_map or {}
        sector_regime, symbol_returns = self._compute_sector_index(
            by_symbol, sector_map,
        )

        # Bulk-deal lookup: (symbol, deal_date) -> {"buy", "sell"} counts.
        # Pre-built in execute() (async) and passed in via bulk_deal_lookup
        # so we only need one DB scan instead of one query per sample.
        bulk_deal_lookup = bulk_deal_lookup or {}
        # Per-symbol sorted deal-date list for fast 5-day window lookups.
        bulk_dates_by_sym: dict[str, list[str]] = {}
        for (sym_key, date_key) in bulk_deal_lookup.keys():
            bulk_dates_by_sym.setdefault(sym_key, []).append(date_key)
        for v in bulk_dates_by_sym.values():
            v.sort()

        # News-sentiment lookup: parse published_at once per article so the
        # per-sample loop can binary-slice the symbol's headline timeline.
        news_lookup = news_lookup or {}
        news_parsed_by_sym: dict[str, list[tuple[str, datetime]]] = {}
        for sym_key, entries in news_lookup.items():
            parsed: list[tuple[str, datetime]] = []
            for headline, published_at in entries:
                try:
                    dt = datetime.fromisoformat(published_at)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=IST)
                    parsed.append((headline, dt))
                except (ValueError, TypeError):
                    continue
            if parsed:
                news_parsed_by_sym[sym_key] = parsed

        for sym, rows in by_symbol.items():
            if len(rows) < window_size + 1:
                continue

            # Compute sample weight for this symbol based on recent performance
            # Per-symbol failure flag — used INSIDE the per-bar loop
            # below to apply the boost only to recent bars. The old
            # behaviour upweighted every historical bar of a symbol
            # whose recent accuracy was <50%, which overfits the
            # model to that symbol's idiosyncratic past rather than
            # learning from the conditions that produced the failures.
            symbol_has_recent_failure = False
            if feedback_data and sym in feedback_data:
                fb = feedback_data[sym]
                pred_acc = fb.get("pred_accuracy", 0.5)
                dry_acc = fb.get("dry_run_accuracy", 0.5)
                if min(pred_acc, dry_acc) < 0.5:
                    symbol_has_recent_failure = True
            feedback_lookback_days = int(
                self.ctx.config.strategy.feedback.lookback_days or 60
            )

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

                # Merge cross-sectional market-regime features for this
                # timestamp (universe breadth + avg %-return). Symbols
                # alone can't tell the model "today is a chop day" —
                # this layer does. Key is the YYYY-MM-DD date string
                # to match the index built by _compute_regime_index.
                _ts = bars[i].timestamp.strftime("%Y-%m-%d")
                _regime = regime_by_ts.get(_ts)
                if _regime:
                    features["universe_breadth"] = _regime["breadth"]
                    features["universe_avg_return"] = _regime["avg_return"]
                else:
                    features["universe_breadth"] = 0.5
                    features["universe_avg_return"] = 0.0

                # Sector-relative features. relative_momentum is the
                # main signal — stock's return minus its sector's
                # average return. Falls back to neutral when the
                # symbol's sector is unknown or has < 3 peers at this
                # timestamp.
                _sec = sector_map.get(sym)
                _sec_stats = sector_regime.get((_sec, _ts)) if _sec else None
                _stock_ret = symbol_returns.get((sym, _ts))
                if _sec_stats and _stock_ret is not None:
                    features["sector_breadth"] = _sec_stats["breadth"]
                    features["sector_avg_return"] = _sec_stats["avg_return"]
                    features["relative_momentum"] = (
                        _stock_ret - _sec_stats["avg_return"]
                    )
                else:
                    features["sector_breadth"] = 0.5
                    features["sector_avg_return"] = 0.0
                    features["relative_momentum"] = 0.0

                # Institutional flow features: bulk-deal net count and
                # average delivery % over the prior 5 bars. Both default
                # to 0 when no data is available (older training rows
                # predate the data sources). Compounds with the
                # institutional_flow risk-check multiplier so the model
                # learns to score these signals natively at inference.
                # bars[i].timestamp is a datetime; the bulk_deals table
                # stores deal_date as YYYY-MM-DD strings, so format
                # consistently before the lookup.
                _sample_date = bars[i].timestamp.strftime("%Y-%m-%d")
                _bulk_window_start = bars[max(0, i - 5)].timestamp.strftime("%Y-%m-%d")
                _bd_dates = bulk_dates_by_sym.get(sym, [])
                _bd_buy = _bd_sell = 0
                for d in _bd_dates:
                    if d > _sample_date:
                        break
                    if d >= _bulk_window_start:
                        counts = bulk_deal_lookup.get((sym, d), {})
                        _bd_buy += counts.get("buy", 0)
                        _bd_sell += counts.get("sell", 0)
                features["bulk_deal_buy_5d"] = float(_bd_buy)
                features["bulk_deal_sell_5d"] = float(_bd_sell)
                features["bulk_deal_net_5d"] = float(_bd_buy - _bd_sell)

                # delivery_pct rolling-5 average. bars[i] is the current
                # sample's bar; look back 5 bars including it. Rows
                # ingested before migration 038 have NULL → treated as 0.
                _delivery_values: list[float] = []
                for k in range(max(0, i - 4), i + 1):
                    dp = getattr(bars[k], "delivery_pct", None)
                    if dp is None and isinstance(rows[k], dict):
                        dp = rows[k].get("delivery_pct")
                    if dp is not None:
                        try:
                            _delivery_values.append(float(dp))
                        except (TypeError, ValueError):
                            pass
                if _delivery_values:
                    features["delivery_pct_avg_5d"] = (
                        sum(_delivery_values) / len(_delivery_values)
                    )
                else:
                    features["delivery_pct_avg_5d"] = 0.0

                # News-sentiment features. Slice the symbol's pre-parsed
                # headline timeline to entries published before this bar's
                # timestamp; compute_news_features handles the 24h / 7d
                # window aggregation. Bar timestamps in training_data are
                # naive; coerce to IST to match the parsed published_at
                # tz so the window-cutoff comparisons stay correct.
                _bar_ts = bars[i].timestamp
                if _bar_ts.tzinfo is None:
                    _bar_ts = _bar_ts.replace(tzinfo=IST)
                _sym_news = news_parsed_by_sym.get(sym)
                if _sym_news:
                    news_feats = compute_news_features(_sym_news, _bar_ts)
                else:
                    news_feats = {k: 0.0 for k in NEWS_FEATURE_KEYS}
                features.update(news_feats)

                # India VIX regime features. Single broadcast series — every
                # symbol on the same _sample_date sees identical VIX values.
                # compute_vix_features handles the trailing-window slicing.
                if vix_timeline:
                    vix_feats = compute_vix_features(vix_timeline, _sample_date)
                else:
                    vix_feats = {k: 0.0 for k in VIX_FEATURE_KEYS}
                features.update(vix_feats)

                # F&O derivatives features. Only F&O-eligible symbols have
                # rows in the timeline; misses return is_fno_stock=0 and
                # the model learns to weight these features only when
                # present. Pass equity closes from the OHLCV window so the
                # oi_buildup classification uses the canonical underlying
                # price change instead of the futures close (which can
                # diverge near expiry).
                _sym_fno = (fno_lookup or {}).get(sym)
                if _sym_fno:
                    _prior_close = bars[i - 1].close if i >= 1 else None
                    fno_feats = compute_fno_features(
                        _sym_fno, _sample_date,
                        prior_stock_close=_prior_close,
                        current_stock_close=bars[i].close,
                    )
                else:
                    fno_feats = {k: 0.0 for k in FNO_FEATURE_KEYS}
                features.update(fno_feats)

                # Path-aware label: BUY iff target hits before SL when
                # walking forward bar-by-bar, using the same ATR-based
                # geometry the live trades use.
                #
                # ENTRY PRICE: bars[i+1].open, NOT bars[i].close.
                # The model sees features computed at bars[i].close
                # (end of session i), but it can never actually enter
                # at that price — the earliest a heartbeat fires the
                # next morning is at the next session's open. Training
                # on close-as-entry while live execution uses open-as-
                # entry creates an overnight-gap mismatch — on volatile
                # stocks the open can be 0.3-0.8% away from close, which
                # is wider than a 0.3×ATR intraday SL. The model would
                # see a "winning" pattern in training that in production
                # is already stopped out before it can react.
                current_close = bars[i].close  # kept for backtest path
                next_open = bars[i + 1].open if i + 1 < len(bars) else current_close
                future_close = bars[i + lookahead_bars].close
                atr_pct = features.get("atr_pct") or 0.0
                if next_open <= 0 or atr_pct <= 0:
                    label = 1
                else:
                    label = self._path_aware_label(
                        bars=bars,
                        start_idx=i,
                        lookahead=lookahead_bars,
                        entry=next_open,
                        target_pct=atr_pct * target_atr_mult,
                        sl_pct=atr_pct * sl_atr_mult,
                    )

                # Maintain a stable feature_names list across all samples.
                # Indicators that need more history (e.g. EMA-200) only
                # appear in features once enough bars are in the window —
                # so later samples may produce keys the first sample
                # didn't have. When that happens, extend feature_names
                # and backfill 0.0 into every prior row so np.array(X)
                # ends up rectangular instead of inhomogeneous.
                # MODEL_FEATURE_EXCLUSIONS gates out raw absolute prices /
                # levels (close, ema_*, obv, ...) that don't transfer
                # across stocks at different price levels — they stay in
                # the features dict for the inference layer's entry-price
                # lookups but the trained model never sees them.
                for k in features:
                    if k in MODEL_FEATURE_EXCLUSIONS:
                        continue
                    if k not in feature_names_set:
                        # With window_size = 200 every iteration should
                        # see the full feature set on entry — late-
                        # appearing keys would mean a new optional feature
                        # was added without a 0-default fallback in the
                        # caller. Log so we notice the train-inference
                        # distribution gap instead of silently backfilling.
                        if X:
                            logger.warning(
                                "model-retrain: feature %s appeared at sample "
                                "%d for %s — backfilling 0.0 into %d prior "
                                "rows. Add a 0-default fallback at feature "
                                "production to avoid this.",
                                k, len(X), sym, len(X),
                            )
                        feature_names.append(k)
                        feature_names_set.add(k)
                        for existing in X:
                            existing.append(0.0)
                # Per-bar feedback weight. Apply weight_boost only to
                # bars within the feedback-lookback window — those are
                # the conditions that produced the recent failure. Older
                # bars stay at 1.0 so the model isn't pushed to overfit
                # this symbol's ancient history.
                bar_weight = 1.0
                if symbol_has_recent_failure and bars:
                    try:
                        latest_ts = bars[-1].timestamp
                        this_ts = bars[i].timestamp
                        age_days = (latest_ts - this_ts).days
                        if 0 <= age_days <= feedback_lookback_days:
                            bar_weight = weight_boost
                    except Exception:
                        # Bad timestamps fall through with no boost.
                        pass

                X.append([features.get(k, 0.0) for k in feature_names])
                y.append(label)
                sample_weights.append(bar_weight)
                # Capture the future-window high/low path so the
                # walk-forward backtest can exit at SL or target with
                # the same geometry as the path-aware label, instead of
                # mark-to-market at exit_close.
                window_end = min(i + lookahead_bars, len(bars) - 1)
                path_highs = [bars[k].high for k in range(i + 1, window_end + 1)]
                path_lows = [bars[k].low for k in range(i + 1, window_end + 1)]
                bars_meta.append({
                    "symbol": sym,
                    # Field name preserved for backwards-compat with
                    # walk_forward_backtest; the value is now next-bar
                    # open (the actual entry the model would see at
                    # inference) instead of the same-bar close.
                    "entry_close": float(next_open),
                    "exit_close": float(future_close),
                    "path_highs": path_highs,
                    "path_lows": path_lows,
                    "target_pct": float(atr_pct * target_atr_mult),
                    "sl_pct": float(atr_pct * sl_atr_mult),
                    # YYYY-MM-DD — walk_forward_backtest aggregates by
                    # this to compute daily-equity-curve Sharpe instead
                    # of the per-trade approximation. Several trades on
                    # the same day get netted before the Sharpe stdev.
                    "entry_date": _sample_date,
                })

        # Global chronological sort. Samples are built symbol-by-symbol,
        # so the arrays come out ordered [symbolA_all_dates,
        # symbolB_all_dates, ...]. The walk-forward CV (TimeSeriesSplit)
        # assumes row order == time order — without this sort the
        # "folds" split by SYMBOL position, not date, training on future
        # dates relative to the test fold (severe temporal leakage that
        # inflates the backtest Sharpe and the tuned thresholds). Sort
        # all parallel arrays by entry_date so the split is a genuine
        # cross-sectional walk-forward. Stable sort keeps same-date
        # samples in their original (symbol) order.
        if bars_meta:
            order = sorted(
                range(len(bars_meta)),
                key=lambda i: bars_meta[i].get("entry_date", ""),
            )
            X = [X[i] for i in order]
            y = [y[i] for i in order]
            sample_weights = [sample_weights[i] for i in order]
            bars_meta = [bars_meta[i] for i in order]

        return X, y, feature_names, sample_weights, bars_meta

    @staticmethod
    def _compute_regime_index(
        by_symbol: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, float]]:
        """For each unique timestamp across the training set, aggregate
        the universe to a {breadth, avg_return} dict.

        breadth = fraction of symbols whose close > prior close at that
                  timestamp (0..1). 0.5 = neutral, ≥0.6 strong up,
                  ≤0.4 strong down.
        avg_return = mean of (close − prev_close) / prev_close across
                     symbols at that timestamp.

        This is a cross-sectional proxy for "what is the broad market
        doing right now". It mirrors what a NIFTY 50 day-return feature
        would give but doesn't require a separate index ingest — the
        500-stock universe alone is more than enough breadth.
        """
        # Build per-timestamp aggregator. Date-string key (YYYY-MM-DD)
        # so it matches sample-time lookups that derive the key from
        # bars[i].timestamp (a datetime, formatted to date-only).
        # Without the explicit date prefix, full ISO strings and
        # datetime objects miss each other and every sample falls back
        # to the neutral 0.5 breadth — silently neutering the feature.
        agg: dict[str, list[float]] = {}
        for rows in by_symbol.values():
            prev_close: float | None = None
            for r in rows:
                raw_ts = r.get("timestamp")
                ts = str(raw_ts)[:10] if raw_ts else ""
                c = r.get("close") or 0.0
                if ts and prev_close and prev_close > 0:
                    ret = (c - prev_close) / prev_close
                    agg.setdefault(ts, []).append(ret)
                prev_close = c

        # Need at least 5 symbols at a timestamp for a meaningful breadth
        # reading — otherwise sparse-data timestamps would dominate with
        # noisy 0/1 fractions.
        out: dict[str, dict[str, float]] = {}
        for ts, returns in agg.items():
            if len(returns) < 5:
                continue
            avg_ret = sum(returns) / len(returns)
            up = sum(1 for r in returns if r > 0)
            out[ts] = {
                "breadth": up / len(returns),
                "avg_return": avg_ret,
            }
        return out

    @staticmethod
    def _compute_sector_index(
        by_symbol: dict[str, list[dict[str, Any]]],
        sector_map: dict[str, str],
    ) -> tuple[
        dict[tuple[str, str], dict[str, float]],
        dict[tuple[str, str], float],
    ]:
        """Aggregate per-(sector, ts) breadth + avg_return, and emit
        the per-(symbol, ts) return series so the sample builder can
        compute relative_momentum cheaply.

        Returns: (sector_stats, symbol_returns) where
          sector_stats[(sector, ts)] = {"breadth": .., "avg_return": ..}
          symbol_returns[(symbol, ts)] = return  (close - prev) / prev

        Sectors with < 3 peers at a given timestamp are dropped — small
        cohorts produce noisy breadth and the model is better served
        falling back to neutral than learning from noise.
        """
        # Date-string keys (YYYY-MM-DD), see _compute_regime_index for
        # why — sample-time lookups derive the key from a datetime and
        # mismatched key types silently zero the features out.
        sector_agg: dict[tuple[str, str], list[float]] = {}
        symbol_returns: dict[tuple[str, str], float] = {}
        for sym, rows in by_symbol.items():
            sector = sector_map.get(sym)
            prev_close: float | None = None
            for r in rows:
                raw_ts = r.get("timestamp")
                ts = str(raw_ts)[:10] if raw_ts else ""
                c = r.get("close") or 0.0
                if ts and prev_close and prev_close > 0:
                    ret = (c - prev_close) / prev_close
                    symbol_returns[(sym, ts)] = ret
                    if sector:
                        sector_agg.setdefault((sector, ts), []).append(ret)
                prev_close = c

        sector_stats: dict[tuple[str, str], dict[str, float]] = {}
        for key, returns in sector_agg.items():
            if len(returns) < 3:
                continue
            up = sum(1 for x in returns if x > 0)
            sector_stats[key] = {
                "breadth": up / len(returns),
                "avg_return": sum(returns) / len(returns),
            }
        return sector_stats, symbol_returns

    @staticmethod
    def _path_aware_label(
        *,
        bars: list["OHLCVBar"],
        start_idx: int,
        lookahead: int,
        entry: float,
        target_pct: float,
        sl_pct: float,
    ) -> int:
        """Simulate hypothetical BUY and SELL trades from `start_idx`
        and label by which (if either) hits its target before its SL,
        walking forward bar-by-bar over `lookahead` future bars.

        - BUY:  target_hit when high ≥ entry × (1 + target_pct)
                 SL_hit    when low  ≤ entry × (1 − sl_pct)
        - SELL: target_hit when low  ≤ entry × (1 − target_pct)
                 SL_hit    when high ≥ entry × (1 + sl_pct)

        Both touched in the same bar is treated as ambiguous because
        daily OHLC can't tell us the intra-bar order. When both legs
        cleanly win on DIFFERENT bars, the side that won first wins
        the label — a real trader who took the BUY would have closed
        at target on bar j and not been around for the SELL win on
        bar k>j (and vice versa). The old "both won → HOLD" rule was
        the dominant source of HOLD-label inflation on the swing
        model (87% HOLD) because, on a 5-bar window with SL closer
        than target, oscillating prices regularly trip both legs'
        targets in different bars.

        Returns: 2 BUY, 0 SELL, 1 HOLD.
        """
        buy_target = entry * (1 + target_pct)
        buy_sl = entry * (1 - sl_pct)
        sell_target = entry * (1 - target_pct)
        sell_sl = entry * (1 + sl_pct)

        buy_outcome: str | None = None  # "win" / "loss" / "ambiguous" / None
        sell_outcome: str | None = None
        buy_win_bar: int | None = None
        sell_win_bar: int | None = None

        end_idx = min(start_idx + lookahead, len(bars) - 1)
        for k in range(start_idx + 1, end_idx + 1):
            bar = bars[k]
            hi, lo = bar.high, bar.low

            # BUY trade leg
            if buy_outcome is None:
                target_now = hi >= buy_target
                sl_now = lo <= buy_sl
                if target_now and sl_now:
                    buy_outcome = "ambiguous"
                elif target_now:
                    buy_outcome = "win"
                    buy_win_bar = k
                elif sl_now:
                    buy_outcome = "loss"

            # SELL trade leg
            if sell_outcome is None:
                target_now = lo <= sell_target
                sl_now = hi >= sell_sl
                if target_now and sl_now:
                    sell_outcome = "ambiguous"
                elif target_now:
                    sell_outcome = "win"
                    sell_win_bar = k
                elif sl_now:
                    sell_outcome = "loss"

            if buy_outcome is not None and sell_outcome is not None:
                break

        buy_won = buy_outcome == "win"
        sell_won = sell_outcome == "win"

        if buy_won and sell_won:
            # Disambiguate by which leg's target hit first. Same-bar
            # cross-direction wins fall through to HOLD because daily
            # OHLC can't tell us the intra-bar order.
            if buy_win_bar is not None and sell_win_bar is not None:
                if buy_win_bar < sell_win_bar:
                    return 2
                if sell_win_bar < buy_win_bar:
                    return 0
            return 1

        if buy_won:
            return 2
        if sell_won:
            return 0
        return 1

    async def _check_shadow_promotions(self) -> list[dict[str, Any]]:
        """Check if shadow models have completed trial period.

        Shadow models that have run for >= shadow_mode_days are evaluated
        on TWO independent gates:

        1. Backtest Sharpe — the walk-forward number stored at training
           time. Necessary but not sufficient: a model can backtest
           great and then collapse in production due to a regime shift
           or feature distribution drift.

        2. Live direction accuracy — accumulated from the shadow's
           scored predictions during the trial window. The shadow must
           track production within a small tolerance (5pp by default)
           so we don't promote a model whose live behaviour has already
           degraded. When production has no live data yet (new install)
           the live gate is skipped.

        Both must pass for promotion. Either failing → retire the
        shadow.
        """
        cfg = self.ctx.config.retraining
        shadow_models = await self.ctx.db.get_shadow_models_ready(cfg.shadow_mode_days)
        promotions = []

        for shadow in shadow_models:
            model_type = shadow["model_type"]
            current = await self.ctx.db.get_production_model(model_type)

            # Backtest Sharpe — necessary gate. Compare on the robust
            # bootstrapped lower bound (falls back to point Sharpe when a
            # legacy model on either side lacks it — see _decision_sharpe).
            shadow_sharpe, current_sharpe = _decision_sharpe(shadow, current)
            backtest_pass = shadow_sharpe >= current_sharpe

            # Live accuracy — sufficiency check on top. Skip when
            # production has no scored predictions yet (e.g., fresh
            # install / first promotion).
            shadow_live = await self.ctx.db.get_live_metrics_for_model(
                shadow["version"], days=cfg.shadow_mode_days,
            )
            current_live = (
                await self.ctx.db.get_live_metrics_for_model(
                    current["version"], days=cfg.shadow_mode_days,
                )
                if current else None
            )
            min_shadow_scored = 30  # need at least 30 scored predictions to trust the comparison
            live_pass = True
            live_reason = "no live data — backtest only"

            def _as_int(v: Any) -> int:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0

            def _as_float(v: Any) -> float:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return 0.0

            current_scored = _as_int(
                current_live.get("scored") if isinstance(current_live, dict) else 0
            )
            shadow_scored = _as_int(
                shadow_live.get("scored") if isinstance(shadow_live, dict) else 0
            )
            if current_scored >= min_shadow_scored:
                tolerance = 0.05  # 5pp
                if shadow_scored < min_shadow_scored:
                    live_pass = False
                    live_reason = (
                        f"only {shadow_scored} scored shadow predictions "
                        f"(need {min_shadow_scored}+)"
                    )
                else:
                    shadow_acc = _as_float(shadow_live.get("direction_accuracy"))
                    current_acc = _as_float(current_live.get("direction_accuracy"))
                    diff = shadow_acc - current_acc
                    live_pass = diff >= -tolerance
                    live_reason = (
                        f"shadow live acc {shadow_acc:.2%} vs "
                        f"production {current_acc:.2%} "
                        f"(diff {diff:+.2%}, tolerance ±{tolerance:.0%})"
                    )

            # Honest-edge gate — the model's untuned (argmax) Sharpe must
            # clear the floor. Blocks promoting a model whose backtest
            # profit lives entirely in a threshold-selected tail.
            edge_pass, edge_reason = passes_edge_gate(
                shadow, self.ctx.config.retraining.min_argmax_sharpe_for_promotion,
            )

            if backtest_pass and live_pass and edge_pass:
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
                    "shadow_live_accuracy": _as_float(
                        shadow_live.get("direction_accuracy")
                        if isinstance(shadow_live, dict) else 0
                    ),
                    "production_live_accuracy": (
                        _as_float(current_live.get("direction_accuracy"))
                        if isinstance(current_live, dict) else None
                    ),
                    "live_reason": live_reason,
                })
                logger.info(
                    "Promoted shadow model %s/%s (backtest Sharpe %.2f vs %.2f, %s)",
                    model_type, shadow["version"], shadow_sharpe, current_sharpe,
                    live_reason,
                )
            else:
                # Retire underperforming shadow
                await self.ctx.db.retire_model(model_type, shadow["version"])
                fail_reason_parts = []
                if not backtest_pass:
                    fail_reason_parts.append(
                        f"backtest Sharpe {shadow_sharpe:.2f} < {current_sharpe:.2f}"
                    )
                if not live_pass:
                    fail_reason_parts.append(f"live: {live_reason}")
                if not edge_pass:
                    fail_reason_parts.append(f"edge: {edge_reason}")
                promotions.append({
                    "model_type": model_type,
                    "version": shadow["version"],
                    "action": "retired",
                    "shadow_sharpe": shadow_sharpe,
                    "production_sharpe": current_sharpe,
                    "shadow_live_accuracy": _as_float(
                        shadow_live.get("direction_accuracy")
                        if isinstance(shadow_live, dict) else 0
                    ),
                    "production_live_accuracy": (
                        _as_float(current_live.get("direction_accuracy"))
                        if isinstance(current_live, dict) else None
                    ),
                    "live_reason": live_reason,
                    "failed_gates": fail_reason_parts,
                })
                logger.info(
                    "Retired shadow model %s/%s (%s)",
                    model_type, shadow["version"], " | ".join(fail_reason_parts),
                )

        return promotions
