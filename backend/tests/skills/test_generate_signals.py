"""Tests for generate-signals skill diagnostics and strategy logic."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from yolovest.models.schemas import MLPrediction, OHLCVBar
from yolovest.skills.generate_signals import GenerateSignalsSkill
from yolovest.timezone import now_ist


def _make_bars(n: int) -> list[OHLCVBar]:
    """Generate `n` consecutive daily bars ending today.

    Anchoring to today keeps these bars on the fresh side of the
    `market_data.max_signal_data_age_trading_days` staleness gate
    regardless of when the test suite runs (was previously fixed to
    Jan 2026 which silently failed every test once the calendar
    advanced past Feb 2026).

    Bar ranges are kept tight (~1% intra-bar) so ATR-% stays under
    the intraday eligibility cap (default 5%) and the holding-bucket
    routing in tests behaves like normal large-caps, not a circuit-
    breaker stock.
    """
    end = now_ist().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return [
        OHLCVBar(
            timestamp=end - timedelta(days=n - 1 - i),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=10000,
        )
        for i in range(n)
    ]


@pytest.fixture
def signal_skill(app_context):
    app_context.ml = AsyncMock()
    app_context.ml.has_shadow = lambda model_type: False
    app_context.config.strategy.mode = "short_term"
    app_context.config.strategy.allowed_holding_periods = ["short_term", "long_term"]
    app_context.config.market_hours.intraday_cutoff = "23:59"
    return GenerateSignalsSkill(app_context)


class TestGenerateSignalsDiagnostics:
    """Test that diagnostics correctly report why signals are filtered."""

    async def test_hold_signals_counted(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"}, {"symbol": "TCS"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="HOLD", entry_price=100.0, target_price=105.0,
            stop_loss_price=95.0, position_size=1, holding_period="3d",
            confidence=0.45, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.success
        assert result.data["signals_generated"] == 0
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["hold_signal"] == 2
        assert diag["filter_counts"]["passed"] == 0
        assert len(diag["rejection_details"]) == 2

    async def test_low_confidence_counted(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"}, {"symbol": "TCS"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="3d",
            confidence=0.50, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.success
        assert result.data["signals_generated"] == 0
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["low_confidence"] == 2
        assert diag["min_confidence_threshold"] == 0.60
        assert all(r["reason"] == "low_confidence" for r in diag["rejection_details"])

    async def test_insufficient_bars_counted(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(30))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.success
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["insufficient_bars"] == 1

    async def test_passed_signals_counted(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="3d",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.success
        assert result.data["signals_generated"] == 1
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["passed"] == 1
        assert diag["filter_counts"]["hold_signal"] == 0

    async def test_already_signaled_skipped(self, signal_skill):
        """Symbols with existing signals or open positions today are skipped."""
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"}, {"symbol": "TCS"}, {"symbol": "INFY"},
        ])
        signal_skill.ctx.db.get_todays_signaled_symbols = AsyncMock(
            return_value={"RELIANCE", "TCS"},
        )
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="3d",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.success
        # Only INFY should generate a signal (RELIANCE and TCS already signaled)
        assert result.data["signals_generated"] == 1
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["already_signaled"] == 2
        assert diag["filter_counts"]["passed"] == 1

    async def test_cooldown_blocks_recently_traded(self, signal_skill):
        """Symbols traded within cooldown_days are hard-blocked."""
        from yolovest.timezone import now_ist
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "BPCL"},
        ])
        # BPCL traded 0 days ago (today) — within 1-day cooldown
        signal_skill.ctx.db.get_recently_traded_symbols = AsyncMock(
            return_value={"BPCL": now_ist().isoformat()},
        )
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.data["signals_generated"] == 0
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["cooldown"] == 1

    async def test_repeat_requires_higher_confidence(self, signal_skill):
        """Symbols traded within lookback but past cooldown need elevated confidence."""
        from datetime import timedelta

        from yolovest.timezone import now_ist
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "BPCL"},
        ])
        # BPCL traded 3 days ago — past cooldown (1d) but within lookback (5d)
        signal_skill.ctx.db.get_recently_traded_symbols = AsyncMock(
            return_value={"BPCL": (now_ist() - timedelta(days=3)).isoformat()},
        )
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        # Confidence 0.70 — passes normal threshold (0.65) but fails repeat (0.80)
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=280.0, target_price=290.0,
            stop_loss_price=270.0, position_size=1, holding_period="3d",
            confidence=0.70, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.data["signals_generated"] == 0
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["repeat_low_confidence"] == 1

    async def test_repeat_passes_with_high_confidence(self, signal_skill):
        """Repeat symbols pass if confidence exceeds the elevated threshold."""
        from datetime import timedelta

        from yolovest.timezone import now_ist
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "BPCL"},
        ])
        signal_skill.ctx.db.get_recently_traded_symbols = AsyncMock(
            return_value={"BPCL": (now_ist() - timedelta(days=3)).isoformat()},
        )
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        # Confidence 0.85 — passes both normal (0.65) and repeat (0.80) thresholds
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=280.0, target_price=290.0,
            stop_loss_price=270.0, position_size=1, holding_period="3d",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.data["signals_generated"] == 1
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["passed"] == 1


class TestHoldingPeriodDecision:
    """Test the intelligent holding period decision logic.

    These call `decide_holding_period` directly (with explicit
    now_time) instead of going through the deleted wrapper method
    on the skill. The signal-evaluator routes through the same
    function, so this is the same logic the production heartbeat
    and dry-run preview both exercise.
    """

    def _decide(self, signal_skill, features, *, now_time):
        from yolovest.config import _MODE_HOLDING_DAYS
        from yolovest.strategy.holding_period import decide_holding_period
        cfg = signal_skill.ctx.config
        return decide_holding_period(
            features,
            cfg.strategy.allowed_holding_periods,
            cfg.strategy.volatility,
            now_time,
            mode_days_range=_MODE_HOLDING_DAYS.get(cfg.strategy.mode),
        )

    def test_intraday_when_high_vol_and_volume_and_morning(self, signal_skill):
        """High ATR%, high relative volume, morning → intraday/MIS."""
        signal_skill.ctx.config.strategy.mode = "balanced"
        signal_skill.ctx.config.strategy.allowed_holding_periods = ["intraday", "short_term", "long_term"]
        features = {
            "atr_pct": 0.025,
            "relative_volume": 2.0,
            "ema_9": 100, "ema_21": 99, "ema_50": 98,
            "supertrend_trend": 1.0,
        }
        period, product, days = self._decide(
            signal_skill, features,
            now_time=datetime(2026, 3, 30, 10, 0).time(),
        )
        assert period == "intraday"
        assert product == "MIS"
        assert days == 0

    def test_no_intraday_after_1400(self, signal_skill):
        """After 14:00 IST, intraday should not be selected in balanced mode."""
        signal_skill.ctx.config.strategy.mode = "balanced"
        signal_skill.ctx.config.strategy.allowed_holding_periods = ["intraday", "short_term", "long_term"]
        features = {
            "atr_pct": 0.025,
            "relative_volume": 2.0,
            "ema_9": 100, "ema_21": 99, "ema_50": 98,
            "supertrend_trend": 1.0,
        }
        period, product, days = self._decide(
            signal_skill, features,
            now_time=datetime(2026, 3, 30, 14, 30).time(),
        )
        assert period != "intraday"
        assert product == "CNC"
        assert days > 0

    def test_1w_when_strong_trend(self, signal_skill):
        """Strong EMA alignment + SuperTrend → longer hold / CNC."""
        signal_skill.ctx.config.strategy.mode = "balanced"
        signal_skill.ctx.config.strategy.allowed_holding_periods = ["intraday", "short_term", "long_term"]
        features = {
            "atr_pct": 0.012,
            "relative_volume": 1.0,
            "ema_9": 110, "ema_21": 105, "ema_50": 100,
            "supertrend_trend": 1.0,
        }
        period, product, days = self._decide(
            signal_skill, features,
            now_time=datetime(2026, 3, 30, 10, 0).time(),
        )
        assert product == "CNC"
        assert days >= 2

    def test_3d_default_fallback(self, signal_skill):
        """Weak trend in balanced mode → short-term hold."""
        signal_skill.ctx.config.strategy.mode = "balanced"
        signal_skill.ctx.config.strategy.allowed_holding_periods = ["intraday", "short_term", "long_term"]
        features = {
            "atr_pct": 0.008,
            "relative_volume": 0.8,
            "ema_9": 100, "ema_21": 101, "ema_50": 99,
            "supertrend_trend": -1.0,
        }
        period, product, days = self._decide(
            signal_skill, features,
            now_time=datetime(2026, 3, 30, 10, 0).time(),
        )
        assert product == "CNC"
        assert days >= 1

    def test_intraday_mode_only_returns_intraday(self, signal_skill):
        """With mode=intraday, only intraday is allowed."""
        signal_skill.ctx.config.strategy.mode = "intraday"
        signal_skill.ctx.config.strategy.allowed_holding_periods = ["intraday"]
        features = {
            "atr_pct": 0.025,
            "relative_volume": 2.0,
        }
        period, product, days = self._decide(
            signal_skill, features,
            now_time=datetime(2026, 3, 30, 10, 0).time(),
        )
        assert period == "intraday"
        assert product == "MIS"
        assert days == 0

    def test_long_term_mode_only_returns_1w(self, signal_skill):
        """With mode=long_term, holding days >= 5."""
        signal_skill.ctx.config.strategy.mode = "long_term"
        signal_skill.ctx.config.strategy.allowed_holding_periods = ["long_term"]
        features = {
            "atr_pct": 0.01,
            "relative_volume": 1.0,
            "ema_9": 100, "ema_21": 101, "ema_50": 99,
            "supertrend_trend": -1.0,
        }
        period, product, days = self._decide(
            signal_skill, features,
            now_time=datetime(2026, 3, 30, 10, 0).time(),
        )
        assert period in ("positional", "long_term")
        assert product == "CNC"
        assert days >= 5


class TestProductPropagation:
    """Test that product field flows through to generated signals."""

    async def test_signal_contains_product_field(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="3d",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.data["signals_generated"] == 1
        sig = result.data["signals"][0]
        assert sig["product"] == "CNC"
        assert sig["expected_holding_period"] == "short_term"

    async def test_intraday_signal_has_mis_product(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.ml.predict_intraday = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="intraday",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("intraday", "MIS", 0)):
            result = await signal_skill.execute()

        assert result.data["signals_generated"] == 1
        sig = result.data["signals"][0]
        assert sig["product"] == "MIS"
        assert sig["expected_holding_period"] == "intraday"


class TestATRMultipliers:
    """Test that ATR multipliers are applied per holding period."""

    async def test_intraday_uses_tighter_multipliers(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.ml.predict_intraday = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="intraday",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("intraday", "MIS", 0)):
            result = await signal_skill.execute()

        sig = result.data["signals"][0]
        entry = sig["entry_price"]
        assert sig["target_price"] > entry
        assert sig["stop_loss_price"] < entry

    async def test_week_uses_wider_multipliers(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="BUY", entry_price=100.0, target_price=110.0,
            stop_loss_price=95.0, position_size=1, holding_period="3d",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("long_term", "CNC", 5)):
            result = await signal_skill.execute()

        sig = result.data["signals"][0]
        entry = sig["entry_price"]
        assert sig["target_price"] > entry
        assert sig["stop_loss_price"] < entry


class TestSellHoldingsAdjustment:
    """Test that SELL signals are forced to MIS/intraday when user doesn't hold the stock."""

    def test_sell_without_holdings_on_intraday_decision_is_mis_short(self):
        """When the per-symbol decision is already intraday, a non-held SELL
        becomes an MIS short. (Intraday strategy mode, or balanced mode where
        the intraday model won.)"""
        from yolovest.strategy.holding_period import adjust_sell_for_holdings

        result = adjust_sell_for_holdings(
            "SELL", "intraday", "MIS", "BEL",
            held_symbols=set(), expected_days=0,
        )
        assert result == ("intraday", "MIS", 0)

    def test_sell_without_holdings_on_swing_decision_is_dropped(self):
        """When the per-symbol decision is swing (short_term, long_term, week,
        positional), a non-held SELL would have to be converted to intraday
        MIS — mixing swing geometry with an intraday horizon. Drop instead."""
        from yolovest.strategy.holding_period import adjust_sell_for_holdings

        assert adjust_sell_for_holdings(
            "SELL", "short_term", "CNC", "BEL",
            held_symbols=set(), expected_days=4,
        ) is None

    def test_sell_with_holdings_keeps_cnc(self):
        from yolovest.strategy.holding_period import adjust_sell_for_holdings

        hp, product, days = adjust_sell_for_holdings("SELL", "short_term", "CNC", "BEL", held_symbols={"BEL", "TCS"}, expected_days=4)
        assert hp == "short_term"
        assert product == "CNC"
        assert days == 4

    def test_sell_long_term_without_holdings_dropped(self):
        """Long-term mode: holding_period == "long_term", never intraday.
        Non-held SELL gets dropped to avoid silently converting to an
        intraday short that contradicts the chosen strategy."""
        from yolovest.strategy.holding_period import adjust_sell_for_holdings

        assert adjust_sell_for_holdings(
            "SELL", "long_term", "CNC", "RELIANCE",
            held_symbols=set(), expected_days=10,
        ) is None

    def test_buy_unaffected_regardless_of_holdings(self):
        from yolovest.strategy.holding_period import adjust_sell_for_holdings

        hp, product, days = adjust_sell_for_holdings("BUY", "short_term", "CNC", "RELIANCE", held_symbols=set(), expected_days=4)
        assert hp == "short_term"
        assert product == "CNC"
        assert days == 4

    def test_hold_unaffected(self):
        from yolovest.strategy.holding_period import adjust_sell_for_holdings

        hp, product, days = adjust_sell_for_holdings("HOLD", "long_term", "CNC", "TCS", held_symbols=set(), expected_days=10)
        assert hp == "long_term"
        assert product == "CNC"
        assert days == 10

    async def test_sell_signal_dropped_on_swing_horizon(self, signal_skill):
        """Full pipeline: non-held SELL on a swing horizon is dropped — we
        don't silently convert a short_term setup into an intraday MIS short."""
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "BEL"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.db.get_open_positions = AsyncMock(return_value=[])
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="SELL", entry_price=400.0, target_price=380.0,
            stop_loss_price=415.0, position_size=1, holding_period="3d",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        # The contract: no MIS short emitted for a swing-decided SELL on
        # a non-held symbol. (Whether the signal is dropped specifically
        # at adjust_sell_for_holdings or earlier in the filter chain
        # depends on fixture details; what matters is signals_generated.)
        assert result.data["signals_generated"] == 0

    async def test_sell_signal_keeps_cnc_when_held(self, signal_skill):
        """Full pipeline: SELL signal for held stock keeps CNC."""
        signal_skill.ctx.config.risk.skip_sell_on_holdings = False
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "BEL"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(60))
        signal_skill.ctx.db.insert_signal = AsyncMock()
        signal_skill.ctx.db.get_open_positions = AsyncMock(return_value=[
            {"symbol": "BEL", "quantity": 10},
        ])
        signal_skill.ctx.ml.predict_swing = AsyncMock(return_value=MLPrediction(
            signal_type="SELL", entry_price=400.0, target_price=380.0,
            stop_loss_price=415.0, position_size=1, holding_period="3d",
            confidence=0.85, model_version="test-v1",
        ))

        with patch("yolovest.strategy.signal_evaluator.decide_holding_period", return_value=("short_term", "CNC", 3)):
            result = await signal_skill.execute()

        assert result.data["signals_generated"] == 1
        sig = result.data["signals"][0]
        assert sig["product"] == "CNC"
        assert sig["expected_holding_period"] == "short_term"
