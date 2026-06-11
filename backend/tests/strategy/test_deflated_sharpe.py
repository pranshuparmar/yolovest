"""Deflated Sharpe Ratio: the selection-bias correction the per-cell
bootstrap lower bound can't provide. Verifies the standalone estimator and
that sweep_thresholds attaches it to the chosen cell.
"""

import random
from datetime import date, timedelta

from yolovest.strategy.walk_forward_backtest import (
    BacktestConfig,
    BarMeta,
    deflated_sharpe_ratio,
    sweep_thresholds,
)


class TestDeflatedSharpeFn:
    def test_none_on_insufficient_inputs(self):
        assert deflated_sharpe_ratio([0.1] * 10, [1.0]) is None  # <2 trials
        assert deflated_sharpe_ratio([0.1, 0.2], [1.0, 1.1]) is None  # <4 obs

    def test_in_unit_interval(self):
        random.seed(3)
        rets = [random.gauss(0.0006, 0.012) for _ in range(250)]
        dsr = deflated_sharpe_ratio(rets, [0.30, 0.40, 0.35], annualization=252)
        assert dsr is not None and 0.0 <= dsr <= 1.0

    def test_more_aggressive_search_lowers_dsr(self):
        # Same (noisy) return series; a wider / larger trial set raises the
        # expected-max-under-null benchmark, so the same edge clears a
        # lower deflated Sharpe.
        random.seed(3)
        rets = [random.gauss(0.0006, 0.012) for _ in range(250)]
        lenient = deflated_sharpe_ratio(rets, [0.30, 0.35], annualization=252)
        aggressive = deflated_sharpe_ratio(
            rets, [0.30 + 0.05 * i for i in range(60)], annualization=252,
        )
        assert lenient is not None and aggressive is not None
        assert aggressive < lenient

    def test_stronger_edge_raises_dsr(self):
        random.seed(4)
        weak = [random.gauss(0.0002, 0.012) for _ in range(250)]
        random.seed(4)
        strong = [random.gauss(0.0015, 0.012) for _ in range(250)]
        trials = [0.40, 0.50, 0.30]
        assert (
            deflated_sharpe_ratio(strong, trials, annualization=252)
            > deflated_sharpe_ratio(weak, trials, annualization=252)
        )


class TestSweepAttachesDSR:
    def test_sweep_sets_deflated_sharpe(self):
        base = date(2022, 1, 1)
        bars: list[BarMeta] = []
        probas: list[list[float]] = []
        for i in range(60):  # BUY winners (+5%)
            bars.append(BarMeta(
                "W", 100.0, 105.0,
                entry_date=(base + timedelta(days=i)).isoformat(),
            ))
            probas.append([0.05, 0.15, 0.80])
        for i in range(60):  # SELL winners (-5%)
            bars.append(BarMeta(
                "L", 100.0, 95.0,
                entry_date=(base + timedelta(days=60 + i)).isoformat(),
            ))
            probas.append([0.80, 0.15, 0.05])
        cfg = BacktestConfig(initial_capital=100_000.0, entry_slippage_pct=0.0)

        _, _, result = sweep_thresholds(
            probas, bars, cfg, min_trades=20, min_class_share=0.10,
        )
        assert result.deflated_sharpe is not None
        assert 0.0 <= result.deflated_sharpe <= 1.0
