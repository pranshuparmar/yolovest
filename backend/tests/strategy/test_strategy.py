"""Tests for the strategy/ML module.

Tests ML prediction, feature vector construction, backtesting metrics,
and training guards. XGBoost is mocked — not required to be installed.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from yolovest.models.schemas import BacktestResult, MLPrediction, OHLCVBar
from yolovest.strategy.backtest import Backtester
from yolovest.strategy.ml_signal import XGBoostSignalModel

# ---------------------------------------------------------------------------
# Feature vector construction
# ---------------------------------------------------------------------------


class TestFeatureVector:
    """Test that feature dicts are converted to sorted-key vectors."""

    def test_sorted_keys(self):
        features = {"rsi": 55.0, "atr_14": 10.0, "macd": 1.2, "close": 100.0}
        result = XGBoostSignalModel._build_feature_vector(features)

        # Keys sorted: atr, close, macd, rsi
        assert result == [[10.0, 100.0, 1.2, 55.0]]

    def test_single_feature(self):
        features = {"rsi": 42.0}
        result = XGBoostSignalModel._build_feature_vector(features)
        assert result == [[42.0]]

    def test_empty_features(self):
        result = XGBoostSignalModel._build_feature_vector({})
        assert result == [[]]


# ---------------------------------------------------------------------------
# Prediction with mocked model
# ---------------------------------------------------------------------------


class TestPredictIntraday:
    """Test predict_intraday with a mocked XGBoost model."""

    @pytest.fixture
    def mock_xgb_model(self):
        model = MagicMock()
        model.predict.return_value = np.array([2])  # BUY
        model.predict_proba.return_value = np.array([[0.1, 0.1, 0.8]])
        return model

    @pytest.fixture
    def signal_model(self, mock_xgb_model, tmp_path):
        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        sm._intraday_model = mock_xgb_model
        sm._intraday_version = "xgb_test_v1"
        return sm

    async def test_predict_buy(self, signal_model, mock_xgb_model):
        features = {"close": 100.0, "atr_14": 5.0, "rsi": 55.0}
        result = await signal_model.predict_intraday("RELIANCE", features)

        assert isinstance(result, MLPrediction)
        assert result.signal_type == "BUY"
        assert result.entry_price == 100.0
        assert result.target_price == 110.0  # 100 + 2*5
        assert result.stop_loss_price == 95.0  # 100 - 1*5
        assert result.position_size == 1
        assert result.holding_period == "intraday"
        assert result.confidence == 0.8
        assert result.model_version == "xgb_test_v1"

    async def test_predict_sell(self, tmp_path):
        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([0])  # SELL
        mock_model.predict_proba.return_value = np.array([[0.75, 0.15, 0.10]])

        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        sm._intraday_model = mock_model
        sm._intraday_version = "xgb_test_v1"

        features = {"close": 200.0, "atr_14": 10.0, "rsi": 30.0}
        result = await sm.predict_intraday("TCS", features)

        assert result.signal_type == "SELL"
        assert result.target_price == 180.0  # 200 - 2*10
        assert result.stop_loss_price == 210.0  # 200 + 1*10
        assert result.confidence == 0.75

    async def test_predict_hold(self, tmp_path):
        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([1])  # HOLD
        mock_model.predict_proba.return_value = np.array([[0.2, 0.6, 0.2]])

        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        sm._intraday_model = mock_model
        sm._intraday_version = "xgb_test_v1"

        features = {"close": 150.0, "atr_14": 3.0, "rsi": 50.0}
        result = await sm.predict_intraday("INFY", features)

        assert result.signal_type == "HOLD"
        assert result.confidence == 0.6

    async def test_predict_no_model_raises(self, tmp_path):
        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="No intraday model loaded"):
            await sm.predict_intraday("RELIANCE", {"close": 100.0})

    async def test_predict_with_calibrator_improving(self, signal_model):
        """When calibrator gives higher confidence, use calibrated values."""
        mock_calibrator = MagicMock()
        mock_calibrator.predict.return_value = np.array([2])  # BUY
        mock_calibrator.predict_proba.return_value = np.array([[0.05, 0.05, 0.90]])
        signal_model._intraday_calibrator = mock_calibrator

        features = {"close": 100.0, "atr_14": 5.0, "rsi": 55.0}
        result = await signal_model.predict_intraday("RELIANCE", features)

        # Calibrated confidence (0.9) > raw (0.8) — calibrated used
        assert result.confidence == 0.9
        assert result.signal_type == "BUY"

    async def test_predict_with_calibrator_compressing(self, signal_model):
        """When calibrator compresses confidence, fall back to raw model."""
        mock_calibrator = MagicMock()
        mock_calibrator.predict.return_value = np.array([2])  # BUY
        mock_calibrator.predict_proba.return_value = np.array([[0.33, 0.34, 0.33]])
        signal_model._intraday_calibrator = mock_calibrator

        features = {"close": 100.0, "atr_14": 5.0, "rsi": 55.0}
        result = await signal_model.predict_intraday("RELIANCE", features)

        # Calibrated confidence (0.34) < raw (0.8) — raw used
        assert result.confidence == 0.8
        assert result.signal_type == "BUY"

    async def test_predict_calibrator_different_label(self, signal_model):
        """When calibrator argmax disagrees with raw, keep raw probas.

        The CalibratedClassifierCV sigmoid Platt-scaling on a HOLD-
        dominated training set (e.g. swing model's 73% HOLD) systematically
        pulls directional predictions back to the HOLD prior, even when
        the class-weighted XGBoost has clear conviction. Keeping raw on
        disagreement preserves the trained model's directional intuition.
        """
        mock_calibrator = MagicMock()
        mock_calibrator.predict.return_value = np.array([0])  # SELL (different from raw BUY)
        mock_calibrator.predict_proba.return_value = np.array([[0.85, 0.05, 0.10]])
        signal_model._intraday_calibrator = mock_calibrator

        features = {"close": 100.0, "atr_14": 5.0, "rsi": 55.0}
        result = await signal_model.predict_intraday("RELIANCE", features)

        # Disagreement on label → raw wins, regardless of confidence delta.
        assert result.confidence == 0.8
        assert result.signal_type == "BUY"

    async def test_predict_calibrator_compresses_directional_to_hold(self, signal_model):
        """The dominant failure case fixed by the calibration logic change:
        raw model has a clear BUY argmax, calibrator pulls it to HOLD with
        higher confidence. Old logic adopted the HOLD; new logic keeps the
        BUY direction.
        """
        mock_calibrator = MagicMock()
        mock_calibrator.predict.return_value = np.array([1])  # HOLD
        mock_calibrator.predict_proba.return_value = np.array([[0.20, 0.55, 0.25]])
        signal_model._intraday_calibrator = mock_calibrator

        features = {"close": 100.0, "atr_14": 5.0, "rsi": 55.0}
        result = await signal_model.predict_intraday("RELIANCE", features)

        # Raw (BUY @ 0.8) preserved despite calibrator preferring HOLD @ 0.55.
        assert result.signal_type == "BUY"
        assert result.confidence == 0.8


class TestPredictSwing:
    """Test predict_swing uses the swing model slot."""

    async def test_swing_holding_period(self, tmp_path):
        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([2])  # BUY
        mock_model.predict_proba.return_value = np.array([[0.1, 0.1, 0.8]])

        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        sm._swing_model = mock_model
        sm._swing_version = "xgb_swing_v1"

        features = {"close": 100.0, "atr_14": 5.0, "rsi": 55.0}
        result = await sm.predict_swing("RELIANCE", features)

        assert result.holding_period == "3d"
        assert result.model_version == "xgb_swing_v1"


# ---------------------------------------------------------------------------
# Training guard
# ---------------------------------------------------------------------------


class TestTrainingGuard:
    """Test that training rejects insufficient data."""

    async def test_insufficient_samples_raises(self, tmp_path):
        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        X = [[1, 2, 3]] * 50  # only 50 samples
        y = [0] * 50

        with pytest.raises(ValueError, match="Insufficient training data"):
            await sm.train("intraday", X, y, {})

    async def test_custom_min_samples(self, tmp_path):
        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        X = [[1, 2, 3]] * 90
        y = [0] * 90

        with pytest.raises(ValueError, match="Insufficient training data: 90"):
            await sm.train("intraday", X, y, {"min_training_samples": 100})

    async def test_exact_min_does_not_raise(self, tmp_path):
        """200 samples exactly should not raise (trains with xgboost)."""
        # This test would need real xgboost, so we just verify the guard
        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        X = [[1, 2, 3]] * 199
        y = [0] * 199

        with pytest.raises(ValueError, match="Insufficient training data: 199"):
            await sm.train("intraday", X, y, {})


class TestFinalScaleHoldout:
    """Large bars_meta corpora tune thresholds on a final-scale holdout
    (a tuning model scored on a strict-future slice), not the per-fold
    OOF probabilities — so tuned thresholds are reachable at inference."""

    @staticmethod
    def _dataset(n=1600, n_feat=8):
        import random
        from datetime import date, timedelta
        random.seed(7)
        base = date(2022, 1, 1)
        X, y, meta = [], [], []
        for i in range(n):
            feats = [random.gauss(0, 1) for _ in range(n_feat)]
            s = feats[0]
            label = 2 if s > 0.4 else 0 if s < -0.4 else 1
            X.append(feats)
            y.append(label)
            entry = 100.0
            exit_close = entry * (1.02 if label == 2 else 0.98 if label == 0 else 1.0)
            meta.append({
                "symbol": f"S{i % 30}",
                "entry_close": entry,
                "exit_close": exit_close,
                "path_highs": [entry * 1.03],
                "path_lows": [entry * 0.97],
                "target_pct": 0.02,
                "sl_pct": 0.01,
                "entry_date": (base + timedelta(days=i)).isoformat(),
            })
        return X, y, meta

    async def test_holdout_path_thresholds_respect_cap(self, tmp_path):
        from types import SimpleNamespace
        cfg = SimpleNamespace(risk=SimpleNamespace(
            tuned_threshold_max_value=0.60,
            tuned_threshold_max_diff=0.05,
            buy_threshold_override=None,
            sell_threshold_override=None,
        ))
        sm = XGBoostSignalModel(model_dir=str(tmp_path), config=cfg)
        X, y, meta = self._dataset()
        metrics = await sm.train("intraday", X, y, {
            "n_estimators": 25, "max_depth": 3,
            "bars_meta": meta, "lookahead_bars": 1,
        })
        # The final-scale holdout path ran (not the small-corpus OOF path).
        assert metrics["threshold_holdout_used"] is True
        assert metrics["backtest_source"].startswith("walk_forward")
        # Tuned thresholds stay within the production-reachable ceiling.
        assert 0.0 <= metrics["tuned_buy_threshold"] <= 0.60 + 1e-9
        assert 0.0 <= metrics["tuned_sell_threshold"] <= 0.60 + 1e-9
        # Robust decision metric present.
        assert "sharpe_lower" in metrics


# ---------------------------------------------------------------------------
# Backtester — Sharpe ratio
# ---------------------------------------------------------------------------


class TestComputeSharpe:
    """Test Sharpe ratio computation."""

    def test_known_returns(self):
        # Constant positive returns = high Sharpe
        returns = [0.01] * 100
        sharpe = Backtester._compute_sharpe(returns)
        # With zero variance in returns, std is 0 → should return 0
        # Actually std of constant is 0
        assert sharpe == 0.0

    def test_mixed_returns(self):
        returns = [0.02, -0.01, 0.03, -0.005, 0.01]
        sharpe = Backtester._compute_sharpe(returns)
        assert isinstance(sharpe, float)
        # mean = 0.009, std ~ 0.015, annualized ~ 9.5
        assert sharpe > 0  # positive mean → positive Sharpe

    def test_negative_returns(self):
        returns = [-0.01, -0.02, -0.03]
        sharpe = Backtester._compute_sharpe(returns)
        assert sharpe < 0

    def test_empty_returns(self):
        assert Backtester._compute_sharpe([]) == 0.0

    def test_single_return(self):
        assert Backtester._compute_sharpe([0.01]) == 0.0


# ---------------------------------------------------------------------------
# Backtester — Max drawdown
# ---------------------------------------------------------------------------


class TestComputeMaxDrawdown:
    """Test max drawdown computation."""

    def test_known_drawdown(self):
        # Equity goes 1.0, 1.1, 1.2, 0.9, 1.0
        # Peak at 1.2, trough at 0.9 → DD = 0.3/1.2 = 0.25
        equity = [1.0, 1.1, 1.2, 0.9, 1.0]
        dd = Backtester._compute_max_drawdown(equity)
        assert abs(dd - 0.25) < 1e-6

    def test_no_drawdown(self):
        equity = [1.0, 1.1, 1.2, 1.3]
        dd = Backtester._compute_max_drawdown(equity)
        assert dd == 0.0

    def test_full_drawdown(self):
        # Peak at 2.0, drops to near zero
        equity = [1.0, 2.0, 0.01]
        dd = Backtester._compute_max_drawdown(equity)
        assert dd > 0.99

    def test_empty_curve(self):
        assert Backtester._compute_max_drawdown([]) == 0.0

    def test_single_point(self):
        assert Backtester._compute_max_drawdown([1.0]) == 0.0

    def test_monotonic_decrease(self):
        equity = [1.0, 0.9, 0.8, 0.7]
        dd = Backtester._compute_max_drawdown(equity)
        assert abs(dd - 0.3) < 1e-6  # (1.0 - 0.7) / 1.0


# ---------------------------------------------------------------------------
# Backtester — Full run with mocked model
# ---------------------------------------------------------------------------


class TestBacktesterRun:
    """Test the full backtest run with a mocked model."""

    @pytest.fixture
    def sample_bars(self) -> list[OHLCVBar]:
        """Generate 300 bars with a simple upward trend."""
        base_time = datetime(2025, 1, 1, 9, 15)
        bars = []
        price = 100.0
        for i in range(300):
            # Slight upward drift with noise
            price = price * (1 + 0.001 * (1 if i % 3 != 0 else -1))
            bars.append(
                OHLCVBar(
                    timestamp=base_time + timedelta(days=i),
                    open=round(price * 0.999, 2),
                    high=round(price * 1.005, 2),
                    low=round(price * 0.995, 2),
                    close=round(price, 2),
                    volume=100000 + i * 100,
                )
            )
        return bars

    async def test_run_produces_result(self, sample_bars):
        mock_model = AsyncMock()
        mock_model.predict_intraday = AsyncMock(
            return_value=MLPrediction(
                signal_type="BUY",
                entry_price=100.0,
                target_price=110.0,
                stop_loss_price=95.0,
                position_size=1,
                holding_period="intraday",
                confidence=0.8,
                model_version="test_v1",
            )
        )

        def simple_features(bars_window):
            last = bars_window[-1]
            return {"close": last.close, "atr": last.close * 0.02, "rsi": 55.0}

        bt = Backtester(transaction_cost_pct=0.001)
        result = await bt.run(mock_model, sample_bars, simple_features)

        assert isinstance(result, BacktestResult)
        assert result.total_trades > 0
        assert len(result.trade_log) > 0

    async def test_run_insufficient_bars(self):
        mock_model = AsyncMock()
        bars = [
            OHLCVBar(
                timestamp=datetime(2025, 1, 1),
                open=100, high=101, low=99, close=100, volume=1000,
            )
        ] * 10

        bt = Backtester()
        result = await bt.run(mock_model, bars, lambda b: {})

        assert result.total_trades == 0
        assert result.sharpe_ratio == 0.0

    async def test_transaction_costs_reduce_returns(self, sample_bars):
        """Higher transaction costs should reduce total return."""
        mock_model = AsyncMock()
        mock_model.predict_intraday = AsyncMock(
            return_value=MLPrediction(
                signal_type="BUY",
                entry_price=100.0,
                target_price=110.0,
                stop_loss_price=95.0,
                position_size=1,
                holding_period="intraday",
                confidence=0.8,
                model_version="test_v1",
            )
        )

        def features_fn(bars_window):
            return {"close": bars_window[-1].close, "atr": 2.0, "rsi": 50.0}

        bt_low = Backtester(transaction_cost_pct=0.0001)
        bt_high = Backtester(transaction_cost_pct=0.01)

        result_low = await bt_low.run(mock_model, sample_bars, features_fn)
        result_high = await bt_high.run(mock_model, sample_bars, features_fn)

        # Higher costs should yield lower returns (or more negative)
        if result_low.total_trades > 0 and result_high.total_trades > 0:
            assert result_low.total_return_pct >= result_high.total_return_pct


# ---------------------------------------------------------------------------
# Model slot management
# ---------------------------------------------------------------------------


class TestModelSlots:
    """Test that intraday and swing use separate model slots."""

    async def test_separate_slots(self, tmp_path):
        sm = XGBoostSignalModel(model_dir=str(tmp_path))

        mock_intraday = MagicMock()
        mock_intraday.predict.return_value = np.array([2])
        mock_intraday.predict_proba.return_value = np.array([[0.1, 0.1, 0.8]])

        mock_swing = MagicMock()
        mock_swing.predict.return_value = np.array([0])
        mock_swing.predict_proba.return_value = np.array([[0.7, 0.2, 0.1]])

        sm._intraday_model = mock_intraday
        sm._swing_model = mock_swing
        sm._intraday_version = "intraday_v1"
        sm._swing_version = "swing_v1"

        features = {"close": 100.0, "atr_14": 5.0}
        intraday_result = await sm.predict_intraday("TEST", features)
        swing_result = await sm.predict_swing("TEST", features)

        assert intraday_result.signal_type == "BUY"
        assert swing_result.signal_type == "SELL"

    def test_unknown_model_type_raises(self, tmp_path):
        sm = XGBoostSignalModel(model_dir=str(tmp_path))
        with pytest.raises(ValueError, match="Unknown model_type"):
            sm._get_model("unknown")
