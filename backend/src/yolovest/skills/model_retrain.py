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
from datetime import datetime, timedelta
from typing import Any

from yolovest.costs import round_trip_cost_floor_pct
from yolovest.data.features import (
    DAILY_TREND_FEATURE_KEYS,
    MODEL_FEATURE_EXCLUSIONS,
    IndicatorConfig,
    compute_features,
    compute_session_features,
    daily_trend_features_series,
    merge_feedback_features,
)
from yolovest.data.fno_features import FNO_FEATURE_KEYS, compute_fno_features
from yolovest.data.news_features import NEWS_FEATURE_KEYS, compute_news_features
from yolovest.data.vix_features import VIX_FEATURE_KEYS, compute_vix_features
from yolovest.models.schemas import OHLCVBar
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST

logger = logging.getLogger(__name__)


# Optional feature-group → feature-key sets, for the train-time
# feature_groups gate (config.strategy.feature_groups). Price/technical
# features are always kept (primary source of truth); these supporting
# groups can be excluded to train a leaner, price-primary model and verify
# they aren't diluting the core signal.
_FEATURE_GROUP_KEYS: dict[str, frozenset[str]] = {
    "regime": frozenset({"universe_breadth", "universe_avg_return"}),
    "sector": frozenset({"sector_breadth", "sector_avg_return", "relative_momentum"}),
    "institutional": frozenset({
        "bulk_deal_buy_5d", "bulk_deal_sell_5d", "bulk_deal_net_5d",
        "delivery_pct_avg_5d",
    }),
    "news": frozenset(NEWS_FEATURE_KEYS),
    "vix": frozenset(VIX_FEATURE_KEYS),
    "fno": frozenset(FNO_FEATURE_KEYS),
    "feedback": frozenset({
        "fb_pred_accuracy", "fb_pred_target_hit", "fb_pred_avg_pnl",
        "fb_dry_run_accuracy", "fb_dry_run_avg_move", "fb_trade_win_rate",
        "fb_trade_avg_pnl", "fb_trade_avg_slippage", "fb_recent_loss_count",
        "fb_has_data",
    }),
}


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


def intraday_triple_barrier_label(
    *,
    entry: float,
    entry_time: "datetime",
    horizon_minutes: int,
    target_pct: float,
    sl_pct: float,
    minute_bars: list["OHLCVBar"],
    start_idx: int,
) -> int:
    """Triple-barrier label whose target-before-SL ordering is resolved on
    the 1-MINUTE path.

    The trade decision and fill are at the 5-min scale — the caller passes
    ``entry`` (the entry 5-min bar's open, the earliest fillable price) and
    ``entry_time`` (that bar's start). But which barrier triggers first is
    walked bar-by-bar on the 1-min series, so the intra-5-min ambiguity
    ("did the high or the low print first inside the bar?") is decided by
    real finer-grained data instead of collapsing to HOLD the way a
    5-min-only walk must (high and low inside one 5-min bar are unordered).

    Discipline carried over:
      - **Hard same-session close-out**: the walk stops at the first 1-min
        bar whose date differs from the entry — no overnight carry (MIS).
      - **Clock-minute horizon** (`horizon_minutes`): a bar at/after
        ``entry_time + horizon`` ends the walk. Bar count differs between
        1-min and 5-min, so the horizon is expressed in minutes, not bars.
      - **Tie → SL**: when a single 1-min bar still straddles both a
        direction's target and stop, it counts as the stop (conservative;
        mirrors ``walk_forward_backtest`` so the label can't be gamed).
      - **First-winner disambiguation**: if both BUY and SELL would have
        won, the side that wins on the earlier 1-min bar takes the label;
        a genuine same-bar cross-direction tie is HOLD.

    ``start_idx`` is the index of the first 1-min bar at/after
    ``entry_time``. Returns: 2 BUY, 0 SELL, 1 HOLD.
    """
    from datetime import timedelta

    if start_idx >= len(minute_bars):
        return 1
    session_date = entry_time.date()
    deadline = entry_time + timedelta(minutes=horizon_minutes)

    buy_target = entry * (1 + target_pct)
    buy_sl = entry * (1 - sl_pct)
    sell_target = entry * (1 - target_pct)
    sell_sl = entry * (1 + sl_pct)

    buy_outcome: str | None = None
    sell_outcome: str | None = None
    buy_win_i: int | None = None
    sell_win_i: int | None = None

    for j in range(start_idx, len(minute_bars)):
        b = minute_bars[j]
        if b.timestamp.date() != session_date:
            break
        if b.timestamp >= deadline:
            break
        hi, lo = b.high, b.low

        if buy_outcome is None:
            target_now = hi >= buy_target
            sl_now = lo <= buy_sl
            if target_now and sl_now:
                buy_outcome = "loss"  # tie → SL
            elif target_now:
                buy_outcome = "win"
                buy_win_i = j
            elif sl_now:
                buy_outcome = "loss"

        if sell_outcome is None:
            target_now = lo <= sell_target
            sl_now = hi >= sell_sl
            if target_now and sl_now:
                sell_outcome = "loss"
            elif target_now:
                sell_outcome = "win"
                sell_win_i = j
            elif sl_now:
                sell_outcome = "loss"

        if buy_outcome is not None and sell_outcome is not None:
            break

    buy_won = buy_outcome == "win"
    sell_won = sell_outcome == "win"
    if buy_won and sell_won:
        if buy_win_i is not None and sell_win_i is not None:
            if buy_win_i < sell_win_i:
                return 2
            if sell_win_i < buy_win_i:
                return 0
        return 1
    if buy_won:
        return 2
    if sell_won:
        return 0
    return 1


# A cross-sectional rank needs breadth: dates with fewer valid forward
# returns than this label everything HOLD (no meaningful quantile exists
# over a handful of names — early-history dates, thin test fixtures).
_RELATIVE_MIN_NAMES = 10


def _assign_relative_labels(
    fwd_returns: list[float | None],
    dates: list[str],
    quantile: float,
    min_names: int = _RELATIVE_MIN_NAMES,
) -> list[int]:
    """Cross-sectional relative-momentum labels.

    Per trading date, rank every sample's forward return (entry next-open
    -> horizon close) across the universe: the top `quantile` become BUY
    (2), the bottom `quantile` SELL (0), the middle HOLD (1). Subtracting
    the cross-section's own move removes the market-drift component that
    dominates absolute barrier labels (a zero-skill pick wins ~40% of
    2:1-barrier trades in a rising market — the model was being graded
    against the tide, not the swimmers). Samples with no valid forward
    return, or on dates thinner than `min_names`, are HOLD.
    """
    by_date: dict[str, list[int]] = {}
    for idx, d in enumerate(dates):
        if fwd_returns[idx] is not None:
            by_date.setdefault(d, []).append(idx)

    labels = [1] * len(fwd_returns)
    for idxs in by_date.values():
        if len(idxs) < min_names:
            continue
        ranked = sorted(idxs, key=lambda i: fwd_returns[i])  # type: ignore[arg-type, return-value]
        k = max(1, int(len(ranked) * quantile))
        for i in ranked[-k:]:
            labels[i] = 2  # BUY: top-quantile relative performer
        for i in ranked[:k]:
            labels[i] = 0  # SELL: bottom-quantile relative performer
    return labels


def _index_bulk_deal_dates(
    bulk_deal_lookup: dict[tuple[str, str], dict[str, int]],
) -> dict[str, list[str]]:
    """Per-symbol sorted deal-date list for fast trailing-window lookups
    over the (symbol, deal_date) -> counts map. Shared by the daily and
    intraday matrix builders."""
    out: dict[str, list[str]] = {}
    for sym_key, date_key in bulk_deal_lookup:
        out.setdefault(sym_key, []).append(date_key)
    for v in out.values():
        v.sort()
    return out


def _parse_news_timelines(
    news_lookup: dict[str, list[tuple[str, str]]],
) -> dict[str, list[tuple[str, "datetime"]]]:
    """Parse each headline's published_at once (ISO -> aware-IST datetime)
    so the per-sample loops can window-slice the symbol's timeline without
    re-parsing. Unparseable rows are dropped. Shared by the daily and
    intraday matrix builders."""
    out: dict[str, list[tuple[str, datetime]]] = {}
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
            out[sym_key] = parsed
    return out


def _time_decay_multipliers(n: int, last_weight: float) -> list[float]:
    """Linear time-decay multipliers for `n` chronologically-ordered
    samples: index 0 (oldest) → `last_weight`, index n-1 (newest) → 1.0.
    `last_weight` >= 1.0 (or n <= 1) returns all-ones (decay disabled)."""
    if last_weight >= 1.0 or n <= 1:
        return [1.0] * max(0, n)
    span = n - 1
    return [last_weight + (1.0 - last_weight) * (i / span) for i in range(n)]


# Intraday MIS positions are squared off by session end (broker auto-squares
# at 15:30), so a 5-min entry's label/backtest path runs to the session close,
# not a fixed bar count. The full NSE session is 375 minutes (09:15-15:30);
# passing it as the horizon makes the same-session boundary in
# intraday_triple_barrier_label the binding stop = "to session close".
_INTRADAY_TO_CLOSE_HORIZON_MIN = 375

# 1-min bars for the whole intraday universe at once can exhaust available
# memory, so the intraday matrix is built in symbol chunks of this size.
_INTRADAY_SYMBOL_CHUNK = 15

# Sample a 5-min decision bar every 15 minutes (every 3rd bar), matching the
# live heartbeat cadence. Avoids training on heavily overlapping 5-min windows
# whose autocorrelated labels both inflate the walk-forward Sharpe and bloat
# the feature matrix (~3x fewer samples).
_INTRADAY_DECISION_STRIDE = 3


class ModelRetrainSkill(SkillBase):
    name = "model-retrain"
    description = "Retrain ML models, version artifacts, A/B test"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        return self.ctx.config.retraining.schedule_cron

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
        # caps history so the feature matrix fits in available RAM —
        # peak memory scales with days × symbols.
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

        # Training-data coverage summary — what history the models will
        # actually learn from. Timestamps are ISO strings so min/max are
        # lexical. Also report the thinnest symbol so survivorship / short
        # listings are visible.
        _all_bars = training_data.get("bars", [])
        if _all_bars:
            _ts = [r["timestamp"] for r in _all_bars if r.get("timestamp")]
            _lo, _hi = (min(_ts), max(_ts)) if _ts else ("?", "?")
            _per_sym: dict[str, int] = {}
            for r in _all_bars:
                s = r.get("symbol")
                if s:
                    _per_sym[s] = _per_sym.get(s, 0) + 1
            _counts = sorted(_per_sym.values())
            _median = _counts[len(_counts) // 2] if _counts else 0
            _yrs = (max(_counts) / 252.0) if _counts else 0.0
            logger.info(
                "Training data: %d daily bars | %d symbols | %s -> %s "
                "(deepest ~%.1f yrs) | bars/symbol min=%d median=%d max=%d | "
                "max_training_days=%d, min_training_samples=%d",
                len(_all_bars), len(unique_symbols), str(_lo)[:10], str(_hi)[:10],
                _yrs, _counts[0] if _counts else 0, _median,
                _counts[-1] if _counts else 0,
                cfg.max_training_days, min_samples,
            )
        else:
            logger.warning("Training data: 0 bars loaded — nothing to train on")

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

        # Swing lookahead: 10 daily bars (~2 weeks) gives genuine swing
        # setups enough room for the 1.5×ATR target to develop without the
        # 0.75×ATR SL noise-tripping on the same window — at 5 bars the SL
        # fires constantly and the labeler classes most outcomes as HOLD
        # even after the first-winner disambiguation.
        #
        # The intraday model doesn't use a bar-lookahead for its LABEL (it
        # walks the 1-min path to the session close — see
        # _build_intraday_matrix), but it still needs a lookahead value as
        # the CV purge gap: its labels resolve within the entry day, so 1
        # trading day is the right gap to drop train rows whose label window
        # overlaps the test fold (ml_signal converts it to calendar days).
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

        # Log the exact feature-group configuration this retrain will use,
        # so the artifact's feature set is never a mystery. Price/technical
        # features are always trained on; these support groups are toggled
        # via config.strategy.feature_groups.
        _fg = getattr(self.ctx.config.strategy, "feature_groups", None)
        if _fg is not None:
            _fg_state = " ".join(
                f"{g}={'ON' if getattr(_fg, g, True) else 'OFF'}"
                for g in _FEATURE_GROUP_KEYS
            )
            logger.info(
                "Retrain feature groups (price/technical always ON): %s", _fg_state,
            )

        for model_type in ("intraday", "swing"):
            # Build feature matrix with model-specific labeling + feedback features
            target_mult, sl_mult = atr_mult_map[model_type]
            # Cost-aware label floor: the triple-barrier target must clear the
            # round-trip cost + slippage of the product this model trades
            # (MIS for intraday, CNC for swing), else a "win" is a net loss.
            # Computed once per model from the same cost model the backtest
            # uses; 0 disables it (legacy gross-return labels).
            label_product = "MIS" if model_type == "intraday" else "CNC"
            cost_floor = 0.0
            if self.ctx.config.strategy.label_cost_floor_enabled:
                cost_floor = round_trip_cost_floor_pct(
                    label_product, self.ctx.config.transaction_costs,
                )
            # Bound for BOTH branches: intraday uses it only as the CV purge
            # gap (its label walks to session close, not a bar count); swing
            # uses it as both the label lookahead and the purge gap.
            lookahead = lookahead_map[model_type]
            if model_type == "intraday":
                # The intraday model trains on 5-min decision bars with 1-min
                # triple-barrier label resolution, walked to the session close
                # (MIS auto-squares EOD). The old daily-bar "intraday" model
                # (1-day lookahead) was really a next-day predictor with no
                # real intraday edge.
                intraday_label_mode = str(
                    getattr(self.ctx.config.strategy, "intraday_label_mode",
                            "triple_barrier")
                )
                logger.info(
                    "=== Retraining intraday (5-min) model: label=%s "
                    "(1-min path), to-session-close horizon, "
                    "exit geometry target=%.2f×ATR, SL=%.2f×ATR ===",
                    intraday_label_mode, target_mult, sl_mult,
                )
                X, y, feat_names, sample_weights, bars_meta = (
                    await self._build_intraday_matrix(
                        training_data,
                        horizon_minutes=_INTRADAY_TO_CLOSE_HORIZON_MIN,
                        target_atr_mult=target_mult, sl_atr_mult=sl_mult,
                        cost_floor_pct=cost_floor,
                        label_mode=intraday_label_mode,
                        feedback_data=feedback_data, sector_map=sector_map,
                        bulk_deal_lookup=bulk_deal_lookup, news_lookup=news_lookup,
                        vix_timeline=vix_timeline, fno_lookup=fno_lookup,
                    )
                )
            else:
                swing_label_mode = str(
                    getattr(self.ctx.config.strategy, "swing_label_mode", "barrier")
                )
                logger.info(
                    "=== Retraining %s model: label=%s, lookahead=%d bars, "
                    "exit geometry target=%.2f×ATR, SL=%.2f×ATR ===",
                    model_type, swing_label_mode, lookahead, target_mult, sl_mult,
                )
                X, y, feat_names, sample_weights, bars_meta = self._prepare_training_data(
                    training_data, lookahead_bars=lookahead, feedback_data=feedback_data,
                    target_atr_mult=target_mult, sl_atr_mult=sl_mult,
                    cost_floor_pct=cost_floor,
                    label_mode=swing_label_mode,
                    sector_map=sector_map,
                    bulk_deal_lookup=bulk_deal_lookup,
                    news_lookup=news_lookup,
                    vix_timeline=vix_timeline,
                    fno_lookup=fno_lookup,
                )
            # Spell out exactly what this model trained on: total feature
            # count + which support groups actually landed in the matrix
            # (so a disabled/empty group is visibly absent).
            _present = {
                g for g, keys in _FEATURE_GROUP_KEYS.items()
                if any(k in feat_names for k in keys)
            }
            _absent = [g for g in _FEATURE_GROUP_KEYS if g not in _present]
            logger.info(
                "%s training matrix: %d features | support groups present: %s | "
                "absent: %s",
                model_type, len(feat_names),
                sorted(_present) or "none", _absent or "none",
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
                for lbl, lbl_key in key_for.items():
                    c = label_counts.get(lbl_key, 0)
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

            # Time-decay weighting (opt-in). sample_weights is already in
            # chronological order (the builders sort by entry_date), so the
            # list index is the chronological rank. Multiplies on top of the
            # per-bar feedback boost + class weights.
            _last_w = float(
                getattr(self.ctx.config.strategy, "time_decay_last_weight", 1.0)
            )
            if _last_w < 1.0 and len(sample_weights) > 1:
                _decay = _time_decay_multipliers(len(sample_weights), _last_w)
                sample_weights = [
                    sw * d
                    for sw, d in zip(sample_weights, _decay, strict=False)
                ]
                logger.info(
                    "Time-decay weights for %s: oldest×%.2f → newest×1.00",
                    model_type, _last_w,
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
                # The live book cannot act on swing SELLs: shorts on
                # non-held names are MIS-only (no overnight retail
                # shorting) and get dropped at swing horizons, while
                # exits on held names belong to position-monitor. A
                # backtest that books SELL trades therefore measures an
                # edge the account can't trade — evaluate the swing lane
                # long-only so its Sharpe describes reality.
                train_params["backtest_long_only"] = model_type == "swing"
                # Bound the backtest's concurrent-positions count to
                # the same cap the live engine enforces. Without
                # this, the simulator treats every signal as
                # independently fillable and inflates Sharpe (e.g.
                # 12.98 intraday on the user's last run).
                train_params["backtest_max_positions"] = (
                    self.ctx.config.risk.max_open_positions
                )
                # train() consumes X in place (it frees the list once the
                # numpy matrix is built, to bound peak memory) — capture
                # the post-train guard's evaluation slice BEFORE training.
                # The slice shares row objects with X, so it survives the
                # outer list being cleared. Without this the guard scored
                # an empty matrix and its crash was swallowed: the
                # silent-model check never actually ran.
                guard_x: list[list[float]] = (
                    X[-1000:]
                    if self.ctx.config.strategy.post_train_class_check_enabled
                    else []
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
                # Honest-data caveat, stamped into the artifact metrics:
                # the corpus is whatever history this install accumulated
                # for CURRENT constituents — names that exited the index
                # or delisted before ingestion are absent, so the
                # cross-sectional features and the backtest Sharpe skew
                # optimistic (survivorship). No point-in-time constituent
                # source is wired; treat absolute backtest numbers
                # accordingly.
                metrics["data_caveats"] = ["survivor_universe"]
                metrics["label_mode"] = (
                    intraday_label_mode if model_type == "intraday"
                    else swing_label_mode
                )
                if class_weights:
                    metrics["class_weights"] = {
                        "BUY": round(class_weights.get(2, 0.0), 4),
                        "HOLD": round(class_weights.get(1, 0.0), 4),
                        "SELL": round(class_weights.get(0, 0.0), 4),
                    }
                # Post-train guard: run the fresh model on the most recent
                # N training rows through the FULL PRODUCTION PATH
                # (calibration + tuned thresholds) — not the raw booster
                # argmax — and verify it still produces a non-trivial
                # non-HOLD signal rate. The raw-argmax check passes even
                # when the deployed model fires ~zero signals live (the
                # silent-model failure: thresholds unreachable after
                # calibration). This catches that end-to-end.
                if self.ctx.config.strategy.post_train_class_check_enabled:
                    try:
                        # The freshest N rows — what the production model
                        # sees first in live use. Captured before train()
                        # (which consumes X) — see guard_x above.
                        n_check = len(guard_x)
                        prod_labels = self.ctx.ml.predict_labels_batch(
                            guard_x, model_type,
                        )
                        pred_counts = {0: 0, 1: 0, 2: 0}
                        for p in prod_labels:
                            pred_counts[int(p)] = pred_counts.get(int(p), 0) + 1
                        # Map: 0=SELL, 1=HOLD, 2=BUY.
                        pred_dist = {
                            "SELL": pred_counts.get(0, 0),
                            "HOLD": pred_counts.get(1, 0),
                            "BUY": pred_counts.get(2, 0),
                        }
                        n_eval = len(prod_labels) or 1
                        # Tradeability-aware rate: swing SELLs are no-ops
                        # live (non-held shorts dropped; held-name exits
                        # belong to position-monitor), so a SELL-heavy
                        # swing model must not pass as "non-silent".
                        # Intraday can short, so both sides count there.
                        tradeable = pred_dist["BUY"] + (
                            pred_dist["SELL"] if model_type == "intraday" else 0
                        )
                        signal_rate = tradeable / n_eval
                        logger.info(
                            "Post-train production-path distribution for %s "
                            "(n=%d): BUY=%d, HOLD=%d, SELL=%d (signal_rate=%.2f%%)",
                            model_type, n_check,
                            pred_dist["BUY"], pred_dist["HOLD"], pred_dist["SELL"],
                            signal_rate * 100,
                        )
                        metrics["post_train_pred_dist"] = pred_dist
                        metrics["post_train_signal_rate"] = round(signal_rate, 4)

                        min_rate = self.ctx.config.strategy.post_train_min_signal_rate
                        if min_rate > 0 and signal_rate < min_rate:
                            msg = (
                                f"Refusing to save {model_type}: through the "
                                f"production path (calibration + tuned "
                                f"thresholds) it signals on only "
                                f"{signal_rate * 100:.2f}% of the most recent "
                                f"{n_check} samples (< {min_rate * 100:.2f}% "
                                f"floor) — it would be near-silent live. "
                                f"Thresholds are likely unreachable; check "
                                f"the tuned cutoffs / tuned_min_signal_rate."
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
                                "post_train_signal_rate": round(signal_rate, 4),
                                "label_pct": label_pct,
                            }
                            continue
                    except Exception:
                        # Inference inside the guard shouldn't crash the
                        # retrain — fall through and save the model. But
                        # a guard that can't run is a real degradation
                        # (the silent-model check is the last gate before
                        # an unvetted artifact ships), so say it loudly.
                        logger.warning(
                            "Post-train signal-rate check CRASHED for %s — "
                            "saving the model without the silent-model "
                            "guard. Investigate before trusting this "
                            "artifact.",
                            model_type, exc_info=True,
                        )
                        try:
                            await self.ctx.notify.send(
                                f"Model retrain: post-train guard crashed "
                                f"for {model_type}; model saved WITHOUT the "
                                f"silent-model check.",
                                alert_type="errors",
                            )
                        except Exception:
                            logger.debug(
                                "Failed to notify on guard crash",
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
                    model_type, version, f"models/{version}.pkl", metrics
                )

                # Step 5: Compare with production on the robust
                # (bootstrapped lower-bound) Sharpe, not the noisy
                # single-holdout point estimate.
                current = await self.ctx.db.get_production_model(model_type)
                cand_sharpe, current_sharpe = _decision_sharpe(metrics, current)

                improved = cand_sharpe > current_sharpe
                if improved:
                    shadow_deployed.append(model_type)

                # Step 6: registry-honouring deployment. train() leaves
                # the candidate in the live production slots (save_model
                # and the post-train guard need it there), but the
                # REGISTRY decides what trades: the candidate's row is
                # 'shadow' until the promotion gates pass. Restore the
                # incumbent to the production slots and start the
                # candidate's A/B trial in the shadow slot immediately
                # (no restart needed). First-ever train (no production
                # row) bootstraps: promote the candidate directly — a
                # system with no model can't shadow-test.
                deployed_as = "shadow"
                if current is None:
                    # Bootstrap still has to clear the honest-edge gate:
                    # "no incumbent" means the lane is PARKED, not that
                    # any candidate deserves production. A negative-argmax
                    # model promoted here would go live the moment the
                    # user re-enables the lane via strategy.mode — the
                    # exact unvetted-deployment path this system exists
                    # to close. Refused candidates stay shadow-only (the
                    # normal evaluation cycle retires them) and the live
                    # slot is cleared so a mode flip can't trade them.
                    edge_ok, edge_reason = passes_edge_gate(
                        metrics,
                        self.ctx.config.retraining.min_argmax_sharpe_for_promotion,
                    )
                    if not edge_ok:
                        deployed_as = "shadow (bootstrap refused: no honest edge)"
                        logger.warning(
                            "Bootstrap promotion REFUSED for %s/%s: %s. "
                            "Lane stays parked; candidate saved as shadow "
                            "only.",
                            model_type, version, edge_reason,
                        )
                        try:
                            await self.ctx.notify.send(
                                f"Model retrain: {model_type} candidate "
                                f"{version} was NOT promoted (no incumbent, "
                                f"but {edge_reason}). The lane stays parked.",
                                alert_type="errors",
                            )
                        except Exception:
                            logger.debug(
                                "bootstrap-refusal notify failed",
                                exc_info=True,
                            )
                        try:
                            await self.ctx.ml.load_shadow_model(
                                model_type, version,
                            )
                        except Exception:
                            logger.debug(
                                "shadow load after bootstrap refusal failed",
                                exc_info=True,
                            )
                        # train() left the candidate in the live slot and
                        # there is no incumbent to restore — clear it.
                        try:
                            self.ctx.ml.clear_model(model_type)
                        except Exception:
                            logger.debug(
                                "clear_model after bootstrap refusal failed",
                                exc_info=True,
                            )
                        results[model_type] = {
                            "version": version,
                            "metrics": metrics,
                            "improved": improved,
                            "deployed_as": deployed_as,
                        }
                        continue
                    try:
                        await self.ctx.db.promote_model(model_type, version)
                        deployed_as = "production (bootstrap)"
                        logger.info(
                            "No production %s model in the registry — "
                            "promoted %s directly (bootstrap, %s).",
                            model_type, version, edge_reason,
                        )
                        try:
                            await self.ctx.notify.send(
                                f"Model retrain: {model_type} model {version} "
                                f"passed its gates and was promoted to "
                                f"production (no incumbent). If this lane was "
                                f"parked via strategy.mode, it can be "
                                f"re-enabled now.",
                                alert_type="daily_summary",
                            )
                        except Exception:
                            logger.debug(
                                "bootstrap-promotion notify failed",
                                exc_info=True,
                            )
                    except Exception:
                        logger.warning(
                            "Bootstrap promotion failed for %s/%s",
                            model_type, version, exc_info=True,
                        )
                else:
                    try:
                        await self.ctx.ml.load_shadow_model(model_type, version)
                    except Exception:
                        logger.warning(
                            "Failed to load candidate %s/%s into the shadow "
                            "slot — its A/B trial starts at next restart.",
                            model_type, version, exc_info=True,
                        )
                    try:
                        await self.ctx.ml.load_model(
                            model_type, str(current["version"]),
                        )
                        logger.info(
                            "Restored production %s model %s to the live "
                            "slots; candidate %s runs as shadow.",
                            model_type, current["version"], version,
                        )
                    except Exception:
                        deployed_as = "production (incumbent restore failed)"
                        logger.warning(
                            "Could not restore production %s model %s — the "
                            "fresh candidate %s stays in the live slots so "
                            "trading continues. Re-promote or retrain to "
                            "restore registry state.",
                            model_type, current.get("version"), version,
                            exc_info=True,
                        )
                        try:
                            await self.ctx.notify.send(
                                f"Model retrain: failed to restore production "
                                f"{model_type} model "
                                f"{current.get('version')}; unvetted candidate "
                                f"{version} is live until fixed.",
                                alert_type="errors",
                            )
                        except Exception:
                            logger.debug(
                                "Failed to notify on restore failure",
                                exc_info=True,
                            )

                results[model_type] = {
                    "version": version,
                    "metrics": metrics,
                    "improved": improved,
                    "deployed_as": deployed_as,
                }
            except Exception as e:
                logger.warning("Retrain failed for %s: %s", model_type, e)
                results[model_type] = {"error": str(e)}
                try:
                    await self.ctx.notify.send(
                        f"Model retrain FAILED for {model_type}: {e}",
                        alert_type="errors",
                    )
                except Exception:
                    logger.debug(
                        "Failed to notify on retrain failure", exc_info=True,
                    )
            # Free per-model scratch (feature matrix + bars_meta) before
            # the next model's _prepare_training_data allocates its
            # own copy. Without this the intraday and swing matrices
            # would briefly coexist and double peak memory.
            X = y = feat_names = sample_weights = bars_meta = None  # type: ignore[assignment]
            _gc.collect()

        # Free training_data eagerly — _check_shadow_promotions doesn't
        # need it and it's the largest single resident structure
        # (~365K rows × 8 fields at the default 730-day cap, much more
        # if the user raised retraining.max_training_days).
        training_data = None
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

        # A run where NO model shipped is a failed retrain — the stale
        # incumbent keeps trading (deliberately: keep trading, alert),
        # but the audit log must say the retrain produced nothing.
        # Partial success (e.g. intraday lane short on 1-min data while
        # swing trained fine) stays a success.
        if not any_success:
            error_summary = "; ".join(
                f"{mt}: {r.get('error', 'unknown')}"
                for mt, r in results.items()
                if isinstance(r, dict)
            ) or "no models attempted"
            logger.warning(
                "model-retrain produced no new model (%s) — the existing "
                "production model keeps trading.",
                error_summary,
            )
            return SkillResult(
                success=False,
                skill_name=self.name,
                error=error_summary,
                data={
                    "models": results,
                    "shadow_deployed": shadow_deployed,
                    "promotions": promotions,
                    "failure_analysis_generated": failure_analysis is not None,
                },
            )

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
        cost_floor_pct: float = 0.0,
        label_mode: str = "barrier",
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
            # Daily/swing path → extended momentum on (config-toggled).
            extended_momentum=self.ctx.config.strategy.indicators.extended_momentum,
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
        # Relative-label mode: per-sample forward return (entry next-open
        # -> horizon close), labelled cross-sectionally AFTER the global
        # date sort below. Parallel to X/y; None = no valid entry.
        rel_fwd_returns: list[float | None] = []

        # Train-time feature-group gate. Price/technical features always
        # stay; disabled support groups (config.strategy.feature_groups)
        # are excluded from the feature matrix so the model trains
        # price-primary. Folded into MODEL_FEATURE_EXCLUSIONS so the filter
        # below is a single check.
        _fg = getattr(self.ctx.config.strategy, "feature_groups", None)
        _disabled_keys: set[str] = set()
        if _fg is not None:
            for _group, _keys in _FEATURE_GROUP_KEYS.items():
                if not getattr(_fg, _group, True):
                    _disabled_keys |= set(_keys)
        excluded_keys = set(MODEL_FEATURE_EXCLUSIONS) | _disabled_keys
        if _disabled_keys:
            logger.info(
                "Feature groups DISABLED for training: %s — excluding %d "
                "support features (price/technical core retained)",
                [g for g in _FEATURE_GROUP_KEYS if not getattr(_fg, g, True)],
                len(_disabled_keys),
            )
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
        bulk_dates_by_sym = _index_bulk_deal_dates(bulk_deal_lookup)
        news_lookup = news_lookup or {}
        news_parsed_by_sym = _parse_news_timelines(news_lookup)

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

                # EOD-PUBLISHED broadcast data (bulk deals, delivery %,
                # VIX, F&O) is merged AS-OF THE PRIOR SESSION (bars[i-1])
                # — the daily-lane mirror of the intraday lane's
                # _merge_daily_broadcast. The heartbeat runs mid-session,
                # BEFORE the day's EOD publications and ingests land
                # (bulk deals + delivery % publish after close,
                # ingest-vix runs 16:00, ingest-fno 18:30), so the
                # freshest value live inference can ever see is the
                # prior session's. Training on same-day EOD values
                # taught a lag-0 relationship that serving could only
                # feed at lag-1 — a systematic train/serve skew.
                # bars[i].timestamp is a datetime; the bulk_deals table
                # stores deal_date as YYYY-MM-DD strings, so format
                # consistently before the lookup. i >= window_size=200,
                # so bars[i-1] / bars[i-2] always exist.
                _sample_date = bars[i].timestamp.strftime("%Y-%m-%d")
                _prior_date = bars[i - 1].timestamp.strftime("%Y-%m-%d")
                # Bulk deals: 5 sessions ending at the PRIOR session.
                _bulk_window_start = bars[max(0, i - 5)].timestamp.strftime("%Y-%m-%d")
                _bd_dates = bulk_dates_by_sym.get(sym, [])
                _bd_buy = _bd_sell = 0
                for d in _bd_dates:
                    if d > _prior_date:
                        break
                    if d >= _bulk_window_start:
                        counts = bulk_deal_lookup.get((sym, d), {})
                        _bd_buy += counts.get("buy", 0)
                        _bd_sell += counts.get("sell", 0)
                features["bulk_deal_buy_5d"] = float(_bd_buy)
                features["bulk_deal_sell_5d"] = float(_bd_sell)
                features["bulk_deal_net_5d"] = float(_bd_buy - _bd_sell)

                # delivery_pct rolling-5 average over the 5 sessions
                # ENDING AT bars[i-1] — today's delivery % isn't
                # published until after the close. Rows ingested before
                # migration 038 have NULL → treated as missing.
                _delivery_values: list[float] = []
                for k in range(max(0, i - 5), i):
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

                # News-sentiment features, windowed to the ENTRY bar's
                # timestamp (midnight opening the entry day): live
                # inference reads news up to the moment of entry, so
                # cutting training at bars[i]'s own midnight (the old
                # behaviour) excluded the decision day's headlines the
                # live model does see — day-i news is public well before
                # the day-i+1 open, so this is lag-aligned and leak-free.
                # Bar timestamps in training_data are naive; coerce to
                # IST to match the parsed published_at tz.
                _news_cutoff = bars[i + 1].timestamp
                if _news_cutoff.tzinfo is None:
                    _news_cutoff = _news_cutoff.replace(tzinfo=IST)
                _sym_news = news_parsed_by_sym.get(sym)
                if _sym_news:
                    news_feats = compute_news_features(_sym_news, _news_cutoff)
                else:
                    news_feats = {k: 0.0 for k in NEWS_FEATURE_KEYS}
                features.update(news_feats)

                # India VIX regime features as-of the PRIOR session —
                # the day-i VIX close lands in the DB at 16:00, after
                # any heartbeat that could trade on it. Single broadcast
                # series; compute_vix_features slices the trailing window.
                if vix_timeline:
                    vix_feats = compute_vix_features(vix_timeline, _prior_date)
                else:
                    vix_feats = {k: 0.0 for k in VIX_FEATURE_KEYS}
                features.update(vix_feats)

                # F&O derivatives features as-of the PRIOR session
                # (ingest-fno runs 18:30). The oi_buildup price pair is
                # the prior session's move — (close[i-2], close[i-1]) —
                # matching what inference derives from a daily window
                # ending at the last completed session. Only
                # F&O-eligible symbols have rows; misses return
                # is_fno_stock=0 and the model learns to weight these
                # features only when present.
                _sym_fno = (fno_lookup or {}).get(sym)
                if _sym_fno:
                    fno_feats = compute_fno_features(
                        _sym_fno, _prior_date,
                        prior_stock_close=bars[i - 2].close,
                        current_stock_close=bars[i - 1].close,
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
                # Cost-aware target: a win must clear round-trip costs, else
                # it's a net loss. Floor leaves the swing geometry unchanged
                # whenever the ATR target already exceeds costs. The same
                # value flows into bars_meta so the backtest exits at the
                # labelled barrier.
                eff_target_pct = max(atr_pct * target_atr_mult, cost_floor_pct)
                if label_mode == "relative":
                    # Cross-sectional label, assigned after the global
                    # date sort (needs every symbol's same-date forward
                    # return). Placeholder HOLD here; the ATR target/SL
                    # still flow into bars_meta so the backtest exits at
                    # the LIVE trade geometry — which also breaks the
                    # label/exit circularity of barrier mode.
                    label = 1
                    rel_fwd_returns.append(
                        (future_close / next_open - 1.0)
                        if next_open > 0 else None
                    )
                elif next_open <= 0 or atr_pct <= 0:
                    label = 1
                else:
                    label = self._path_aware_label(
                        bars=bars,
                        start_idx=i,
                        lookahead=lookahead_bars,
                        entry=next_open,
                        target_pct=eff_target_pct,
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
                for feat_key in features:
                    if feat_key in excluded_keys:
                        continue
                    if feat_key not in feature_names_set:
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
                                feat_key, len(X), sym, len(X),
                            )
                        feature_names.append(feat_key)
                        feature_names_set.add(feat_key)
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
                    "target_pct": float(eff_target_pct),
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
            if label_mode == "relative" and rel_fwd_returns:
                rel_fwd_returns = [rel_fwd_returns[i] for i in order]

        if label_mode == "relative" and rel_fwd_returns:
            y = _assign_relative_labels(
                rel_fwd_returns,
                [str(m.get("entry_date", "")) for m in bars_meta],
                quantile=float(
                    getattr(self.ctx.config.strategy,
                            "relative_label_quantile", 0.20)
                ),
            )

        return X, y, feature_names, sample_weights, bars_meta

    def _prepare_intraday_training_data(
        self,
        intraday_data: dict[str, Any],
        daily_data: dict[str, Any],
        *,
        horizon_minutes: int,
        target_atr_mult: float,
        sl_atr_mult: float,
        cost_floor_pct: float = 0.0,
        label_mode: str = "triple_barrier",
        feedback_data: dict[str, Any] | None = None,
        sector_map: dict[str, str] | None = None,
        bulk_deal_lookup: dict[tuple[str, str], dict[str, int]] | None = None,
        news_lookup: dict[str, list[tuple[str, str]]] | None = None,
        vix_timeline: list[tuple[str, float]] | None = None,
        fno_lookup: dict[str, list[tuple[str, dict[str, float]]]] | None = None,
    ) -> tuple[
        list[list[float]], list[int], list[str], list[float], list[dict[str, Any]]
    ]:
        """Build the 5-min intraday training matrix.

        Two timeframes, two jobs:
          - **Features + entry** are computed on the 5-min *decision* bars
            (`intraday_data["decision_bars"]`). Entry is the next 5-min
            bar's open (the earliest fillable price), same rule as daily.
          - **Labels** are resolved on the 1-min *path* bars
            (`intraday_data["minute_bars"]`) via
            ``intraday_triple_barrier_label`` — so target-before-SL ordering
            inside each 5-min bar is decided by real finer data, not HOLD.

        Daily-broadcast features (universe/sector regime, VIX, F&O, bulk
        deals, delivery%) are merged **as-of the prior session** — the most
        recent daily date strictly before the bar's date — because at, say,
        09:35 the same day's EOD aggregates don't exist yet. Using them
        would be lookahead leakage that inflates the offline Sharpe and
        evaporates live. News stays timestamp-windowed (leak-free as-of the
        actual intraday moment), and minutes_since_open / day_phase finally
        vary intra-session. ``daily_data`` supplies the prior-session
        context; ``intraday_data`` supplies the bars we actually label.

        Returns (X, y, feature_names, sample_weights, bars_meta), globally
        sorted by entry_date so the walk-forward CV splits by time.
        """
        import bisect

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
            # Intraday 5-min bars → daily-horizon momentum is meaningless,
            # so the extended-momentum block stays OFF here.
            extended_momentum=False,
        )
        window_size = 200

        _fg = getattr(self.ctx.config.strategy, "feature_groups", None)
        _disabled_keys: set[str] = set()
        if _fg is not None:
            for _group, _keys in _FEATURE_GROUP_KEYS.items():
                if not getattr(_fg, _group, True):
                    _disabled_keys |= set(_keys)
        excluded_keys = set(MODEL_FEATURE_EXCLUSIONS) | _disabled_keys

        sector_map = sector_map or {}
        bulk_deal_lookup = bulk_deal_lookup or {}
        news_lookup = news_lookup or {}
        fno_lookup = fno_lookup or {}

        # ---- Prior-session daily context (built from daily bars) ----
        daily_rows = daily_data.get("bars", [])
        daily_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for r in daily_rows:
            daily_by_symbol.setdefault(r["symbol"], []).append(r)

        # Per (symbol, date) higher-timeframe trend context, precomputed in
        # one EMA pass per symbol. _merge_daily_broadcast looks this up by the
        # PRIOR session date, so the intraday feature uses only completed
        # daily sessions (no intraday-day lookahead) — symmetric with the
        # inference path in generate_signals.
        daily_trend_by_sym: dict[str, dict[str, dict[str, float]]] = {}
        for _sym, _rows in daily_by_symbol.items():
            _sorted = sorted(_rows, key=lambda r: str(r["timestamp"]))
            _closes: list[float] = []
            _dates: list[str] = []
            for _r in _sorted:
                try:
                    _closes.append(float(_r["close"]))
                    _dates.append(str(_r["timestamp"])[:10])
                except (TypeError, ValueError):
                    pass
            if len(_closes) < 21:
                continue
            _series = daily_trend_features_series(_closes)
            daily_trend_by_sym[_sym] = dict(zip(_dates, _series, strict=False))

        regime_by_ts = self._compute_regime_index(daily_by_symbol)
        sector_regime, symbol_returns = self._compute_sector_index(
            daily_by_symbol, sector_map,
        )
        # Sorted universe of daily dates for "prior session" resolution.
        daily_dates_sorted = sorted(regime_by_ts.keys())
        # Per-symbol daily close + delivery, keyed by date.
        daily_close_by_sym: dict[str, dict[str, float]] = {}
        daily_delivery_by_sym: dict[str, dict[str, float]] = {}
        for r in daily_rows:
            s = r["symbol"]
            d = str(r["timestamp"])[:10]
            try:
                daily_close_by_sym.setdefault(s, {})[d] = float(r["close"])
            except (TypeError, ValueError):
                pass
            dp = r.get("delivery_pct")
            if dp is not None:
                try:
                    daily_delivery_by_sym.setdefault(s, {})[d] = float(dp)
                except (TypeError, ValueError):
                    pass

        def _prior_session(date_str: str) -> str | None:
            idx = bisect.bisect_left(daily_dates_sorted, date_str)
            return daily_dates_sorted[idx - 1] if idx > 0 else None

        def _window_dates(end_date: str, n: int) -> list[str]:
            """Up to `n` daily dates ending at (and including) end_date."""
            hi = bisect.bisect_right(daily_dates_sorted, end_date)
            return daily_dates_sorted[max(0, hi - n):hi]

        # Bulk-deal date index + parsed news timeline (shared helpers).
        bulk_dates_by_sym = _index_bulk_deal_dates(bulk_deal_lookup)
        news_parsed_by_sym = _parse_news_timelines(news_lookup)

        def _merge_daily_broadcast(features: dict[str, Any], sym: str, bar_ts: datetime) -> None:
            """Merge prior-session daily features into `features` in place."""
            prev = _prior_session(bar_ts.strftime("%Y-%m-%d"))

            # Higher-timeframe daily trend (always-on core feature — not in
            # any optional group). Keyed by the prior completed session.
            dt = daily_trend_by_sym.get(sym, {}).get(prev) if prev else None
            features.update(dt if dt else {k: 0.0 for k in DAILY_TREND_FEATURE_KEYS})

            reg = regime_by_ts.get(prev) if prev else None
            features["universe_breadth"] = reg["breadth"] if reg else 0.5
            features["universe_avg_return"] = reg["avg_return"] if reg else 0.0

            sec = sector_map.get(sym)
            sec_stats = sector_regime.get((sec, prev)) if (sec and prev) else None
            stock_ret = symbol_returns.get((sym, prev)) if prev else None
            if sec_stats and stock_ret is not None:
                features["sector_breadth"] = sec_stats["breadth"]
                features["sector_avg_return"] = sec_stats["avg_return"]
                features["relative_momentum"] = stock_ret - sec_stats["avg_return"]
            else:
                features["sector_breadth"] = 0.5
                features["sector_avg_return"] = 0.0
                features["relative_momentum"] = 0.0

            bd_buy = bd_sell = 0
            if prev:
                win = _window_dates(prev, 5)
                win_lo = win[0] if win else prev
                for d in bulk_dates_by_sym.get(sym, []):
                    if d > prev:
                        break
                    if d >= win_lo:
                        counts = bulk_deal_lookup.get((sym, d), {})
                        bd_buy += counts.get("buy", 0)
                        bd_sell += counts.get("sell", 0)
            features["bulk_deal_buy_5d"] = float(bd_buy)
            features["bulk_deal_sell_5d"] = float(bd_sell)
            features["bulk_deal_net_5d"] = float(bd_buy - bd_sell)

            deliveries: list[float] = []
            if prev:
                sym_deliv = daily_delivery_by_sym.get(sym, {})
                for d in _window_dates(prev, 5):
                    if d in sym_deliv:
                        deliveries.append(sym_deliv[d])
            features["delivery_pct_avg_5d"] = (
                sum(deliveries) / len(deliveries) if deliveries else 0.0
            )

            news_ts = bar_ts if bar_ts.tzinfo else bar_ts.replace(tzinfo=IST)
            sym_news = news_parsed_by_sym.get(sym)
            features.update(
                compute_news_features(sym_news, news_ts) if sym_news
                else {k: 0.0 for k in NEWS_FEATURE_KEYS}
            )

            features.update(
                compute_vix_features(vix_timeline, prev) if (vix_timeline and prev)
                else {k: 0.0 for k in VIX_FEATURE_KEYS}
            )

            sym_fno = fno_lookup.get(sym)
            if sym_fno and prev:
                closes = daily_close_by_sym.get(sym, {})
                win = _window_dates(prev, 2)
                prior_c = closes.get(win[0]) if len(win) >= 2 else None
                features.update(compute_fno_features(
                    sym_fno, prev,
                    prior_stock_close=prior_c,
                    current_stock_close=closes.get(prev),
                ))
            else:
                features.update({k: 0.0 for k in FNO_FEATURE_KEYS})

        # ---- Group 5-min decision bars + index 1-min path bars per symbol ----
        # Normalize every timestamp to naive IST wall-clock (mirrors
        # data/db._canonical_ohlcv_ts) and dedupe per instant. Legacy rows
        # predating that canonicaliser left the same 5-min bar stored twice —
        # once tz-aware ('...+05:30', old kite) and once naive (old fallback
        # provider) — which (a) doubles a session to ~150 bars and corrupts
        # the 200-bar feature window's time span, and (b) would raise
        # TypeError when an aware decision bar is bisected against the naive
        # 1-min path. Collapsing to one naive bar per instant fixes both.
        def _norm_bars(rows: Any) -> list[OHLCVBar]:
            by_ts: dict[datetime, OHLCVBar] = {}
            for r in rows:
                bar = OHLCVBar(
                    timestamp=r["timestamp"], open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=r["volume"],
                )
                ts = bar.timestamp
                if ts.tzinfo is not None:
                    ts = ts.astimezone(IST).replace(tzinfo=None)
                    bar = bar.model_copy(update={"timestamp": ts})
                by_ts.setdefault(ts, bar)  # keep first; OHLC of an instant's dupes match
            return [by_ts[k] for k in sorted(by_ts)]

        decision_by_sym: dict[str, list[OHLCVBar]] = {}
        dec_rows_by_sym: dict[str, list[Any]] = {}
        for r in intraday_data.get("decision_bars", []):
            dec_rows_by_sym.setdefault(r["symbol"], []).append(r)
        for sym, rows in dec_rows_by_sym.items():
            decision_by_sym[sym] = _norm_bars(rows)
        minute_by_sym: dict[str, list[OHLCVBar]] = {}
        minute_ts_by_sym: dict[str, list[datetime]] = {}
        for sym, rows in intraday_data.get("minute_bars", {}).items():
            mbars = _norm_bars(rows)
            minute_by_sym[sym] = mbars
            minute_ts_by_sym[sym] = [b.timestamp for b in mbars]

        X: list[list[float]] = []
        y: list[int] = []
        sample_weights: list[float] = []
        bars_meta: list[dict[str, Any]] = []
        feature_names: list[str] = []
        feature_names_set: set[str] = set()

        feedback_data = feedback_data or {}
        feedback_lookback_days = int(
            self.ctx.config.strategy.feedback.lookback_days or 60
        )

        # Only sample entries before the intraday cutoff — the live engine
        # opens no new MIS positions after it, and near-close entries have
        # almost no runway to target under the to-session-close horizon.
        cutoff_str = (
            getattr(self.ctx.config.market_hours, "intraday_cutoff", "14:30")
            or "14:30"
        )
        try:
            cutoff_time = datetime.strptime(cutoff_str, "%H:%M").time()
        except (ValueError, TypeError):
            cutoff_time = datetime.strptime("14:30", "%H:%M").time()

        for sym, bars in decision_by_sym.items():
            if len(bars) < window_size + 2:
                continue

            symbol_has_recent_failure = False
            if sym in feedback_data:
                fb = feedback_data[sym]
                if min(fb.get("pred_accuracy", 0.5), fb.get("dry_run_accuracy", 0.5)) < 0.5:
                    symbol_has_recent_failure = True

            mbars = minute_by_sym.get(sym, [])
            mts = minute_ts_by_sym.get(sym, [])

            # Feature at i, fill at i+1 open, label on the 1-min path.
            # Stride = 15-min cadence (every 3rd 5-min bar).
            for i in range(window_size, len(bars) - 1, _INTRADAY_DECISION_STRIDE):
                # Skip decision bars at/after the intraday cutoff (IST clock).
                dts = bars[i].timestamp
                t_local = (
                    dts.astimezone(IST).time() if dts.tzinfo else dts.time()
                )
                if t_local >= cutoff_time:
                    continue

                window = bars[i - window_size : i + 1]
                features = compute_features(window, indicator_cfg)
                if not features:
                    continue
                # Session-relative intraday features (VWAP distance, opening-
                # range position) from the same 5-min window — identical helper
                # at inference, so no train/serve skew.
                features.update(compute_session_features(window))

                if feedback_data:
                    merge_feedback_features(features, sym, feedback_data)
                _merge_daily_broadcast(features, sym, bars[i].timestamp)

                entry_bar = bars[i + 1]
                next_open = entry_bar.open
                atr_pct = features.get("atr_pct") or 0.0
                # Cost-aware target floor (MIS round-trip + slippage), same
                # value reused for the precomputed exits + bars_meta below so
                # label and backtest agree on the barrier.
                eff_target_pct = max(atr_pct * target_atr_mult, cost_floor_pct)
                if next_open <= 0 or atr_pct <= 0 or not mbars:
                    label = 1
                    m_start = len(mbars)
                elif label_mode == "relative":
                    # Cross-sectional label: ranked per decision INSTANT
                    # across the universe by _build_intraday_matrix AFTER
                    # the cross-chunk concat (per-chunk cross-sections are
                    # <= chunk-size symbols — too thin to rank). The
                    # forward return-to-close it ranks comes from the
                    # 1-min flat_exit walk below; placeholder HOLD here.
                    label = 1
                    m_start = bisect.bisect_left(mts, entry_bar.timestamp)
                else:
                    m_start = bisect.bisect_left(mts, entry_bar.timestamp)
                    label = intraday_triple_barrier_label(
                        entry=next_open,
                        entry_time=entry_bar.timestamp,
                        horizon_minutes=horizon_minutes,
                        target_pct=eff_target_pct,
                        sl_pct=atr_pct * sl_atr_mult,
                        minute_bars=mbars,
                        start_idx=m_start,
                    )

                for k in features:
                    if k in excluded_keys:
                        continue
                    if k not in feature_names_set:
                        if X:
                            logger.warning(
                                "intraday-retrain: feature %s appeared late at "
                                "sample %d for %s — backfilling 0.0 into %d prior "
                                "rows.", k, len(X), sym, len(X),
                            )
                        feature_names.append(k)
                        feature_names_set.add(k)
                        for existing in X:
                            existing.append(0.0)

                bar_weight = 1.0
                if symbol_has_recent_failure:
                    try:
                        age_days = (bars[-1].timestamp - bars[i].timestamp).days
                        if 0 <= age_days <= feedback_lookback_days:
                            bar_weight = self.ctx.config.strategy.feedback.sample_weight_boost
                    except Exception:
                        pass

                # Precompute the realized exit per direction on the 1-min
                # path (same tie→SL ordering as walk_forward's _path_aware_exit)
                # instead of storing the raw path. At the to-session-close
                # horizon the path is hundreds of 1-min bars; keeping it per
                # sample × millions of samples would OOM. Two scalars carry
                # all the information the backtest's exit walk would extract.
                target_pct = eff_target_pct
                sl_pct = atr_pct * sl_atr_mult
                buy_target = next_open * (1 + target_pct)
                buy_sl = next_open * (1 - sl_pct)
                sell_target = next_open * (1 - target_pct)
                sell_sl = next_open * (1 + sl_pct)
                buy_exit: float | None = None
                sell_exit: float | None = None
                flat_exit = next_open
                if mbars and next_open > 0:
                    deadline = entry_bar.timestamp + timedelta(minutes=horizon_minutes)
                    session_date = entry_bar.timestamp.date()
                    for j in range(m_start, len(mbars)):
                        mb = mbars[j]
                        if mb.timestamp.date() != session_date or mb.timestamp >= deadline:
                            break
                        flat_exit = mb.close
                        hi, lo = mb.high, mb.low
                        if buy_exit is None:
                            ht, hs = hi >= buy_target, lo <= buy_sl
                            if ht and hs:
                                buy_exit = buy_sl  # tie → SL
                            elif ht:
                                buy_exit = buy_target
                            elif hs:
                                buy_exit = buy_sl
                        if sell_exit is None:
                            ht, hs = lo <= sell_target, hi >= sell_sl
                            if ht and hs:
                                sell_exit = sell_sl
                            elif ht:
                                sell_exit = sell_target
                            elif hs:
                                sell_exit = sell_sl
                        if buy_exit is not None and sell_exit is not None:
                            break
                # Untriggered directions exit flat at the last in-window close.
                if buy_exit is None:
                    buy_exit = flat_exit
                if sell_exit is None:
                    sell_exit = flat_exit

                X.append([features.get(k, 0.0) for k in feature_names])
                y.append(label)
                sample_weights.append(bar_weight)
                meta: dict[str, Any] = {
                    "symbol": sym,
                    "entry_close": float(next_open),
                    "exit_close": float(flat_exit),
                    "buy_exit": float(buy_exit),
                    "sell_exit": float(sell_exit),
                    "hold_days": 1,  # MIS closes same session
                    "target_pct": float(target_pct),
                    "sl_pct": float(sl_pct),
                    "entry_date": entry_bar.timestamp.strftime("%Y-%m-%d"),
                }
                if label_mode == "relative":
                    # Forward return-to-close (entry next-5min-open ->
                    # session close via the 1-min walk) + the exact
                    # decision instant as the cross-sectional group key.
                    meta["_rel_fwd"] = (
                        (flat_exit / next_open - 1.0)
                        if (mbars and next_open > 0) else None
                    )
                    meta["_rel_group"] = bars[i].timestamp.isoformat()
                bars_meta.append(meta)

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

    async def _build_intraday_matrix(
        self,
        daily_data: dict[str, Any],
        *,
        horizon_minutes: int,
        target_atr_mult: float,
        sl_atr_mult: float,
        cost_floor_pct: float = 0.0,
        label_mode: str = "triple_barrier",
        feedback_data: dict[str, Any] | None = None,
        sector_map: dict[str, str] | None = None,
        bulk_deal_lookup: dict[tuple[str, str], dict[str, int]] | None = None,
        news_lookup: dict[str, list[tuple[str, str]]] | None = None,
        vix_timeline: list[tuple[str, float]] | None = None,
        fno_lookup: dict[str, Any] | None = None,
    ) -> tuple[
        list[list[float]], list[int], list[str], list[float], list[dict[str, Any]]
    ]:
        """Memory-safe builder for the 5-min intraday training matrix.

        1-min path bars for the whole intraday universe at once can exhaust
        available memory, so we walk the symbol set in chunks: fetch each chunk's
        5-min + 1-min bars, run ``_prepare_intraday_training_data`` on it, and
        concatenate. Per-chunk ``feature_names`` are realigned to a canonical
        column order (a feature absent from a chunk → 0.0) before concat, and
        the combined matrix is globally re-sorted by entry_date so the
        walk-forward CV still splits cleanly by time.

        ``daily_data`` (the full-universe daily set already loaded for the
        swing model) supplies the prior-session broadcast context for every
        chunk.
        """
        import gc as _gc

        cfg = self.ctx.config.retraining
        intraday_window = int(
            getattr(self.ctx.config.database.retention, "intraday_ohlcv_days", 365)
        )
        win = min(int(cfg.max_training_days), intraday_window)

        # Decision bars come from 5-min, but the triple-barrier label and the
        # per-direction exits resolve on the 1-min path. A symbol with 5-min
        # bars but no 1-min path can only emit all-HOLD, zero-return samples —
        # the 5-min universe (~365 syms) is far wider than the 1-min backfill
        # (~97), so feeding the difference would bury the real BUY/SELL signal
        # under path-less HOLD noise and drag the backtest Sharpe to zero.
        # Intersect: the 1-min coverage is the trainable universe.
        dec_symbols = await self.ctx.db.get_distinct_ohlcv_symbols("5minute", max_days=win)
        path_symbols = set(
            await self.ctx.db.get_distinct_ohlcv_symbols("1m", max_days=win)
        )
        symbols = [s for s in dec_symbols if s in path_symbols]
        if not symbols:
            logger.warning(
                "Intraday matrix: no symbol has BOTH 5-min decision bars and "
                "1-min path bars within %dd (5m=%d, 1m=%d) — run "
                "backfill-intraday + backfill-intraday-1m first. Skipping.",
                win, len(dec_symbols), len(path_symbols),
            )
            return [], [], [], [], []
        if len(symbols) < len(dec_symbols):
            logger.info(
                "Intraday matrix: training on %d symbols with 1-min path "
                "(dropped %d 5m-only symbols that can't be path-labelled).",
                len(symbols), len(dec_symbols) - len(symbols),
            )

        canonical: list[str] = []
        canon_idx: dict[str, int] = {}
        X_all: list[list[float]] = []
        y_all: list[int] = []
        w_all: list[float] = []
        meta_all: list[dict[str, Any]] = []

        for start in range(0, len(symbols), _INTRADAY_SYMBOL_CHUNK):
            chunk = symbols[start : start + _INTRADAY_SYMBOL_CHUNK]
            intraday_data = await self.ctx.db.get_intraday_training_dataset(
                max_days=win, symbols=chunk,
            )
            Xc, yc, namesc, wc, metac = self._prepare_intraday_training_data(
                intraday_data, daily_data,
                horizon_minutes=horizon_minutes,
                target_atr_mult=target_atr_mult, sl_atr_mult=sl_atr_mult,
                cost_floor_pct=cost_floor_pct,
                label_mode=label_mode,
                feedback_data=feedback_data, sector_map=sector_map,
                bulk_deal_lookup=bulk_deal_lookup, news_lookup=news_lookup,
                vix_timeline=vix_timeline, fno_lookup=fno_lookup,
            )
            del intraday_data
            if yc:
                for nm in namesc:
                    if nm not in canon_idx:
                        canon_idx[nm] = len(canonical)
                        canonical.append(nm)
                        for r in X_all:
                            r.append(0.0)
                col = {nm: i for i, nm in enumerate(namesc)}
                for row in Xc:
                    X_all.append(
                        [row[col[nm]] if nm in col else 0.0 for nm in canonical]
                    )
                y_all.extend(yc)
                w_all.extend(wc)
                meta_all.extend(metac)
            _gc.collect()

        if meta_all:
            order = sorted(
                range(len(meta_all)),
                key=lambda i: meta_all[i].get("entry_date", ""),
            )
            X_all = [X_all[i] for i in order]
            y_all = [y_all[i] for i in order]
            w_all = [w_all[i] for i in order]
            meta_all = [meta_all[i] for i in order]

        if label_mode == "relative" and meta_all:
            # Cross-sectional ranking per decision INSTANT, now that all
            # symbol chunks are concatenated (~full universe per instant).
            y_all = _assign_relative_labels(
                [m.get("_rel_fwd") for m in meta_all],
                [str(m.get("_rel_group", "")) for m in meta_all],
                quantile=float(
                    getattr(self.ctx.config.strategy,
                            "relative_label_quantile", 0.20)
                ),
            )

        logger.info(
            "Intraday matrix: %d samples across %d symbols | %d feature cols "
            "| chunked %d/fetch | window=%dd",
            len(y_all), len(symbols), len(canonical),
            _INTRADAY_SYMBOL_CHUNK, win,
        )
        return X_all, y_all, canonical, w_all, meta_all

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

    def _clear_shadow_slot_if_holds(self, model_type: str, version: str) -> None:
        """Unload the in-memory shadow slot when it hosts `version`.

        After a promotion the version lives in the production slot
        (keeping it in the shadow slot would double-log its shadow
        predictions); after a retirement it must stop emitting shadow
        predictions entirely. The slot may instead hold a NEWER
        candidate loaded by this run's deployment step — leave that
        one alone.
        """
        ml = self.ctx.ml
        if ml is None:
            return
        try:
            if ml.get_shadow_version(model_type) == version:
                ml.clear_shadow(model_type)
        except Exception:
            logger.debug(
                "shadow-slot hygiene failed for %s/%s",
                model_type, version, exc_info=True,
            )

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
                    shadow_acc = _as_float((shadow_live or {}).get("direction_accuracy"))
                    current_acc = _as_float((current_live or {}).get("direction_accuracy"))
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
                    self._clear_shadow_slot_if_holds(model_type, shadow["version"])
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
                if self.ctx.ml:
                    self._clear_shadow_slot_if_holds(model_type, shadow["version"])
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
