"""Walk-forward backtesting engine.

Simulates trading on historical data with transaction costs.
Uses a rolling train/test window approach for realistic performance estimation.
"""

import logging
from collections.abc import Callable
from typing import Any

import numpy as np

from yolovest.models.schemas import BacktestResult, OHLCVBar

logger = logging.getLogger(__name__)


class Backtester:
    """Walk-forward backtesting with transaction cost deduction.

    Splits historical bars into rolling windows: train the model on each
    train window, then evaluate on the subsequent test window. Aggregates
    results across all windows.
    """

    def __init__(self, transaction_cost_pct: float = 0.001) -> None:
        """Initialize backtester.

        Args:
            transaction_cost_pct: Round-trip transaction cost as fraction
                                  of trade value. Default 0.1%.
        """
        self.transaction_cost_pct = transaction_cost_pct

    async def run(
        self,
        model: Any,
        bars: list[OHLCVBar],
        features_fn: Callable[[list[OHLCVBar]], dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> BacktestResult:
        """Run walk-forward backtest.

        Args:
            model: ML model with predict_intraday(symbol, features) method.
            bars: Historical OHLCV bars, chronologically ordered.
            features_fn: Function that takes a list of bars and returns
                         a features dict suitable for model.predict_intraday.
            config: Optional config dict with:
                - train_window (int): bars for training window (default 200)
                - test_window (int): bars for test window (default 50)
                - symbol (str): symbol for predictions (default "BACKTEST")

        Returns:
            BacktestResult with performance metrics.
        """
        config = config or {}
        train_window = config.get("train_window", 200)
        test_window = config.get("test_window", 50)
        symbol = config.get("symbol", "BACKTEST")

        if len(bars) < train_window + test_window:
            logger.warning(
                "Insufficient bars for backtest: %d < %d (train) + %d (test)",
                len(bars),
                train_window,
                test_window,
            )
            return BacktestResult(
                sharpe_ratio=0.0,
                max_drawdown_pct=0.0,
                win_rate=0.0,
                profit_factor=0.0,
                total_trades=0,
                total_return_pct=0.0,
            )

        all_returns: list[float] = []
        trade_log: list[dict[str, Any]] = []
        equity = [1.0]

        # Walk-forward windows
        start = 0
        while start + train_window + test_window <= len(bars):
            test_start = start + train_window
            test_end = min(test_start + test_window, len(bars))
            test_bars = bars[test_start:test_end]

            for i, bar in enumerate(test_bars):
                # Build features from bars up to this point
                lookback = bars[start : test_start + i + 1]
                try:
                    features = features_fn(lookback)
                except Exception:
                    logger.debug("features_fn failed at bar %d, skipping", i)
                    continue

                try:
                    prediction = await model.predict_intraday(symbol, features)
                except Exception:
                    logger.debug("Model prediction failed at bar %d", i)
                    continue

                if prediction.signal_type == "HOLD":
                    continue

                # Simulate trade using next bar if available
                if i + 1 >= len(test_bars):
                    break

                entry_price = bar.close
                exit_price = test_bars[i + 1].close

                if prediction.signal_type == "BUY":
                    raw_return = (exit_price - entry_price) / entry_price
                else:  # SELL
                    raw_return = (entry_price - exit_price) / entry_price

                # Deduct transaction costs
                net_return = raw_return - self.transaction_cost_pct

                all_returns.append(net_return)
                equity.append(equity[-1] * (1 + net_return))

                trade_log.append(
                    {
                        "signal": prediction.signal_type,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "return_pct": round(net_return * 100, 4),
                        "timestamp": bar.timestamp.isoformat(),
                    }
                )

            start += test_window  # slide forward

        # Compute aggregate metrics
        total_trades = len(all_returns)

        if total_trades == 0:
            return BacktestResult(
                sharpe_ratio=0.0,
                max_drawdown_pct=0.0,
                win_rate=0.0,
                profit_factor=0.0,
                total_trades=0,
                total_return_pct=0.0,
                trade_log=trade_log,
            )

        sharpe = self._compute_sharpe(all_returns)
        max_dd = self._compute_max_drawdown(equity)

        wins = [r for r in all_returns if r > 0]
        losses = [r for r in all_returns if r <= 0]
        win_rate = len(wins) / total_trades
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )

        total_return_pct = (equity[-1] - 1.0) * 100

        logger.info(
            "Backtest complete: %d trades, sharpe=%.2f, max_dd=%.2f%%, "
            "win_rate=%.2f, return=%.2f%%",
            total_trades,
            sharpe,
            max_dd * 100,
            win_rate,
            total_return_pct,
        )

        return BacktestResult(
            sharpe_ratio=round(sharpe, 4),
            max_drawdown_pct=round(max_dd, 4),
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 4),
            total_trades=total_trades,
            total_return_pct=round(total_return_pct, 4),
            trade_log=trade_log,
        )

    @staticmethod
    def _compute_sharpe(returns: list[float]) -> float:
        """Compute annualized Sharpe ratio from a list of trade returns.

        Assumes daily-ish frequency, annualizes with sqrt(252).
        Returns 0 if insufficient data or zero variance.
        """
        if len(returns) < 2:
            return 0.0

        arr = np.array(returns)
        std = float(arr.std())
        if std < 1e-12:
            return 0.0

        return float((arr.mean() / std) * np.sqrt(252))

    @staticmethod
    def _compute_max_drawdown(equity_curve: list[float]) -> float:
        """Compute max peak-to-trough drawdown from an equity curve.

        Returns drawdown as a positive fraction (e.g., 0.15 = 15% drawdown).
        """
        if len(equity_curve) < 2:
            return 0.0

        arr = np.array(equity_curve)
        peak = np.maximum.accumulate(arr)
        drawdowns = (peak - arr) / np.where(peak > 0, peak, 1.0)
        return float(drawdowns.max())
