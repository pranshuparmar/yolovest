"""Inference-time feature enrichment — the single source for the
non-technical features the model trains on but `compute_features` does not
produce: universe regime, sector-relative, institutional (bulk-deal /
delivery), and feedback features.

Both the live heartbeat (`generate_signals`) and the dashboard dry-run call
this so the model is fed the SAME 54-feature vector it trained on. Without
it ~19 features default to 0.0 at inference — including `universe_breadth`
and `sector_breadth`, whose training neutral is 0.5 — which pushes the model
off-distribution and compresses its probabilities below the tuned
thresholds (the "model never signals" failure). Mirrors the per-sample
merge in `model_retrain._prepare_training_data`.

`load_inference_feature_context` is called ONCE per run (cross-sectional
regime / sector / feedback are shared across symbols); `enrich_features` is
called per symbol with that context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from yolovest.data.features import merge_feedback_features
from yolovest.timezone import now_ist

if TYPE_CHECKING:
    from yolovest.context import AppContext

logger = logging.getLogger(__name__)


async def load_inference_feature_context(ctx: AppContext) -> dict[str, Any]:
    """Load the per-run shared context (cross-sectional regime, sector
    stats, per-symbol returns, feedback data). Each piece fails open to a
    neutral empty value so a missing data source never blocks signals."""
    regime: dict[str, float] = {"breadth": 0.5, "avg_return": 0.0, "sample_size": 0}
    sector_stats: dict[str, dict[str, float]] = {}
    symbol_returns: dict[str, float] = {}
    sector_map: dict[str, str] = {}
    feedback_data: dict[str, dict[str, float]] = {}

    # Exclude today's developing daily bar so the model features are
    # "last completed session vs the one before" — the training
    # convention (training only ever sees completed sessions; a
    # partial-day breadth is a different distribution). The regime
    # RISK-GATE deliberately keeps the default (today-so-far) read —
    # it wants live market state, not training parity.
    _today = now_ist().strftime("%Y-%m-%d")
    try:
        regime = await ctx.db.compute_live_regime(exclude_date=_today)
    except Exception:
        logger.debug("inference: compute_live_regime failed", exc_info=True)
    try:
        sector_stats, symbol_returns = await ctx.db.compute_live_sector_regime(
            exclude_date=_today,
        )
    except Exception:
        logger.debug("inference: compute_live_sector_regime failed", exc_info=True)
    try:
        sector_map = await ctx.db.get_symbol_sectors_map()
    except Exception:
        logger.debug("inference: get_symbol_sectors_map failed", exc_info=True)
    try:
        if ctx.config.strategy.feedback.enabled:
            feedback_data = await ctx.db.get_feedback_data(
                lookback_days=ctx.config.strategy.feedback.lookback_days,
            )
    except Exception:
        logger.debug("inference: get_feedback_data failed", exc_info=True)

    return {
        "regime": regime,
        "sector_stats": sector_stats,
        "symbol_returns": symbol_returns,
        "sector_map": sector_map,
        "feedback_data": feedback_data,
    }


async def enrich_features(
    ctx: AppContext, symbol: str, features: dict[str, float], fctx: dict[str, Any],
) -> None:
    """Populate the regime / sector / bulk-deal / delivery / feedback
    features (in place), mirroring training's neutral defaults exactly
    (universe & sector breadth default to 0.5, NOT 0.0)."""
    # Universe regime (broadcast — same for every symbol this run).
    reg = fctx.get("regime") or {}
    if reg.get("sample_size"):
        features["universe_breadth"] = float(reg.get("breadth", 0.5))
        features["universe_avg_return"] = float(reg.get("avg_return", 0.0))
    else:
        features["universe_breadth"] = 0.5
        features["universe_avg_return"] = 0.0

    # Sector-relative.
    sec = fctx.get("sector_map", {}).get(symbol)
    sec_stats = fctx.get("sector_stats", {}).get(sec) if sec else None
    stock_ret = fctx.get("symbol_returns", {}).get(symbol)
    if sec_stats and stock_ret is not None:
        features["sector_breadth"] = float(sec_stats["breadth"])
        features["sector_avg_return"] = float(sec_stats["avg_return"])
        features["relative_momentum"] = float(stock_ret - sec_stats["avg_return"])
    else:
        features["sector_breadth"] = 0.5
        features["sector_avg_return"] = 0.0
        features["relative_momentum"] = 0.0

    # Institutional flow (per-symbol queries).
    try:
        bd = await ctx.db.count_recent_bulk_deals(symbol, 5)
        buy, sell = float(bd.get("buy_count", 0)), float(bd.get("sell_count", 0))
        features["bulk_deal_buy_5d"] = buy
        features["bulk_deal_sell_5d"] = sell
        features["bulk_deal_net_5d"] = buy - sell
    except Exception:
        logger.debug("inference: bulk-deal merge failed for %s", symbol, exc_info=True)
        features["bulk_deal_buy_5d"] = 0.0
        features["bulk_deal_sell_5d"] = 0.0
        features["bulk_deal_net_5d"] = 0.0
    try:
        _dp = await ctx.db.get_recent_delivery_pct(symbol, 5)
        features["delivery_pct_avg_5d"] = float(_dp) if _dp is not None else 0.0
    except Exception:
        logger.debug("inference: delivery merge failed for %s", symbol, exc_info=True)
        features["delivery_pct_avg_5d"] = 0.0

    # Feedback (fb_*) — neutral 0.5/0.0 defaults when the symbol has no
    # recent feedback, matching training.
    merge_feedback_features(features, symbol, fctx.get("feedback_data", {}))
