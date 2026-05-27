"""Tests for the real-PnL walk-forward backtest that replaced the
legacy +1%/-0.5% synthetic payoff."""

import math

import pytest

from yolovest.strategy.walk_forward_backtest import (
    BacktestConfig,
    BarMeta,
    _path_aware_exit,
    _size_position,
    backtest_by_period,
    run_walk_forward_backtest,
    sweep_thresholds,
)


class TestBacktestByPeriod:
    def test_buckets_trades_by_entry_year(self):
        # Two BUY trades in 2024 (winners), one in 2025 (loser).
        metas = [
            BarMeta(symbol="A", entry_close=100.0, exit_close=101.0,
                    buy_exit=101.0, sell_exit=100.0, entry_date="2024-03-01"),
            BarMeta(symbol="B", entry_close=100.0, exit_close=101.0,
                    buy_exit=101.0, sell_exit=100.0, entry_date="2024-06-01"),
            BarMeta(symbol="C", entry_close=100.0, exit_close=99.0,
                    buy_exit=99.0, sell_exit=100.0, entry_date="2025-02-01"),
        ]
        preds = [2, 2, 2]  # all BUY
        out = backtest_by_period(preds, metas, BacktestConfig())
        assert set(out.keys()) == {"2024", "2025"}
        assert out["2024"].total_trades == 2
        assert out["2025"].total_trades == 1
        assert out["2024"].net_pnl > 0   # winners
        assert out["2025"].net_pnl < 0   # loser (incl. costs)

    def test_drops_trades_without_entry_date(self):
        metas = [
            BarMeta(symbol="A", entry_close=100.0, exit_close=101.0,
                    buy_exit=101.0, sell_exit=100.0, entry_date=""),
        ]
        out = backtest_by_period([2], metas, BacktestConfig())
        assert out == {}


class TestPrecomputedExits:
    """The intraday builder stores realized per-direction exit prices
    (buy_exit / sell_exit) instead of raw 1-min path arrays. They must
    take precedence and match what walking the equivalent path yields."""

    def test_precomputed_exits_take_precedence(self):
        meta = BarMeta(
            symbol="X", entry_close=100.0, exit_close=100.0,
            target_pct=0.01, sl_pct=0.005,
            buy_exit=101.0, sell_exit=99.5,
        )
        assert _path_aware_exit(100.0, 1, meta) == 101.0   # BUY → buy_exit
        assert _path_aware_exit(100.0, -1, meta) == 99.5   # SELL → sell_exit

    def test_precomputed_matches_path_walk(self):
        entry = 100.0
        path_highs = [100.4, 101.2, 101.5]
        path_lows = [98.9, 99.5, 99.0]
        path_meta = BarMeta(
            symbol="X", entry_close=entry, exit_close=101.5,
            target_pct=0.01, sl_pct=0.02,
            path_highs=path_highs, path_lows=path_lows,
        )
        buy_via_path = _path_aware_exit(entry, 1, path_meta)
        sell_via_path = _path_aware_exit(entry, -1, path_meta)
        compact = BarMeta(
            symbol="X", entry_close=entry, exit_close=101.5,
            target_pct=0.01, sl_pct=0.02,
            buy_exit=buy_via_path, sell_exit=sell_via_path,
        )
        assert _path_aware_exit(entry, 1, compact) == buy_via_path
        assert _path_aware_exit(entry, -1, compact) == sell_via_path

    def test_hold_days_overrides_path_length_for_reservation(self):
        # Two intraday trades on consecutive days; with hold_days=1 the
        # 1-slot cap frees overnight so the 2nd trade is not blocked.
        cfg = BacktestConfig(max_concurrent_positions=1, initial_capital=100_000.0)
        metas = [
            BarMeta(symbol="A", entry_close=100.0, exit_close=101.0,
                    target_pct=0.01, sl_pct=0.005, buy_exit=101.0,
                    sell_exit=100.0, hold_days=1, entry_date="2026-05-18"),
            BarMeta(symbol="B", entry_close=100.0, exit_close=101.0,
                    target_pct=0.01, sl_pct=0.005, buy_exit=101.0,
                    sell_exit=100.0, hold_days=1, entry_date="2026-05-19"),
        ]
        res = run_walk_forward_backtest([2, 2], metas, cfg)
        assert res.total_trades == 2


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

    def test_min_signal_rate_rejects_ultra_selective_cell(self):
        # 8 ultra-high-conviction winners (P(BUY)=0.90) + 92 moderate
        # mixed-outcome signals (P(BUY)=0.58). A cell at buy_t in
        # (0.58, 0.90] fires on only the 8 winners — 8% signal rate, best
        # Sharpe (pure winners). Without a signal-rate floor the sweep
        # picks it: a cutoff that fires ~never live. With the floor it must
        # pick a reachable cell that fires on the moderate mass (P=0.58),
        # i.e. a threshold <= 0.58. This is the silent-model fix.
        hi = [BarMeta("HI", 100.0, 100.0 + (i % 3 + 2)) for i in range(8)]  # +2..+4%
        mod_win = [BarMeta("MW", 100.0, 102.0) for _ in range(46)]
        mod_lose = [BarMeta("ML", 100.0, 98.0) for _ in range(46)]
        bars = hi + mod_win + mod_lose
        probas = [[0.05, 0.05, 0.90]] * 8 + [[0.05, 0.37, 0.58]] * 92
        cfg = BacktestConfig(initial_capital=100_000.0, entry_slippage_pct=0.0)

        buy_no, _, _ = sweep_thresholds(
            probas, bars, cfg, min_trades=3, min_class_share=0.0,
            min_signal_rate=0.0,
        )
        buy_floor, _, _ = sweep_thresholds(
            probas, bars, cfg, min_trades=3, min_class_share=0.0,
            min_signal_rate=0.10,  # require >=10% of samples to signal
        )
        # No floor → can lock onto the ultra-selective 8% cell (buy_t > 0.58).
        assert buy_no > 0.58
        # With the floor → forced down to a reachable cell firing on the
        # moderate mass (P=0.58), so buy_t <= 0.58.
        assert buy_floor <= 0.58 + 1e-9
        assert buy_floor < buy_no

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
