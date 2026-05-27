"""Live intraday inference must feed the intraday model features computed
from a 5-min window (not the daily-derived dict with `close` swapped), and
intraday target/SL geometry must use the 5-min ATR — otherwise the offline
walk-forward Sharpe doesn't transfer to live signals.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from yolovest.data.features import compute_features
from yolovest.models.schemas import MLPrediction, OHLCVBar
from yolovest.strategy.signal_evaluator import (
    _INTRADAY_WINDOW_SIZE,
    _intraday_indicator_cfg,
    evaluate_symbol_signal,
)


def _five_min_bars(n: int, *, high_low_half: float = 0.05) -> list[OHLCVBar]:
    bars = []
    t = datetime(2026, 5, 18, 9, 15)
    px = 100.0
    for _ in range(n):
        bars.append(OHLCVBar(
            timestamp=t.isoformat(), open=px, high=px + high_low_half,
            low=px - high_low_half, close=px + 0.01, volume=10_000,
        ))
        t += timedelta(minutes=5)
    return bars


def _daily_features(ctx) -> dict:
    bars = []
    t = datetime(2026, 1, 1)
    px = 100.0
    for _ in range(60):
        bars.append(OHLCVBar(
            timestamp=t.isoformat(), open=px, high=px + 2.0,
            low=px - 2.0, close=px + 0.1, volume=1_000_000,
        ))
        t += timedelta(days=1)
        px += 0.1
    feats = compute_features(bars, _intraday_indicator_cfg(ctx))
    # Force a large DAILY ATR (absolute) but a low daily atr_pct so the
    # intraday eligibility cap doesn't reject; add a broadcast marker.
    feats["atr_14"] = 3.0
    feats["atr_pct"] = 0.01
    feats["vix_level"] = 0.7
    return feats


@pytest.mark.asyncio
async def test_intraday_inference_uses_5min_window_and_atr(app_context):
    ctx = app_context
    ctx.config.strategy.mode = "intraday"
    ctx.config.market_data.kite_data_enabled = False  # skip circuit reality-check

    five_min = _five_min_bars(_INTRADAY_WINDOW_SIZE + 5)
    ctx.db.get_ohlcv = AsyncMock(return_value=five_min)

    captured: dict = {}

    async def _capture(sym, feats, *, current_price=None):
        captured["feats"] = feats
        return MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="intraday",
            confidence=0.95, model_version="t",
        )

    ctx.ml = MagicMock()
    ctx.ml.predict_intraday = AsyncMock(side_effect=_capture)

    features = _daily_features(ctx)

    ev = await evaluate_symbol_signal(
        ctx, "RELIANCE", features,
        current_price=100.0, held_symbols=set(), locked_symbols=set(),
        effective_mode="intraday", bypass_time_gates=True,
    )

    # 1. The model received 5-min-derived technicals, not the daily atr_14=3.0.
    five_min_atr = compute_features(
        five_min[-(_INTRADAY_WINDOW_SIZE + 1):], _intraday_indicator_cfg(ctx),
    )["atr_14"]
    assert captured["feats"]["atr_14"] == pytest.approx(five_min_atr)
    assert captured["feats"]["atr_14"] < 0.5  # 5-min ATR, far below daily 3.0

    # 2. Broadcast features survived the overlay.
    assert captured["feats"]["vix_level"] == 0.7

    # 3. Geometry used the 5-min ATR → target hugs entry (daily ATR would
    #    push it to ~101.8).
    assert ev.outcome == "passed"
    assert ev.target_price is not None
    assert abs(ev.target_price - 100.0) < 1.0


@pytest.mark.asyncio
async def test_intraday_skipped_when_insufficient_5min_history(app_context):
    # Too few 5-min bars → no intraday feature dict built; the model still
    # gets the daily features (graceful fallback, no crash).
    ctx = app_context
    ctx.config.strategy.mode = "intraday"
    ctx.config.market_data.kite_data_enabled = False
    ctx.db.get_ohlcv = AsyncMock(return_value=_five_min_bars(20))

    captured: dict = {}

    async def _capture(sym, feats, *, current_price=None):
        captured["feats"] = feats
        return MLPrediction(
            signal_type="HOLD", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="intraday",
            confidence=0.5, model_version="t",
        )

    ctx.ml = MagicMock()
    ctx.ml.predict_intraday = AsyncMock(side_effect=_capture)

    features = _daily_features(ctx)
    await evaluate_symbol_signal(
        ctx, "RELIANCE", features,
        current_price=100.0, held_symbols=set(), locked_symbols=set(),
        effective_mode="intraday", bypass_time_gates=True,
    )
    # Fell back to the daily dict (atr_14 unchanged at 3.0).
    assert captured["feats"]["atr_14"] == 3.0
