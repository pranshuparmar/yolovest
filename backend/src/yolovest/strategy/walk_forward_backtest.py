"""Walk-forward backtest with real-PnL geometry.

Replaces the long-standing synthetic `+1% correct / -0.5% wrong` payoff
in `ml_signal.train`. That fiction inflated Sharpe / max-drawdown /
win-rate to numbers that didn't translate to live trading PnL.

This module does the smallest honest thing: for each non-HOLD
prediction, simulate a one-bar trade through the same cost model the
live trading path uses (`compute_transaction_costs`), size by
`risk_per_trade_pct × capital`, apply entry slippage, and compound
PnL into a real equity curve. Sharpe / drawdown / win-rate fall out
of that PnL series directly.

What this is NOT (deliberately):
  - A full portfolio simulator with concurrent open positions, sector
    caps, max-open-positions, daily/weekly circuit breakers, etc.
    Those gates are pre-trade filters in the live path; here we treat
    every fold-test sample independently. Adding them is a multi-day
    refactor with a much smaller honest-metric payoff than this step.

Path-aware exits: when BarMeta carries `path_highs` / `path_lows` /
`target_pct` / `sl_pct`, the simulator walks the future window bar by
bar and exits at the first SL or target hit using the same geometry
the label uses. Without those fields, it falls back to close-to-close
exit at `exit_close` for backwards-compat with older tests.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from yolovest.costs import compute_transaction_costs

if TYPE_CHECKING:
    from yolovest.config import TransactionCostConfig

logger = logging.getLogger(__name__)


# Label values used by the XGBoost classifier
_LABEL_SELL = 0
_LABEL_HOLD = 1
_LABEL_BUY = 2


@dataclass
class BarMeta:
    """Per-sample metadata the backtest needs to simulate a trade.

    Parallel to X / y rows emitted by `_prepare_training_data` — entry
    is the close at sample index `i`, exit_close is the close at
    `i + lookahead_bars` (the label's own forward window).

    When path_highs / path_lows / target_pct / sl_pct are supplied,
    the simulator walks the future window bar-by-bar and exits at the
    first SL or target hit (path-aware), matching the geometry the
    path-aware label uses. Without them, the simulator falls back to
    close-to-close exit at exit_close.

    entry_date (YYYY-MM-DD) enables daily-aggregated Sharpe — without
    it, per-trade Sharpe massively over-inflates on high-frequency
    strategies because each trade is annualised as if it were a
    full day's return.
    """
    symbol: str
    entry_close: float
    exit_close: float
    path_highs: list[float] = field(default_factory=list)
    path_lows: list[float] = field(default_factory=list)
    target_pct: float = 0.0
    sl_pct: float = 0.0
    entry_date: str = ""


@dataclass
class BacktestConfig:
    """Knobs the simulator honours.

    Defaults match a conservative retail equity setup (₹1L capital,
    2% risk per trade, 25% max single-stock, 0.05% per-side slippage,
    MIS product for cost calc).
    """
    initial_capital: float = 100_000.0
    risk_per_trade_pct: float = 0.02
    max_single_stock_pct: float = 0.25
    entry_slippage_pct: float = 0.0005  # each side
    product: str = "MIS"
    annualization_factor: int = 252
    cost_config: "TransactionCostConfig | None" = None
    # Implicit per-trade stop loss used for position sizing only. We
    # don't actually exit at SL in v1 — exit is always the lookahead-
    # bar close. This is the sizing denominator (risk amount / risk per
    # share). A 1% SL is a sane default that matches the live strategy's
    # ATR-multiplier sizing on most stocks.
    sizing_sl_pct: float = 0.01


@dataclass
class BacktestResult:
    sharpe: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    total_trades: int
    wins: int
    losses: int
    gross_profit: float
    gross_loss: float
    net_pnl: float
    final_capital: float
    returns: list[float] = field(default_factory=list)


def _path_aware_exit(
    entry: float,
    direction: int,
    meta: BarMeta,
) -> float:
    """Walk the future window bar by bar; return the price the trade
    actually exited at. Conservative ordering when both target and SL
    are touched in the same bar: assume SL fires first (the metric
    should be hard to game, not optimistic).

    Returns `meta.exit_close` when path data is missing or neither
    barrier is touched.
    """
    if (
        not meta.path_highs
        or not meta.path_lows
        or meta.target_pct <= 0
        or meta.sl_pct <= 0
        or len(meta.path_highs) != len(meta.path_lows)
    ):
        return meta.exit_close

    if direction > 0:  # BUY
        target = entry * (1 + meta.target_pct)
        sl = entry * (1 - meta.sl_pct)
        for hi, lo in zip(meta.path_highs, meta.path_lows, strict=False):
            hit_target = hi >= target
            hit_sl = lo <= sl
            if hit_target and hit_sl:
                return sl
            if hit_target:
                return target
            if hit_sl:
                return sl
    else:  # SELL
        target = entry * (1 - meta.target_pct)
        sl = entry * (1 + meta.sl_pct)
        for hi, lo in zip(meta.path_highs, meta.path_lows, strict=False):
            hit_target = lo <= target
            hit_sl = hi >= sl
            if hit_target and hit_sl:
                return sl
            if hit_target:
                return target
            if hit_sl:
                return sl

    return meta.exit_close


def _size_position(
    entry: float, capital: float, cfg: BacktestConfig,
) -> int:
    """Position size = min(risk-based, single-stock-cap-based, ≥1)."""
    if entry <= 0 or capital <= 0:
        return 0
    risk_per_share = max(entry * cfg.sizing_sl_pct, 0.01)
    by_risk = int((cfg.risk_per_trade_pct * capital) / risk_per_share)
    by_cap = int((cfg.max_single_stock_pct * capital) / entry)
    return max(0, min(by_risk, by_cap))


def run_walk_forward_backtest(
    preds: list[int],
    bars_meta: list[BarMeta],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Walk through predictions chronologically and build a real PnL curve.

    Each non-HOLD prediction is one trade:
        entry  = bars_meta[i].entry_close × (1 ± entry_slippage_pct)
        exit   = bars_meta[i].exit_close
        size   = `_size_position(entry, capital, config)`
        gross  = (exit − entry) × size × direction
        costs  = compute_transaction_costs(entry, exit, size, product)
        net    = gross − costs
        capital += net

    Returns sharpe / DD / win-rate / profit-factor computed off the
    realised per-trade return series.
    """
    cfg = config or BacktestConfig()
    if len(preds) != len(bars_meta):
        raise ValueError(
            f"preds ({len(preds)}) and bars_meta ({len(bars_meta)}) "
            "must be the same length"
        )

    capital = cfg.initial_capital
    peak = capital
    max_dd = 0.0
    returns: list[float] = []
    # Daily aggregation for honest Sharpe. Per-trade Sharpe with
    # annualization_factor=252 inflates massively on high-frequency
    # strategies (5 intraday trades/day × 252 days → annualization
    # factor should be sqrt(5×252) not sqrt(252), but the standard
    # quant convention is to compute returns on a DAILY equity curve
    # and annualise by sqrt(252)). When entry_date is available on
    # the bars_meta we use that.
    daily_pnl: dict[str, float] = {}
    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    net_pnl_total = 0.0

    for pred, meta in zip(preds, bars_meta, strict=False):
        if pred == _LABEL_HOLD:
            continue
        if meta.entry_close <= 0 or meta.exit_close <= 0:
            continue
        direction = 1 if pred == _LABEL_BUY else -1

        if direction > 0:
            entry = meta.entry_close * (1 + cfg.entry_slippage_pct)
        else:
            entry = meta.entry_close * (1 - cfg.entry_slippage_pct)
        exit_price = _path_aware_exit(entry, direction, meta)

        # Size on fixed initial_capital, not on compounded capital.
        # Otherwise a high-win-rate simulated equity curve doubles
        # over and over until it overflows float64 and the next
        # int(capital * ratio) call raises
        # "cannot convert float infinity to integer". This also
        # matches how a real account is sized — risk-per-trade is a
        # fraction of a stable base, not of the running balance.
        size = _size_position(entry, cfg.initial_capital, cfg)
        if size <= 0:
            continue

        gross = (exit_price - entry) * size * direction
        costs = compute_transaction_costs(
            entry, exit_price, size,
            product=cfg.product, cost_config=cfg.cost_config,
        )
        net = gross - costs

        # Pre-trade position value as the denominator so per-trade
        # returns are comparable across capital levels.
        position_value = entry * size
        if position_value <= 0:
            continue
        ret = net / position_value
        # Skip non-finite returns / nets defensively. A single bad
        # bar can otherwise propagate inf/nan into sharpe and the
        # downstream metrics dict.
        if not (math.isfinite(ret) and math.isfinite(net)):
            continue
        returns.append(ret)

        capital += net
        net_pnl_total += net
        if not math.isfinite(capital):
            # Should never happen with fixed-base sizing above, but
            # guard so a runaway equity curve can't crash the loop.
            capital = peak
            break
        peak = max(peak, capital)
        # Drawdown normalised by INITIAL capital, not peak. With a
        # high-win-rate model + fixed-base sizing the peak inflates
        # over thousands of trades, making per-loss drawdowns
        # microscopic as a fraction of peak. Normalising by initial
        # capital reports the actual % of starting capital at risk
        # in the worst drawdown — the number you'd feel in live
        # trading even after months of cumulative gains.
        dd = (peak - capital) / cfg.initial_capital
        if dd > max_dd:
            max_dd = dd

        if meta.entry_date:
            daily_pnl[meta.entry_date] = daily_pnl.get(meta.entry_date, 0.0) + net

        if net > 0:
            wins += 1
            gross_profit += net
        elif net < 0:
            losses += 1
            gross_loss += -net

    total = wins + losses
    win_rate = wins / total if total else 0.0
    profit_factor = (
        gross_profit / gross_loss if gross_loss > 0
        else (math.inf if gross_profit > 0 else 0.0)
    )

    # Sharpe: prefer the daily-aggregated equity-curve series (standard
    # quant convention; annualises cleanly via sqrt(252)). Fall back to
    # per-trade returns × sqrt(annualization_factor) when entry_date
    # isn't available on the metadata (older callers / unit tests).
    if len(daily_pnl) > 1:
        daily_rets = [n / cfg.initial_capital for n in daily_pnl.values()]
        mean = sum(daily_rets) / len(daily_rets)
        var = sum((r - mean) ** 2 for r in daily_rets) / (len(daily_rets) - 1)
        stdev = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / stdev) * math.sqrt(252) if stdev > 0 else 0.0
    elif len(returns) > 1:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        stdev = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / stdev) * math.sqrt(cfg.annualization_factor) if stdev > 0 else 0.0
    else:
        sharpe = 0.0

    return BacktestResult(
        sharpe=round(sharpe, 4),
        max_drawdown_pct=round(max_dd, 4),
        win_rate=round(win_rate, 4),
        profit_factor=(
            round(profit_factor, 4) if profit_factor != math.inf else math.inf
        ),
        total_trades=total,
        wins=wins,
        losses=losses,
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        net_pnl=round(net_pnl_total, 2),
        final_capital=round(capital, 2),
        returns=returns,
    )
