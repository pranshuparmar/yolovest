"""Cross-sectional relative-momentum labels + long-only evaluation.

The swing model's question changed from "will this stock hit +2R before
-1R?" (absolute — dominated by market drift) to "will this stock be a
top-quantile relative performer among the universe?" — and its backtest
evaluates long-only, because the live book cannot act on swing SELLs.
"""

from yolovest.skills.model_retrain import _assign_relative_labels
from yolovest.strategy.walk_forward_backtest import (
    BacktestConfig,
    BarMeta,
    run_walk_forward_backtest,
    sweep_thresholds,
)


class TestAssignRelativeLabels:
    def _panel(self, n_symbols: int = 12, n_dates: int = 3):
        """fwd_returns/dates for `n_symbols` per date; symbol 0 always
        +5% (clear winner), last symbol always -5% (clear loser), the
        rest spread evenly in between."""
        fwd, dates = [], []
        for d in range(n_dates):
            for k in range(n_symbols):
                if k == 0:
                    r = 0.05
                elif k == n_symbols - 1:
                    r = -0.05
                else:
                    r = 0.01 - 0.002 * k
                fwd.append(r)
                dates.append(f"2026-01-{d + 1:02d}")
        return fwd, dates

    def test_top_and_bottom_quantiles_labelled(self):
        fwd, dates = self._panel()
        labels = _assign_relative_labels(fwd, dates, quantile=0.20)
        n = 12
        for d in range(3):
            day = labels[d * n:(d + 1) * n]
            assert day[0] == 2, "clear winner must be BUY"
            assert day[-1] == 0, "clear loser must be SELL"
            assert day.count(2) == 2  # int(12 * 0.2) = 2 per side
            assert day.count(0) == 2
            assert day.count(1) == 8

    def test_thin_dates_are_all_hold(self):
        # 5 names < the 10-name minimum → no meaningful cross-section.
        fwd, dates = self._panel(n_symbols=5, n_dates=2)
        labels = _assign_relative_labels(fwd, dates, quantile=0.20)
        assert labels == [1] * 10

    def test_invalid_returns_are_hold_and_excluded_from_ranking(self):
        fwd, dates = self._panel(n_symbols=12, n_dates=1)
        fwd[3] = None  # no valid entry price for this sample
        labels = _assign_relative_labels(fwd, dates, quantile=0.20)
        assert labels[3] == 1
        assert labels[0] == 2 and labels[11] == 0

    def test_labels_are_market_neutral(self):
        # Shift EVERY return up 10% (a roaring bull day) — the relative
        # label must not change: same winners, same losers. This is the
        # property the absolute barrier label lacked.
        fwd, dates = self._panel(n_dates=1)
        base = _assign_relative_labels(fwd, dates, quantile=0.20)
        shifted = _assign_relative_labels(
            [r + 0.10 for r in fwd], dates, quantile=0.20,
        )
        assert base == shifted


def _meta(entry: float = 100.0, up: bool = True, day: str = "2026-01-05") -> BarMeta:
    return BarMeta(
        symbol="S",
        entry_close=entry,
        exit_close=entry * (1.05 if up else 0.95),
        entry_date=day,
    )


class TestLongOnlyBacktest:
    def test_sell_predictions_do_not_trade(self):
        # 1 BUY winner + 3 SELL "winners": long-only books only the BUY.
        preds = [2, 0, 0, 0]
        metas = [
            _meta(up=True, day="2026-01-05"),
            _meta(up=False, day="2026-01-06"),
            _meta(up=False, day="2026-01-07"),
            _meta(up=False, day="2026-01-08"),
        ]
        res = run_walk_forward_backtest(
            preds, metas, BacktestConfig(long_only=True),
        )
        assert res.total_trades == 1
        assert res.wins == 1

        both = run_walk_forward_backtest(preds, metas, BacktestConfig())
        assert both.total_trades == 4

    def test_long_only_sweep_picks_a_buy_cell(self):
        # 300 samples: P(BUY) is informative (0.7 on winners, 0.2 on
        # losers); SELL probabilities are junk. The long-only sweep must
        # find a viable BUY threshold despite zero SELL trades (the
        # class-share floor would otherwise reject every cell).
        probas, metas = [], []
        for i in range(300):
            win = i % 2 == 0
            probas.append([0.15, 0.15, 0.70] if win else [0.30, 0.50, 0.20])
            metas.append(_meta(up=win, day=f"2026-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}"))
        buy, _sell, result = sweep_thresholds(
            probas, metas, BacktestConfig(long_only=True), min_trades=50,
        )
        assert result.total_trades >= 50
        assert result.losses == 0  # only the 0.70-P(BUY) winners trade
        assert buy > 0.2
