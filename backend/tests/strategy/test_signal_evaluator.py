"""Direct tests for the signal evaluator — the money-path step that turns an
ML prediction into a tradeable signal (holding-period routing, HOLD / locked /
held / short-on-swing skips, ATR target/SL geometry, confidence floor).

The module had no eponymous test file; it was only exercised indirectly via
generate-signals (which patches out `decide_holding_period`) and the e2e
replay. These tests drive `evaluate_symbol_signal` directly through each
outcome branch.
"""

from datetime import time
from unittest.mock import AsyncMock

import pytest

from yolovest.models.schemas import MLPrediction
from yolovest.strategy import signal_evaluator as se
from yolovest.strategy.signal_evaluator import _capped_days_range, evaluate_symbol_signal


def _prediction(signal_type="BUY", confidence=0.9, entry=2500.0):
    return MLPrediction(
        signal_type=signal_type,
        entry_price=entry,
        target_price=entry * 1.04,
        stop_loss_price=entry * 0.98,
        position_size=1,
        holding_period="swing",
        confidence=confidence,
        model_version="v1",
        class_probabilities={"BUY": confidence, "HOLD": 1 - confidence, "SELL": 0.0},
    )


class TestCappedDaysRange:
    """The swing-horizon cap keeps holding horizons inside what the swing
    label actually measures."""

    def test_none_passthrough(self):
        assert _capped_days_range(None, 5) is None

    def test_non_positive_cap_disables(self):
        assert _capped_days_range((2, 30), 0) == (2, 30)
        assert _capped_days_range((2, 30), -1) == (2, 30)

    def test_clamps_both_bounds(self):
        assert _capped_days_range((2, 30), 10) == (2, 10)
        assert _capped_days_range((12, 30), 10) == (10, 10)

    def test_below_cap_unchanged(self):
        assert _capped_days_range((1, 5), 10) == (1, 5)


@pytest.fixture
def eval_ctx(app_context):
    """app_context with an ML mock wired in (AppContext.ml defaults to None)."""
    app_context.ml = AsyncMock()
    return app_context


def _force_holding(monkeypatch, period="swing", product="CNC", days=3):
    """Pin decide_holding_period so the test controls the routing branch."""
    monkeypatch.setattr(
        se, "decide_holding_period", lambda *a, **k: (period, product, days),
    )


async def _evaluate(ctx, *, held=None, locked=None, features=None):
    return await evaluate_symbol_signal(
        ctx, "RELIANCE", features if features is not None else {"atr_14": 50.0},
        current_price=2500.0,
        held_symbols=held or set(),
        locked_symbols=locked or set(),
        now_time=time(10, 0),
        effective_mode="swing",
    )


class TestEvaluateOutcomes:
    async def test_hold_returns_hold_signal(self, eval_ctx, monkeypatch):
        _force_holding(monkeypatch)
        eval_ctx.ml.predict_swing = AsyncMock(return_value=_prediction("HOLD"))

        result = await _evaluate(eval_ctx)

        assert result.outcome == "hold_signal"
        assert result.signal_type == "HOLD"

    async def test_no_prediction_returns_hold_signal(self, eval_ctx, monkeypatch):
        _force_holding(monkeypatch)
        eval_ctx.ml.predict_swing = AsyncMock(return_value=None)

        result = await _evaluate(eval_ctx)

        assert result.outcome == "hold_signal"

    async def test_locked_sell_skipped(self, eval_ctx, monkeypatch):
        _force_holding(monkeypatch)
        eval_ctx.ml.predict_swing = AsyncMock(return_value=_prediction("SELL"))

        result = await _evaluate(eval_ctx, locked={"RELIANCE"})

        assert result.outcome == "locked_holding"

    async def test_held_sell_skipped_when_monitor_owns_exits(self, eval_ctx, monkeypatch):
        _force_holding(monkeypatch)
        eval_ctx.config.risk.skip_sell_on_holdings = True
        eval_ctx.ml.predict_swing = AsyncMock(return_value=_prediction("SELL"))

        result = await _evaluate(eval_ctx, held={"RELIANCE"})

        assert result.outcome == "sell_on_holding"

    async def test_nonheld_swing_sell_dropped(self, eval_ctx, monkeypatch):
        # A SELL on a non-held name with a swing horizon can't be expressed
        # (no overnight retail shorting) — it must be dropped, not turned
        # into an intraday MIS short with swing geometry.
        _force_holding(monkeypatch, period="swing", product="CNC", days=3)
        eval_ctx.config.risk.skip_sell_on_holdings = True
        eval_ctx.ml.predict_swing = AsyncMock(return_value=_prediction("SELL"))

        result = await _evaluate(eval_ctx)

        assert result.outcome == "short_on_swing_horizon"

    async def test_buy_passes_with_geometry(self, eval_ctx, monkeypatch):
        _force_holding(monkeypatch, period="swing", product="CNC", days=3)
        eval_ctx.config.risk.min_confidence_buy_swing = 0.6
        eval_ctx.ml.predict_swing = AsyncMock(
            return_value=_prediction("BUY", confidence=0.9, entry=2500.0),
        )

        result = await _evaluate(eval_ctx, features={"atr_14": 50.0})

        assert result.outcome == "passed"
        assert result.signal_type == "BUY"
        assert result.entry_price == 2500.0
        # BUY geometry: target above entry above stop-loss.
        assert result.target_price > result.entry_price > result.stop_loss_price
        # Snapped to the tick grid (mock broker rounds to 2dp).
        assert result.target_price == round(result.target_price, 2)
        assert result.stop_loss_price == round(result.stop_loss_price, 2)
        # Routing fields propagate from the holding-period decision.
        assert result.holding_period == "swing"
        assert result.product == "CNC"
        assert result.expected_days == 3
        assert result.effective_min_confidence == pytest.approx(0.6)

    async def test_buy_below_floor_is_low_confidence(self, eval_ctx, monkeypatch):
        _force_holding(monkeypatch, period="swing", product="CNC", days=3)
        eval_ctx.config.risk.min_confidence_buy_swing = 0.6
        eval_ctx.ml.predict_swing = AsyncMock(
            return_value=_prediction("BUY", confidence=0.2),
        )

        result = await _evaluate(eval_ctx)

        assert result.outcome == "low_confidence"
        assert result.effective_min_confidence == pytest.approx(0.6)
        # Prices are still populated for diagnostics even on rejection.
        assert result.target_price > 0

    async def test_intraday_atr_ineligible(self, eval_ctx, monkeypatch):
        # ATR% above the intraday eligibility cap -> rejected before any model
        # call (can't square off a 10%-ATR name in a half-day session).
        _force_holding(monkeypatch, period="intraday", product="MIS", days=0)
        eval_ctx.config.strategy.volatility.max_atr_pct_for_intraday_eligibility = 0.05

        result = await _evaluate(eval_ctx, features={"atr_14": 50.0, "atr_pct": 0.10})

        assert result.outcome == "intraday_atr_ineligible"

    async def test_intraday_after_cutoff_skipped(self, eval_ctx, monkeypatch):
        # A fresh intraday BUY after the configured cutoff is skipped — too
        # late in the session to open and manage an intraday position.
        _force_holding(monkeypatch, period="intraday", product="MIS", days=0)
        eval_ctx.config.market_hours.intraday_cutoff = "14:30"
        eval_ctx.config.strategy.volatility.max_atr_pct_for_intraday_eligibility = 0.05
        eval_ctx.config.risk.min_confidence_buy_intraday = 0.1
        eval_ctx.ml.predict_intraday = AsyncMock(
            return_value=_prediction("BUY", confidence=0.9),
        )

        result = await evaluate_symbol_signal(
            eval_ctx, "RELIANCE", {"atr_14": 50.0, "atr_pct": 0.01},
            current_price=2500.0, held_symbols=set(), locked_symbols=set(),
            now_time=time(15, 0), effective_mode="swing",
        )

        assert result.outcome == "intraday_cutoff"

    async def test_implausible_atr_rejected(self, eval_ctx, monkeypatch):
        # An ATR% above the hard-reject ceiling means corrupt OHLCV — reject
        # rather than emit a nonsense target/SL.
        _force_holding(monkeypatch, period="swing", product="CNC", days=3)
        eval_ctx.config.strategy.max_atr_pct_hard_reject = 0.20
        eval_ctx.ml.predict_swing = AsyncMock(
            return_value=_prediction("BUY", confidence=0.9, entry=100.0),
        )

        # atr_14 = 50 on a 100 entry -> 50% ATR, well above the 20% ceiling.
        result = await _evaluate(eval_ctx, features={"atr_14": 50.0})

        assert result.outcome == "implausible_atr"
