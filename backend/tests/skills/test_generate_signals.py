"""Tests for generate-signals skill diagnostics."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from yolovest.models.schemas import MLPrediction, OHLCVBar
from yolovest.skills.generate_signals import GenerateSignalsSkill


def _make_bars(n: int) -> list[OHLCVBar]:
    return [
        OHLCVBar(
            timestamp=datetime(2026, 1, 1 + i % 28 + 1),
            open=100.0, high=105.0, low=95.0, close=102.0, volume=10000,
        )
        for i in range(n)
    ]


@pytest.fixture
def signal_skill(app_context):
    app_context.ml = AsyncMock()
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

        with patch.object(signal_skill, "_should_use_intraday_model", return_value=False):
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

        with patch.object(signal_skill, "_should_use_intraday_model", return_value=False):
            result = await signal_skill.execute()

        assert result.success
        assert result.data["signals_generated"] == 0
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["low_confidence"] == 2
        assert diag["min_confidence_threshold"] == 0.65
        assert all(r["reason"] == "low_confidence" for r in diag["rejection_details"])

    async def test_insufficient_bars_counted(self, signal_skill):
        signal_skill.ctx.db.get_combined_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"},
        ])
        signal_skill.ctx.db.get_ohlcv = AsyncMock(return_value=_make_bars(30))

        with patch.object(signal_skill, "_should_use_intraday_model", return_value=False):
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

        with patch.object(signal_skill, "_should_use_intraday_model", return_value=False):
            result = await signal_skill.execute()

        assert result.success
        assert result.data["signals_generated"] == 1
        diag = result.data["diagnostics"]
        assert diag["filter_counts"]["passed"] == 1
        assert diag["filter_counts"]["hold_signal"] == 0
