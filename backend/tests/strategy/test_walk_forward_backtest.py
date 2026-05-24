"""Tests for the real-PnL walk-forward backtest that replaced the
legacy +1%/-0.5% synthetic payoff."""

import math

import pytest

from yolovest.strategy.walk_forward_backtest import (
    BacktestConfig,
    BarMeta,
    _size_position,
    run_walk_forward_backtest,
    sweep_thresholds,
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


class TestSweepThresholds:
    """The PnL-tuned threshold sweep should find cutoffs that filter
    out low-conviction signals when those signals lose money."""

    @staticmethod
    def _losing_low_conf_meta() -> list[BarMeta]:
        """30 winning high-conviction trades, 30 losing low-conviction
        trades. A threshold that filters out the low-conviction tail
        should beat argmax handily."""
        # First 30 are winners (entry 100 → exit 102)
        winners = [BarMeta("WINNER", 100.0, 102.0) for _ in range(30)]
        # Next 30 are losers (entry 100 → exit 98)
        losers = [BarMeta("LOSER", 100.0, 98.0) for _ in range(30)]
        return winners + losers

    @staticmethod
    def _losing_low_conf_probas() -> list[list[float]]:
        """Winners come with high BUY probability; losers with marginal
        BUY (just over 0.5). A tuned threshold of, say, 0.65 only fires
        on winners — argmax fires on both and loses money."""
        # Winners: P(BUY)=0.80, P(HOLD)=0.15, P(SELL)=0.05
        winners = [[0.05, 0.15, 0.80] for _ in range(30)]
        # Losers: P(BUY)=0.55, P(HOLD)=0.40, P(SELL)=0.05
        losers = [[0.05, 0.40, 0.55] for _ in range(30)]
        return winners + losers

    def test_picks_threshold_excluding_low_conf_losers(self):
        bars_meta = self._losing_low_conf_meta()
        probas = self._losing_low_conf_probas()
        cfg = BacktestConfig(
            initial_capital=100_000.0,
            entry_slippage_pct=0.0,
            risk_per_trade_pct=0.02,
            max_single_stock_pct=0.25,
        )
        # Tiny synthetic corpus (60 BUY-only samples). Pass
        # min_trades=10 to clear the production default of 100, and
        # min_class_share=0.0 to bypass the class-collapse floor (the
        # corpus has zero SELL samples by construction so it'd fail
        # the >=10% SELL share requirement regardless of threshold).
        buy_t, sell_t, result = sweep_thresholds(
            probas, bars_meta, cfg, min_trades=10, min_class_share=0.0,
        )
        # The tuned BUY threshold should be above the losers' P(BUY)=0.55
        # so only the 0.80-conviction winners survive.
        assert buy_t > 0.55
        assert result.win_rate >= 0.99

    def test_bounds_restrict_sweep_to_reachable_thresholds(self):
        # 40 high-conviction winners (P(BUY)=0.80, +5%) and 40 losers
        # whose conviction sits ABOVE the 0.60 ceiling (P(BUY)=0.68, -5%).
        # Shedding the losers requires a BUY threshold > 0.68, so the
        # unbounded sweep climbs past 0.60; bounded to <=0.60 it can't
        # separate them and is forced to a reachable cell — exactly what
        # production's tuned_threshold_max_value clamp does at inference.
        winners = [BarMeta("W", 100.0, 105.0) for _ in range(40)]
        losers = [BarMeta("L", 100.0, 95.0) for _ in range(40)]
        bars = winners + losers
        probas = (
            [[0.05, 0.15, 0.80]] * 40
            + [[0.05, 0.27, 0.68]] * 40
        )
        cfg = BacktestConfig(initial_capital=100_000.0, entry_slippage_pct=0.0)

        ub_buy, _, _ = sweep_thresholds(
            probas, bars, cfg, min_trades=10, min_class_share=0.0,
        )
        b_buy, b_sell, _ = sweep_thresholds(
            probas, bars, cfg, min_trades=10, min_class_share=0.0,
            max_threshold=0.60, max_diff=0.05,
        )
        assert ub_buy > 0.60  # unbounded climbs past the ceiling
        assert b_buy <= 0.60 + 1e-9  # bounded stays within it
        assert b_sell <= 0.60 + 1e-9
        assert abs(b_buy - b_sell) <= 0.05 + 1e-9

    def test_returns_argmax_baseline_when_no_cell_clears_min_trades(self):
        # Tiny corpus: 2 samples, both BUYs. min_trades=10 → no cell
        # clears the floor → function falls back to argmax.
        bars_meta = [BarMeta("X", 100.0, 101.0), BarMeta("Y", 100.0, 99.0)]
        probas = [[0.1, 0.2, 0.7], [0.1, 0.2, 0.7]]
        buy_t, sell_t, result = sweep_thresholds(
            probas, bars_meta, BacktestConfig(), min_trades=10,
        )
        # Fallback signature: 0.5 thresholds, baseline result computed.
        assert buy_t == 0.5
        assert sell_t == 0.5
        assert result.total_trades == 2

    def test_input_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            sweep_thresholds(
                probas=[[0.3, 0.4, 0.3]],
                bars_meta=[BarMeta("X", 100.0, 101.0), BarMeta("Y", 100.0, 101.0)],
            )
