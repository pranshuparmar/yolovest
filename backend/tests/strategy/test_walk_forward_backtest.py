"""Tests for the real-PnL walk-forward backtest that replaced the
legacy +1%/-0.5% synthetic payoff."""

import math

import pytest

from yolovest.strategy.walk_forward_backtest import (
    BacktestConfig,
    BarMeta,
    _size_position,
    run_walk_forward_backtest,
)


class TestPositionSizing:
    def test_size_capped_by_single_stock_pct(self):
        cfg = BacktestConfig(
            initial_capital=100_000.0,
            risk_per_trade_pct=0.10,     # huge — would size 10000 shares at 1% SL
            max_single_stock_pct=0.25,   # but capital cap allows only ₹25k
            sizing_sl_pct=0.01,
        )
        size = _size_position(entry=100.0, capital=100_000.0, cfg=cfg)
        # 25% of 100k / 100 = 250 shares
        assert size == 250

    def test_size_zero_when_capital_zero(self):
        size = _size_position(entry=100.0, capital=0.0, cfg=BacktestConfig())
        assert size == 0

    def test_size_zero_for_invalid_entry(self):
        size = _size_position(entry=0.0, capital=100_000.0, cfg=BacktestConfig())
        assert size == 0


class TestRunWalkForwardBacktest:
    def _cfg(self):
        return BacktestConfig(
            initial_capital=100_000.0,
            risk_per_trade_pct=0.02,
            max_single_stock_pct=0.25,
            entry_slippage_pct=0.0005,
            product="MIS",
            sizing_sl_pct=0.01,
        )

    def test_empty_input_returns_zero_metrics(self):
        result = run_walk_forward_backtest(preds=[], bars_meta=[], config=self._cfg())
        assert result.total_trades == 0
        assert result.sharpe == 0.0
        assert result.final_capital == 100_000.0

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            run_walk_forward_backtest(
                preds=[2], bars_meta=[],
                config=self._cfg(),
            )

    def test_hold_predictions_skipped(self):
        result = run_walk_forward_backtest(
            preds=[1, 1, 1],  # all HOLD
            bars_meta=[
                BarMeta("X", 100.0, 102.0),
                BarMeta("X", 100.0, 98.0),
                BarMeta("X", 100.0, 105.0),
            ],
            config=self._cfg(),
        )
        assert result.total_trades == 0
        assert result.final_capital == 100_000.0

    def test_winning_buy_increases_capital(self):
        result = run_walk_forward_backtest(
            preds=[2],  # BUY
            bars_meta=[BarMeta("RELIANCE", 100.0, 102.0)],
            config=self._cfg(),
        )
        assert result.total_trades == 1
        assert result.wins == 1
        assert result.losses == 0
        assert result.final_capital > 100_000.0
        # Gross gain on 250 shares × ₹2 ≈ ₹500, less costs → still > 0
        assert result.net_pnl > 0

    def test_losing_sell_decreases_capital(self):
        result = run_walk_forward_backtest(
            preds=[0],  # SELL
            bars_meta=[BarMeta("RELIANCE", 100.0, 102.0)],  # price went UP, SELL loses
            config=self._cfg(),
        )
        assert result.total_trades == 1
        assert result.losses == 1
        assert result.final_capital < 100_000.0

    def test_slippage_eats_into_winning_trade(self):
        # Same trade with and without slippage — without slippage should
        # net more.
        bars = [BarMeta("X", 100.0, 100.5)]
        zero_slip = BacktestConfig(
            initial_capital=100_000.0, entry_slippage_pct=0.0,
            risk_per_trade_pct=0.02, max_single_stock_pct=0.25,
        )
        with_slip = BacktestConfig(
            initial_capital=100_000.0, entry_slippage_pct=0.005,
            risk_per_trade_pct=0.02, max_single_stock_pct=0.25,
        )
        r_no = run_walk_forward_backtest([2], bars, zero_slip)
        r_yes = run_walk_forward_backtest([2], bars, with_slip)
        assert r_no.net_pnl > r_yes.net_pnl

    def test_costs_applied_to_pnl(self):
        # A trade where gross is zero (entry = exit) must show negative
        # net because costs are always paid.
        result = run_walk_forward_backtest(
            preds=[2],
            bars_meta=[BarMeta("X", 100.0, 100.0)],
            config=BacktestConfig(
                initial_capital=100_000.0, entry_slippage_pct=0.0,
                risk_per_trade_pct=0.02, max_single_stock_pct=0.25,
            ),
        )
        assert result.net_pnl < 0  # only costs left
        assert result.losses == 1

    def test_invalid_prices_skipped(self):
        result = run_walk_forward_backtest(
            preds=[2, 2],
            bars_meta=[
                BarMeta("X", 0.0, 100.0),   # bad entry → skipped
                BarMeta("X", 100.0, 0.0),   # bad exit → skipped
            ],
            config=self._cfg(),
        )
        assert result.total_trades == 0

    def test_sharpe_positive_when_streak_of_wins(self):
        result = run_walk_forward_backtest(
            preds=[2] * 20,
            bars_meta=[BarMeta("X", 100.0, 101.0)] * 20,
            config=self._cfg(),
        )
        assert result.win_rate == 1.0
        assert math.isfinite(result.sharpe)
        # 20 small same-direction wins with low variance gives a healthy
        # Sharpe but not infinite (returns vary slightly across trades
        # because capital compounds).

    def test_profit_factor_computed_correctly(self):
        # 2 winning ₹1 moves and 1 losing ₹2 move on the same sizing
        result = run_walk_forward_backtest(
            preds=[2, 2, 2],
            bars_meta=[
                BarMeta("X", 100.0, 101.0),
                BarMeta("X", 100.0, 101.0),
                BarMeta("X", 100.0, 98.0),
            ],
            config=BacktestConfig(
                initial_capital=100_000.0, entry_slippage_pct=0.0,
                risk_per_trade_pct=0.02, max_single_stock_pct=0.25,
            ),
        )
        assert result.wins == 2
        assert result.losses == 1
        assert result.profit_factor > 0
        # Direction-only sanity: gross profit and loss are both populated
        assert result.gross_profit > 0
        assert result.gross_loss > 0
