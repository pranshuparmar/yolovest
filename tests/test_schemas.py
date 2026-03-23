"""Tests for Pydantic data models in models/schemas.py.

Tests valid construction, validation errors for invalid data,
and edge cases for all inter-skill data contracts.
"""

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from yolovest.models.schemas import (
    OHLCVBar,
    PortfolioState,
    Position,
    Prediction,
    PremarketContext,
    SentimentResult,
    Signal,
    Trade,
    TradeContext,
    TradeReview,
)

# ── Signal ──────────────────────────────────────────────────────────


class TestSignalValid:
    def test_signal_valid_construction(self, sample_signal):
        assert sample_signal.symbol == "RELIANCE"
        assert sample_signal.signal_type == "BUY"
        assert sample_signal.confidence_score == 0.85

    def test_signal_hold_type(self):
        sig = Signal(
            symbol="TCS",
            signal_type="HOLD",
            entry_price=3500.0,
            target_price=3600.0,
            stop_loss_price=3400.0,
            position_size=5,
            expected_holding_period="3d",
            confidence_score=0.50,
            model_version="lgbm-v1.0",
        )
        assert sig.signal_type == "HOLD"

    def test_signal_boundary_confidence_zero(self):
        sig = Signal(
            symbol="INFY",
            signal_type="BUY",
            entry_price=1500.0,
            target_price=1600.0,
            stop_loss_price=1450.0,
            position_size=1,
            expected_holding_period="intraday",
            confidence_score=0.0,
            model_version="v1",
        )
        assert sig.confidence_score == 0.0

    def test_signal_boundary_confidence_one(self):
        sig = Signal(
            symbol="INFY",
            signal_type="SELL",
            entry_price=1500.0,
            target_price=1400.0,
            stop_loss_price=1550.0,
            position_size=1,
            expected_holding_period="intraday",
            confidence_score=1.0,
            model_version="v1",
        )
        assert sig.confidence_score == 1.0

    def test_signal_empty_features_snapshot(self):
        sig = Signal(
            symbol="HDFC",
            signal_type="BUY",
            entry_price=100.0,
            target_price=110.0,
            stop_loss_price=95.0,
            position_size=10,
            expected_holding_period="1w",
            confidence_score=0.7,
            model_version="v1",
        )
        assert sig.features_snapshot == {}


class TestSignalInvalid:
    def test_signal_rejects_confidence_above_one(self):
        with pytest.raises(ValidationError, match="confidence_score"):
            Signal(
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                target_price=2600.0,
                stop_loss_price=2450.0,
                position_size=10,
                expected_holding_period="intraday",
                confidence_score=1.1,
                model_version="v1",
            )

    def test_signal_rejects_confidence_below_zero(self):
        with pytest.raises(ValidationError, match="confidence_score"):
            Signal(
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                target_price=2600.0,
                stop_loss_price=2450.0,
                position_size=10,
                expected_holding_period="intraday",
                confidence_score=-0.1,
                model_version="v1",
            )

    def test_signal_rejects_invalid_signal_type(self):
        with pytest.raises(ValidationError, match="signal_type"):
            Signal(
                symbol="RELIANCE",
                signal_type="INVALID",
                entry_price=2500.0,
                target_price=2600.0,
                stop_loss_price=2450.0,
                position_size=10,
                expected_holding_period="intraday",
                confidence_score=0.8,
                model_version="v1",
            )

    def test_signal_rejects_zero_entry_price(self):
        with pytest.raises(ValidationError, match="entry_price"):
            Signal(
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=0.0,
                target_price=2600.0,
                stop_loss_price=2450.0,
                position_size=10,
                expected_holding_period="intraday",
                confidence_score=0.8,
                model_version="v1",
            )

    def test_signal_rejects_negative_entry_price(self):
        with pytest.raises(ValidationError, match="entry_price"):
            Signal(
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=-100.0,
                target_price=2600.0,
                stop_loss_price=2450.0,
                position_size=10,
                expected_holding_period="intraday",
                confidence_score=0.8,
                model_version="v1",
            )

    def test_signal_rejects_zero_position_size(self):
        with pytest.raises(ValidationError, match="position_size"):
            Signal(
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                target_price=2600.0,
                stop_loss_price=2450.0,
                position_size=0,
                expected_holding_period="intraday",
                confidence_score=0.8,
                model_version="v1",
            )

    def test_signal_rejects_missing_symbol(self):
        with pytest.raises(ValidationError):
            Signal(
                signal_type="BUY",
                entry_price=2500.0,
                target_price=2600.0,
                stop_loss_price=2450.0,
                position_size=10,
                expected_holding_period="intraday",
                confidence_score=0.8,
                model_version="v1",
            )


# ── Trade ───────────────────────────────────────────────────────────


class TestTradeValid:
    def test_trade_valid_construction(self, sample_trade):
        assert sample_trade.trade_id == "T-001"
        assert sample_trade.product == "MIS"
        assert sample_trade.mode == "paper"
        assert sample_trade.status == "filled"

    def test_trade_cnc_product(self):
        trade = Trade(
            trade_id="T-002",
            symbol="TCS",
            signal_type="SELL",
            entry_price=3500.0,
            fill_price=3499.0,
            quantity=5,
            stop_loss_price=3550.0,
            target_price=3400.0,
            product="CNC",
            mode="live",
            status="open",
        )
        assert trade.product == "CNC"
        assert trade.mode == "live"

    def test_trade_none_optionals(self):
        trade = Trade(
            trade_id="T-003",
            symbol="INFY",
            signal_type="BUY",
            entry_price=1500.0,
            fill_price=1500.0,
            quantity=10,
            stop_loss_price=1450.0,
            target_price=1550.0,
            product="MIS",
            mode="paper",
            status="placed",
        )
        assert trade.order_id is None
        assert trade.sl_order_id is None
        assert trade.pnl is None
        assert trade.exit_price is None
        assert trade.closed_at is None

    def test_trade_all_statuses(self):
        for status in ["placed", "open", "partially_filled", "filled", "rejected", "cancelled"]:
            trade = Trade(
                trade_id=f"T-{status}",
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                fill_price=2500.0,
                quantity=1,
                stop_loss_price=2450.0,
                target_price=2550.0,
                product="MIS",
                mode="paper",
                status=status,
            )
            assert trade.status == status

    def test_trade_zero_fill_price_allowed(self):
        """fill_price=0.0 is allowed (ge=0 for paper trades not yet filled)."""
        trade = Trade(
            trade_id="T-ZERO",
            symbol="RELIANCE",
            signal_type="BUY",
            entry_price=2500.0,
            fill_price=0.0,
            quantity=1,
            stop_loss_price=2450.0,
            target_price=2550.0,
            product="MIS",
            mode="paper",
            status="placed",
        )
        assert trade.fill_price == 0.0


class TestTradeInvalid:
    def test_trade_rejects_invalid_status(self):
        with pytest.raises(ValidationError, match="status"):
            Trade(
                trade_id="T-BAD",
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                fill_price=2500.0,
                quantity=10,
                stop_loss_price=2450.0,
                target_price=2550.0,
                product="MIS",
                mode="paper",
                status="invalid_status",
            )

    def test_trade_rejects_invalid_product(self):
        with pytest.raises(ValidationError, match="product"):
            Trade(
                trade_id="T-BAD",
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                fill_price=2500.0,
                quantity=10,
                stop_loss_price=2450.0,
                target_price=2550.0,
                product="NRML",
                mode="paper",
                status="filled",
            )

    def test_trade_rejects_invalid_mode(self):
        with pytest.raises(ValidationError, match="mode"):
            Trade(
                trade_id="T-BAD",
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                fill_price=2500.0,
                quantity=10,
                stop_loss_price=2450.0,
                target_price=2550.0,
                product="MIS",
                mode="simulation",
                status="filled",
            )

    def test_trade_rejects_zero_quantity(self):
        with pytest.raises(ValidationError, match="quantity"):
            Trade(
                trade_id="T-BAD",
                symbol="RELIANCE",
                signal_type="BUY",
                entry_price=2500.0,
                fill_price=2500.0,
                quantity=0,
                stop_loss_price=2450.0,
                target_price=2550.0,
                product="MIS",
                mode="paper",
                status="filled",
            )

    def test_trade_rejects_hold_signal_type(self):
        """Trade should only accept BUY or SELL, not HOLD."""
        with pytest.raises(ValidationError, match="signal_type"):
            Trade(
                trade_id="T-BAD",
                symbol="RELIANCE",
                signal_type="HOLD",
                entry_price=2500.0,
                fill_price=2500.0,
                quantity=10,
                stop_loss_price=2450.0,
                target_price=2550.0,
                product="MIS",
                mode="paper",
                status="filled",
            )


# ── PortfolioState ──────────────────────────────────────────────────


class TestPortfolioStateValid:
    def test_portfolio_state_valid_construction(self, sample_portfolio_state):
        assert sample_portfolio_state.total_capital == 100000.0
        assert sample_portfolio_state.open_positions == 1
        assert sample_portfolio_state.trades_today == 2

    def test_portfolio_state_empty_dicts(self):
        ps = PortfolioState(
            total_capital=50000.0,
            available_cash=50000.0,
            exposure_pct=0.0,
            open_positions=0,
        )
        assert ps.stock_exposures == {}
        assert ps.sector_counts == {}

    def test_portfolio_state_zero_capital(self):
        ps = PortfolioState(
            total_capital=0.0,
            available_cash=0.0,
            exposure_pct=0.0,
            open_positions=0,
        )
        assert ps.total_capital == 0.0


class TestPortfolioStateInvalid:
    def test_portfolio_state_rejects_negative_capital(self):
        with pytest.raises(ValidationError, match="total_capital"):
            PortfolioState(
                total_capital=-1000.0,
                available_cash=0.0,
                exposure_pct=0.0,
                open_positions=0,
            )

    def test_portfolio_state_rejects_negative_cash(self):
        with pytest.raises(ValidationError, match="available_cash"):
            PortfolioState(
                total_capital=100000.0,
                available_cash=-500.0,
                exposure_pct=0.0,
                open_positions=0,
            )

    def test_portfolio_state_rejects_negative_open_positions(self):
        with pytest.raises(ValidationError, match="open_positions"):
            PortfolioState(
                total_capital=100000.0,
                available_cash=80000.0,
                exposure_pct=0.0,
                open_positions=-1,
            )

    def test_portfolio_state_rejects_exposure_above_one(self):
        with pytest.raises(ValidationError, match="exposure_pct"):
            PortfolioState(
                total_capital=100000.0,
                available_cash=0.0,
                exposure_pct=1.5,
                open_positions=5,
            )


# ── TradeReview ─────────────────────────────────────────────────────


class TestTradeReviewValid:
    def test_trade_review_approve(self):
        tr = TradeReview(decision="APPROVE", reasoning="Good risk/reward")
        assert tr.decision == "APPROVE"
        assert tr.adjusted_size is None

    def test_trade_review_reject(self):
        tr = TradeReview(decision="REJECT", reasoning="Too risky")
        assert tr.decision == "REJECT"

    def test_trade_review_resize(self):
        tr = TradeReview(decision="RESIZE", reasoning="Reduce size", adjusted_size=5)
        assert tr.adjusted_size == 5


class TestTradeReviewInvalid:
    def test_trade_review_rejects_invalid_decision(self):
        with pytest.raises(ValidationError, match="decision"):
            TradeReview(decision="MAYBE", reasoning="Not sure")

    def test_trade_review_resize_requires_adjusted_size(self):
        with pytest.raises(ValidationError, match="adjusted_size"):
            TradeReview(decision="RESIZE", reasoning="Reduce size")


# ── SentimentResult ─────────────────────────────────────────────────


class TestSentimentResultValid:
    def test_sentiment_result_valid(self):
        sr = SentimentResult(
            symbol="RELIANCE",
            sentiment="bullish",
            confidence=0.9,
            key_drivers=["strong earnings", "sector tailwind"],
        )
        assert sr.sentiment == "bullish"
        assert len(sr.key_drivers) == 2

    def test_sentiment_result_empty_drivers(self):
        sr = SentimentResult(
            symbol="TCS",
            sentiment="neutral",
            confidence=0.5,
        )
        assert sr.key_drivers == []


class TestSentimentResultInvalid:
    def test_sentiment_rejects_invalid_sentiment(self):
        with pytest.raises(ValidationError, match="sentiment"):
            SentimentResult(
                symbol="RELIANCE",
                sentiment="very_bullish",
                confidence=0.9,
            )

    def test_sentiment_rejects_confidence_above_one(self):
        with pytest.raises(ValidationError, match="confidence"):
            SentimentResult(
                symbol="RELIANCE",
                sentiment="bullish",
                confidence=1.5,
            )

    def test_sentiment_rejects_confidence_below_zero(self):
        with pytest.raises(ValidationError, match="confidence"):
            SentimentResult(
                symbol="RELIANCE",
                sentiment="bearish",
                confidence=-0.1,
            )


# ── OHLCVBar ────────────────────────────────────────────────────────


class TestOHLCVBarValid:
    def test_ohlcv_bar_valid(self):
        bar = OHLCVBar(
            timestamp=datetime.now(),
            open=100.0,
            high=105.0,
            low=99.0,
            close=103.0,
            volume=1000000,
        )
        assert bar.close == 103.0

    def test_ohlcv_bar_zero_volume(self):
        bar = OHLCVBar(
            timestamp=datetime.now(),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=0,
        )
        assert bar.volume == 0


# ── Position ────────────────────────────────────────────────────────


class TestPositionValid:
    def test_position_valid(self, sample_trade):
        pos = Position(
            position_id="P-001",
            trade=sample_trade,
            current_price=2510.0,
            unrealized_pnl=100.0,
            trailing_sl_active=False,
        )
        assert pos.position_id == "P-001"
        assert pos.trade.symbol == "RELIANCE"

    def test_position_defaults(self, sample_trade):
        pos = Position(
            position_id="P-002",
            trade=sample_trade,
            current_price=2500.0,
        )
        assert pos.unrealized_pnl == 0.0
        assert pos.trailing_sl_active is False


class TestPositionInvalid:
    def test_position_rejects_zero_current_price(self, sample_trade):
        with pytest.raises(ValidationError, match="current_price"):
            Position(
                position_id="P-BAD",
                trade=sample_trade,
                current_price=0.0,
            )


# ── Prediction ──────────────────────────────────────────────────────


class TestPredictionValid:
    def test_prediction_valid(self, sample_signal):
        pred = Prediction(
            prediction_id="PR-001",
            signal=sample_signal,
            trade_id="T-001",
            prediction_end_time=datetime.now() + timedelta(hours=6),
        )
        assert pred.prediction_id == "PR-001"
        assert pred.actual_price is None
        assert pred.direction_correct is None

    def test_prediction_auto_computes_end_time(self, sample_signal):
        """Prediction auto-computes prediction_end_time from holding period."""
        pred = Prediction(
            prediction_id="PR-002",
            signal=sample_signal,
        )
        assert pred.prediction_end_time is not None

    def test_prediction_none_optionals(self, sample_signal):
        pred = Prediction(
            prediction_id="PR-003",
            signal=sample_signal,
        )
        assert pred.trade_id is None
        assert pred.target_hit is None
        assert pred.actual_pnl_pct is None


# ── TradeContext ────────────────────────────────────────────────────


class TestTradeContextValid:
    def test_trade_context_valid(self, sample_signal, sample_portfolio_state):
        tc = TradeContext(
            signal=sample_signal,
            portfolio=sample_portfolio_state,
        )
        assert tc.sentiment is None
        assert tc.premarket is None
        assert tc.todays_trades == []

    def test_trade_context_with_sentiment(self, sample_signal, sample_portfolio_state):
        sentiment = SentimentResult(
            symbol="RELIANCE", sentiment="bullish", confidence=0.8
        )
        tc = TradeContext(
            signal=sample_signal,
            portfolio=sample_portfolio_state,
            sentiment=sentiment,
        )
        assert tc.sentiment.sentiment == "bullish"


# ── PremarketContext ────────────────────────────────────────────────


class TestPremarketContextValid:
    def test_premarket_all_none(self):
        pm = PremarketContext()
        assert pm.gift_nifty_change_pct is None
        assert pm.market_bias is None

    def test_premarket_with_values(self):
        pm = PremarketContext(
            gift_nifty_change_pct=0.5,
            us_sp500_change_pct=-0.3,
            market_bias="bullish",
            llm_summary="Markets expected to open higher",
        )
        assert pm.market_bias == "bullish"
