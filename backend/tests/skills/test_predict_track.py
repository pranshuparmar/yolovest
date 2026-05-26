"""Tests for predict-track skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.skills.predict_track import PredictTrackSkill


@pytest.fixture
def predict_skill(app_context):
    return PredictTrackSkill(app_context)


@pytest.fixture
def base_signal():
    return {
        "symbol": "RELIANCE",
        "signal_type": "BUY",
        "entry_price": 2500.0,
        "target_price": 2600.0,
        "stop_loss_price": 2450.0,
        "position_size": 10,
        "confidence_score": 0.85,
        "expected_holding_period": "intraday",
        "model_version": "xgb-v1.0",
    }


class TestLogPrediction:
    async def test_log_prediction(self, predict_skill, base_signal):
        result = await predict_skill.execute(mode="log", signal=base_signal)

        assert result.success
        assert result.data["mode"] == "log"
        assert result.data["prediction_id"] == "P-test001"
        predict_skill.ctx.db.insert_prediction.assert_awaited_once()

    async def test_log_prediction_with_trade_id(self, predict_skill, base_signal):
        result = await predict_skill.execute(
            mode="log", signal=base_signal, trade_id="T-001"
        )

        assert result.success
        call_args = predict_skill.ctx.db.insert_prediction.call_args
        prediction = call_args[0][0]
        assert prediction["trade_id"] == "T-001"

    async def test_log_captures_all_fields(self, predict_skill, base_signal):
        await predict_skill.execute(mode="log", signal=base_signal)

        call_args = predict_skill.ctx.db.insert_prediction.call_args
        prediction = call_args[0][0]
        assert prediction["symbol"] == "RELIANCE"
        assert prediction["predicted_direction"] == "BUY"
        assert prediction["confidence"] == 0.85
        assert prediction["predicted_target"] == 2600.0
        assert prediction["model_version"] == "xgb-v1.0"


class TestScorePredictions:
    async def test_score_no_pending(self, predict_skill):
        result = await predict_skill.execute(mode="score")

        assert result.success
        assert result.data["predictions_scored"] == 0

    async def test_score_correct_buy(self, predict_skill):
        # Scored against the END-date holding window, not today's LTP. The
        # window bar's close (2620) clears entry and its high (2630) reaches
        # the 2600 target.
        pending = [{
            "id": "P-001",
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "entry_price": 2500.0,
            "predicted_target": 2600.0,
            "predicted_stop_loss": 2450.0,
            "confidence": 0.85,
            "created_at": "2026-02-28T10:00:00",
            "prediction_end_time": "2026-03-01T15:30:00",
        }]
        predict_skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        predict_skill.ctx.db.get_daily_ohlc_between = AsyncMock(
            return_value=[(2510.0, 2630.0, 2505.0, 2620.0, "2026-03-01")]
        )

        result = await predict_skill.execute(mode="score")

        assert result.data["predictions_scored"] == 1
        assert result.data["correct"] == 1
        assert result.data["accuracy"] == 1.0

        # Verify score_prediction was called with correct values
        call_args = predict_skill.ctx.db.score_prediction.call_args
        assert call_args[1]["actual_price"] == 2620.0
        assert call_args[1]["direction_correct"] is True
        assert call_args[1]["target_hit"] is True
        assert call_args[1]["actual_pnl_pct"] == pytest.approx(0.048, abs=0.001)

    async def test_score_incorrect_buy(self, predict_skill):
        # Window close (2440) is below entry and target never touched.
        pending = [{
            "id": "P-002",
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "entry_price": 2500.0,
            "predicted_target": 2600.0,
            "predicted_stop_loss": 2450.0,
            "created_at": "2026-02-28T10:00:00",
            "prediction_end_time": "2026-03-01T15:30:00",
        }]
        predict_skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        predict_skill.ctx.db.get_daily_ohlc_between = AsyncMock(
            return_value=[(2490.0, 2495.0, 2435.0, 2440.0, "2026-03-01")]
        )

        result = await predict_skill.execute(mode="score")

        assert result.data["predictions_scored"] == 1
        assert result.data["correct"] == 0

        call_args = predict_skill.ctx.db.score_prediction.call_args
        assert call_args[1]["direction_correct"] is False
        assert call_args[1]["target_hit"] is False

    async def test_score_correct_sell(self, predict_skill):
        pending = [{
            "id": "P-003",
            "symbol": "TCS",
            "predicted_direction": "SELL",
            "entry_price": 3500.0,
            "predicted_target": 3400.0,
            "predicted_stop_loss": 3550.0,
            "created_at": "2026-02-28T10:00:00",
            "prediction_end_time": "2026-03-01T15:30:00",
        }]
        predict_skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        predict_skill.ctx.db.get_daily_ohlc_between = AsyncMock(
            return_value=[(3490.0, 3500.0, 3380.0, 3385.0, "2026-03-01")]
        )

        await predict_skill.execute(mode="score")

        call_args = predict_skill.ctx.db.score_prediction.call_args
        assert call_args[1]["direction_correct"] is True
        assert call_args[1]["target_hit"] is True

    async def test_score_refreshes_scoreboard(self, predict_skill):
        pending = [{
            "id": "P-004",
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "entry_price": 2500.0,
            "predicted_target": 2600.0,
            "created_at": "2026-02-28T10:00:00",
            "prediction_end_time": "2026-03-01T15:30:00",
        }]
        predict_skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        predict_skill.ctx.db.get_daily_ohlc_between = AsyncMock(
            return_value=[(2510.0, 2560.0, 2505.0, 2550.0, "2026-03-01")]
        )

        await predict_skill.execute(mode="score")

        predict_skill.ctx.db.refresh_prediction_scoreboard.assert_awaited_once()

    async def test_score_skips_when_end_date_bar_missing(self, predict_skill):
        # No OHLCV for the holding window yet → left pending, NOT scored
        # against a stale current price.
        pending = [{
            "id": "P-005",
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "entry_price": 2500.0,
            "predicted_target": 2600.0,
            "created_at": "2026-02-28T10:00:00",
            "prediction_end_time": "2026-03-01T15:30:00",
        }]
        predict_skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        predict_skill.ctx.db.get_daily_ohlc_between = AsyncMock(return_value=[])
        predict_skill.ctx.db.get_daily_bar_on = AsyncMock(return_value=None)

        result = await predict_skill.execute(mode="score")

        assert result.data["predictions_scored"] == 0
        assert result.data["skipped_awaiting_data"] == 1
        predict_skill.ctx.db.score_prediction.assert_not_called()

    async def test_default_mode_is_score(self, predict_skill):
        result = await predict_skill.execute()

        assert result.data["mode"] == "score"
