"""Shared ML-review service.

Single source of truth for "review one or more symbols and return a
recommendation per symbol". Consumed by the dashboard `/api/review` endpoint
and the Telegram `/review` command so the logic (and its DB→provider OHLCV
fallback, which lets review work for ANY NSE symbol, not just the ingested
universe) lives in exactly one place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from yolovest.data.features import IndicatorConfig, compute_features
from yolovest.data.ohlcv_cache import get_ohlcv_cached

if TYPE_CHECKING:
    from yolovest.context import AppContext

logger = logging.getLogger(__name__)


def _indicator_cfg(ctx: AppContext) -> IndicatorConfig:
    ind = ctx.config.strategy.indicators
    return IndicatorConfig(
        ema_periods=ctx.config.strategy.ema_periods,
        rsi=ind.rsi, macd=ind.macd, bollinger_bands=ind.bollinger_bands,
        vwap=ind.vwap, atr=ind.atr, volume_profile=ind.volume_profile,
        obv=ind.obv, supertrend=ind.supertrend,
        extended_momentum=ind.extended_momentum,
    )


async def review_symbols(
    ctx: AppContext, requested: list[str] | None = None,
) -> dict[str, Any]:
    """Run ML review on the given symbols (or all current holdings when none
    are given) and return ``{"recommendations": [...]}``.

    Works for any NSE symbol, held or not: when the local DB has no/thin
    history for a symbol it falls back to an on-demand provider fetch, so a
    never-ingested symbol still gets a real recommendation. Returns an
    ``error`` key (with empty recommendations) only when neither symbols nor
    holdings are available.
    """
    # Holdings context (for P&L display and held/not-held — never for filtering).
    holdings: list[dict[str, Any]] = []
    try:
        holdings = await ctx.broker.get_holdings()
    except Exception:
        pass
    holding_map = {
        h["tradingsymbol"]: h
        for h in (holdings or []) if h.get("quantity", 0) > 0
    }

    # Open system-managed trades by symbol so the payload can carry trade_id /
    # current_sl — the Holdings UI uses these for the in-row "Tighten SL"
    # action without an extra round trip.
    open_trades_for_symbol: dict[str, dict[str, Any]] = {}
    try:
        for tr in await ctx.db.get_open_positions(mode=ctx.config.mode):
            sym = tr.get("symbol")
            if sym and sym not in open_trades_for_symbol:
                open_trades_for_symbol[sym] = tr
    except Exception:
        logger.debug("review: failed to load open positions", exc_info=True)

    if requested:
        symbols = [s.upper() for s in requested]
    elif holding_map:
        symbols = list(holding_map.keys())
    else:
        return {
            "recommendations": [],
            "error": "Provide symbols or authenticate with Kite for holdings review",
        }

    indicator_cfg = _indicator_cfg(ctx)
    recommendations: list[dict[str, Any]] = []

    for symbol in symbols:
        rec = await _review_one(
            ctx, symbol,
            held=holding_map.get(symbol),
            open_trade=open_trades_for_symbol.get(symbol),
            indicator_cfg=indicator_cfg,
        )
        recommendations.append(rec)

    recommendations.sort(
        key=lambda r: (r["action"] != "HOLD", r["confidence"]), reverse=True,
    )
    return {"recommendations": recommendations}


async def _review_one(
    ctx: AppContext, symbol: str, *,
    held: dict[str, Any] | None,
    open_trade: dict[str, Any] | None,
    indicator_cfg: IndicatorConfig,
) -> dict[str, Any]:
    """Review a single symbol into a recommendation dict."""
    rec: dict[str, Any] = {
        "symbol": symbol,
        "held": held is not None,
        "quantity": held.get("quantity", 0) if held else 0,
        "average_price": held.get("average_price", 0) if held else 0,
        "last_price": held.get("last_price", 0) if held else 0,
        "pnl_pct": 0,
        "action": "HOLD",
        "confidence": 0,
        "signal_type": "HOLD",
        "reasoning": "",
        "target_price": None,
        "stop_loss_price": None,
        "trade_id": open_trade.get("trade_id") if open_trade else None,
        "current_sl": (
            float(open_trade.get("stop_loss_price") or 0) if open_trade else 0
        ),
        "trade_signal_type": (
            open_trade.get("signal_type") if open_trade else None
        ),
        "entry_price": (
            float(open_trade.get("entry_price") or 0) if open_trade else 0
        ),
        # Price-context extras (Telegram review uses these; web card may too).
        "day_change_pct": None,
        "week_change_pct": None,
        "vol_ratio": None,
        "avg_volume_20d": None,
        "rsi": None,
        "target_pct": None,
        "sl_pct": None,
    }

    entry = rec["average_price"]
    ltp = rec["last_price"]
    if ltp <= 0:
        try:
            ltp = await ctx.market_data.get_ltp(symbol)
            rec["last_price"] = ltp
        except Exception:
            pass
    if entry > 0 and ltp > 0:
        rec["pnl_pct"] = round((ltp - entry) / entry * 100, 2)

    try:
        bars = await ctx.db.get_ohlcv(symbol, "daily", days=365)
        if not bars or len(bars) < 50:
            # Not in the ingested universe (or thin history) — fetch on demand
            # from the provider chain so review works for ANY NSE symbol. The
            # model infers from the feature vector. Transient: not persisted.
            try:
                fetched = await get_ohlcv_cached(ctx.market_data, symbol, 365)
                if fetched and len(fetched) > len(bars or []):
                    bars = fetched
            except Exception:
                logger.debug(
                    "review: on-demand OHLCV fetch failed for %s", symbol,
                    exc_info=True,
                )
        if not bars or len(bars) < 50:
            rec["reasoning"] = f"Insufficient data ({len(bars) if bars else 0} bars)"
            return rec

        # Day / week % moves and today's volume vs the 20-day average.
        if ltp and len(bars) >= 2 and bars[-2].close:
            rec["day_change_pct"] = round((ltp - bars[-2].close) / bars[-2].close * 100, 2)
        if ltp and len(bars) >= 8 and bars[-8].close:
            rec["week_change_pct"] = round((ltp - bars[-8].close) / bars[-8].close * 100, 2)
        vols = [b.volume for b in bars[-20:] if b.volume]
        if vols:
            rec["avg_volume_20d"] = sum(vols) / len(vols)
            if bars[-1].volume and rec["avg_volume_20d"]:
                rec["vol_ratio"] = round(bars[-1].volume / rec["avg_volume_20d"], 2)

        features = compute_features(bars, indicator_cfg)
        if not features:
            rec["reasoning"] = "Feature computation failed"
            return rec
        rec["rsi"] = features.get("rsi_14")

        swing_pred = None
        intra_pred = None
        if ctx.ml:
            try:
                swing_pred = await ctx.ml.predict_swing(symbol, features, current_price=ltp or None)
            except Exception:
                pass
            try:
                intra_pred = await ctx.ml.predict_intraday(symbol, features, current_price=ltp or None)
            except Exception:
                pass

        pred = None
        if swing_pred and swing_pred.signal_type != "HOLD":
            pred = swing_pred
        if intra_pred and intra_pred.signal_type != "HOLD":
            if pred is None or intra_pred.confidence > pred.confidence:
                pred = intra_pred

        if pred is None:
            rsi = features.get("rsi_14", 50)
            rec["action"] = "HOLD"
            rec["confidence"] = max(
                (swing_pred.confidence if swing_pred else 0),
                (intra_pred.confidence if intra_pred else 0),
            )
            parts = []
            if rsi < 30:
                parts.append("oversold (RSI %.0f)" % rsi)
            elif rsi > 70:
                parts.append("overbought (RSI %.0f)" % rsi)
            held_b = held is not None
            if held_b and rec["pnl_pct"] > 10:
                parts.append("consider partial profit booking (%.1f%% up)" % rec["pnl_pct"])
                rec["action"] = "TIGHTEN_SL"
            elif held_b and rec["pnl_pct"] < -10:
                parts.append("significant drawdown (%.1f%%)" % rec["pnl_pct"])
            rec["reasoning"] = "; ".join(parts) if parts else "No strong directional signal"
        else:
            held_b = held is not None
            rec["signal_type"] = pred.signal_type
            rec["confidence"] = round(pred.confidence, 2)
            if pred.signal_type == "SELL":
                rec["action"] = "SELL" if held_b else "SHORT"
                rec["target_price"] = round(pred.target_price, 2) if hasattr(pred, "target_price") else None
                rec["stop_loss_price"] = round(pred.stop_loss_price, 2) if hasattr(pred, "stop_loss_price") else None
                rec["reasoning"] = f"ML SELL signal at {pred.confidence:.0%} confidence"
            elif pred.signal_type == "BUY":
                rec["action"] = "BUY_MORE" if held_b else "BUY"
                rec["target_price"] = round(pred.target_price, 2) if hasattr(pred, "target_price") else None
                rec["stop_loss_price"] = round(pred.stop_loss_price, 2) if hasattr(pred, "stop_loss_price") else None
                rec["reasoning"] = f"ML BUY signal at {pred.confidence:.0%} confidence"
            # Target / SL as a % move from the prediction's entry basis
            # (direction-aware: favourable side is positive).
            basis = float(getattr(pred, "entry_price", 0) or ltp or 0)
            if basis > 0 and rec["target_price"] and rec["stop_loss_price"]:
                tgt_pct = (rec["target_price"] - basis) / basis * 100
                sl_pct = (rec["stop_loss_price"] - basis) / basis * 100
                if pred.signal_type == "SELL":
                    tgt_pct, sl_pct = -tgt_pct, -sl_pct
                rec["target_pct"] = round(tgt_pct, 1)
                rec["sl_pct"] = round(sl_pct, 1)

    except Exception as e:
        rec["reasoning"] = f"Analysis failed: {e}"

    return rec
