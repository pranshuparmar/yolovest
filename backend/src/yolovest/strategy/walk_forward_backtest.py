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
import random
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
    # Portfolio cap. When > 0 the simulator only fills a signal if
    # fewer than this many positions are currently "in-flight" (i.e.
    # entered but not yet exited per the lookahead window). Matches
    # the live engine's risk.max_open_positions so the reported
    # Sharpe is bounded by the same parallelism constraint live
    # trading enforces. 0 = legacy behaviour (no cap; every signal
    # is fillable — overstates Sharpe on broad universes with
    # high concurrency).
    max_concurrent_positions: int = 0


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
    # Count of non-HOLD predictions skipped because the portfolio
    # cap was already full. Reported so the user can see how much
    # opportunity the cap closes off (and decide whether
    # max_open_positions is set sensibly).
    signals_skipped_at_cap: int = 0


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

    from datetime import date as _date, timedelta as _td

    def _parse_iso_date(s: str) -> _date | None:
        try:
            # Accept either "YYYY-MM-DD" or full ISO with time prefix.
            return _date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            return None

    capital = cfg.initial_capital
    peak = capital
    max_dd = 0.0
    returns: list[float] = []
    # Portfolio cap: track in-flight exit dates so a signal can only
    # enter when there's a free slot, matching what the live engine's
    # risk.max_open_positions enforces. Stored sorted ascending so
    # the "expire-completed" sweep is O(k) per loop iteration where
    # k is the number of newly-completed positions.
    in_flight_exits: list[_date] = []
    signals_skipped_at_cap = 0
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

        # Portfolio cap enforcement. When the user has set
        # max_concurrent_positions > 0, only fill if there's a free
        # slot. Without entry_date we can't determine when an
        # existing position frees up, so we silently skip the cap
        # (legacy callers without entry_date keep their old behaviour).
        entry_dt = _parse_iso_date(meta.entry_date) if meta.entry_date else None
        if cfg.max_concurrent_positions > 0 and entry_dt is not None:
            # Free positions whose lookahead window has expired by
            # the time this signal arrives.
            in_flight_exits = [d for d in in_flight_exits if d > entry_dt]
            if len(in_flight_exits) >= cfg.max_concurrent_positions:
                signals_skipped_at_cap += 1
                continue

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
        # Reserve a portfolio slot for this position. Approximate exit
        # date = entry_date + (lookahead bars). Bars are daily so we
        # treat each bar as one calendar day. Path-aware exit may
        # fire earlier inside the window; this is a conservative
        # (longer) reservation, which means the cap blocks slightly
        # more signals than strictly necessary — fine for a more
        # honest backtest.
        if cfg.max_concurrent_positions > 0 and entry_dt is not None:
            lookahead = max(1, len(meta.path_highs))
            in_flight_exits.append(entry_dt + _td(days=lookahead))

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
        signals_skipped_at_cap=signals_skipped_at_cap,
    )


_DEFAULT_THRESHOLD_GRID: tuple[float, ...] = (
    0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80,
)


def _sharpe_from_returns(returns: list[float], annualization: int) -> float:
    """Stdlib Sharpe — sample stdev (n-1 denom), zero when degenerate."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    stdev = math.sqrt(var)
    if stdev <= 0:
        return 0.0
    return (mean / stdev) * math.sqrt(annualization)


def _bootstrap_sharpe_lower_bound(
    returns: list[float],
    annualization: int,
    n_iter: int,
    percentile: float,
    seed: int = 42,
) -> float:
    """Resample returns with replacement n_iter times; return the
    requested lower-percentile Sharpe (default 25th).

    This is the "robust Sharpe" used by sweep_thresholds: a threshold
    pair whose point-Sharpe is high purely because the validation
    slice was lucky (one or two outsized winners propping up the mean)
    will see its bootstrapped lower bound collapse, while one with a
    consistent edge across resamples keeps a high lower bound. Picks
    the same family of threshold pairs on average but is far more
    resistant to the small-N curve-fitting failure mode.

    Seeded so the same training data deterministically picks the same
    threshold pair across runs (matches the rest of the ML pipeline's
    random_state=42 convention).
    """
    n = len(returns)
    if n < 2:
        return 0.0
    rng = random.Random(seed)
    sharpes: list[float] = []
    for _ in range(n_iter):
        sample = [returns[rng.randint(0, n - 1)] for _ in range(n)]
        sharpes.append(_sharpe_from_returns(sample, annualization))
    sharpes.sort()
    idx = max(0, min(n_iter - 1, int(n_iter * percentile / 100.0)))
    return sharpes[idx]


def sweep_thresholds(
    probas: list[list[float]],
    bars_meta: list[BarMeta],
    config: BacktestConfig | None = None,
    grid: tuple[float, ...] = _DEFAULT_THRESHOLD_GRID,
    min_trades: int = 100,
    min_class_share: float = 0.10,
    bootstrap_iterations: int = 200,
    bootstrap_percentile: float = 25.0,
) -> tuple[float, float, BacktestResult]:
    """Find the (buy_threshold, sell_threshold) pair with the best
    bootstrapped lower-bound Sharpe on the walk-forward test predictions.

    probas: per-sample class probability vector [P(SELL), P(HOLD), P(BUY)],
      same length as bars_meta. Comes directly from the calibrator's
      predict_proba on the test fold.

    Sweeps a coarse 2D grid (default 9×9 = 81 evals). Each cell derives
    preds via threshold gating:
        BUY  if P(BUY)  >= buy_thresh  and P(BUY)  >= P(SELL)
        SELL if P(SELL) >= sell_thresh and P(SELL) >  P(BUY)
        else HOLD
    runs the existing real-PnL backtest, and ranks cells by the
    `bootstrap_percentile`-th percentile of bootstrapped Sharpe.

    Why bootstrap and not point Sharpe? Sharpe on a 100-500 trade
    validation slice is noisy enough that the cell with the highest
    point-Sharpe often won that race because one or two outsized
    winners happened to land in its trade set. Bootstrapping (200
    resamples by default, 25th percentile lower bound) discounts cells
    whose edge collapses under resampling. Same family of cells wins
    on a robust dataset; lucky cells get filtered out on a noisy one.

    Returns (buy_thresh, sell_thresh, backtest_result) for the best
    cell — tiebreaker is point Sharpe, then win_rate, then total_trades.
    Cells producing fewer than `min_trades` are ignored so an
    over-restrictive threshold pair doesn't win by trivially having
    zero variance.

    `min_class_share` enforces that both BUY and SELL produce at least
    that fraction of total trades in the candidate cell. Without it the
    sweep happily picks asymmetric pairs like (buy=0.80, sell=0.70) that
    fire 528 SELLs and 0 BUYs over 7 days in production — what looks
    great on the holdout's Sharpe collapses to a single-class model
    in live trading. The drift-watch class-collapse alert is the
    downstream symptom this floor prevents. Set to 0.0 to disable.

    Set `bootstrap_iterations=0` to disable bootstrapping and revert
    to point-Sharpe ranking (legacy behaviour).

    Falls back to (0.5, 0.5, run_walk_forward_backtest(argmax)) when no
    grid cell clears the floors (typically a model that just doesn't
    have enough conviction on this corpus).
    """
    cfg = config or BacktestConfig()
    if len(probas) != len(bars_meta):
        raise ValueError(
            f"probas ({len(probas)}) and bars_meta ({len(bars_meta)}) "
            "must be the same length"
        )

    best_buy: float | None = None
    best_sell: float | None = None
    best_result: BacktestResult | None = None
    best_lower_sharpe: float = float("-inf")

    for buy_thresh in grid:
        for sell_thresh in grid:
            preds: list[int] = []
            for p in probas:
                buy_prob = p[_LABEL_BUY] if len(p) > _LABEL_BUY else 0.0
                sell_prob = p[_LABEL_SELL] if len(p) > _LABEL_SELL else 0.0
                if buy_prob >= buy_thresh and buy_prob >= sell_prob:
                    preds.append(_LABEL_BUY)
                elif sell_prob >= sell_thresh and sell_prob > buy_prob:
                    preds.append(_LABEL_SELL)
                else:
                    preds.append(_LABEL_HOLD)
            result = run_walk_forward_backtest(preds, bars_meta, cfg)
            if result.total_trades < min_trades:
                continue
            # Class-collapse floor. Reject cells where one side
            # produces less than `min_class_share` of total trades
            # — those translate to "0 BUY signals in 7 days" in
            # production even when Sharpe looks great on the holdout.
            if min_class_share > 0.0:
                buy_count = sum(1 for p in preds if p == _LABEL_BUY)
                sell_count = sum(1 for p in preds if p == _LABEL_SELL)
                total_nh = buy_count + sell_count
                if total_nh > 0:
                    buy_share = buy_count / total_nh
                    sell_share = sell_count / total_nh
                    if buy_share < min_class_share or sell_share < min_class_share:
                        continue

            # Robust ranking: bootstrap the returns and rank on the
            # lower percentile. Falls through to point Sharpe when
            # bootstrap is disabled.
            if bootstrap_iterations > 0 and result.returns:
                lower_sharpe = _bootstrap_sharpe_lower_bound(
                    result.returns,
                    annualization=cfg.annualization_factor,
                    n_iter=bootstrap_iterations,
                    percentile=bootstrap_percentile,
                )
            else:
                lower_sharpe = result.sharpe

            if best_result is None:
                best_buy, best_sell, best_result = buy_thresh, sell_thresh, result
                best_lower_sharpe = lower_sharpe
                continue
            # Maximise lower-bound Sharpe; tiebreaker on point Sharpe,
            # then win_rate, then trade count.
            if (
                (lower_sharpe, result.sharpe, result.win_rate, result.total_trades)
                > (best_lower_sharpe, best_result.sharpe, best_result.win_rate, best_result.total_trades)
            ):
                best_buy, best_sell, best_result = buy_thresh, sell_thresh, result
                best_lower_sharpe = lower_sharpe

    if best_result is None:
        # No cell met the floors — fall back to argmax baseline.
        baseline_preds = [
            _LABEL_BUY if p[_LABEL_BUY] >= p[_LABEL_SELL] and p[_LABEL_BUY] >= p[_LABEL_HOLD]
            else _LABEL_SELL if p[_LABEL_SELL] >= p[_LABEL_HOLD]
            else _LABEL_HOLD
            for p in probas
        ]
        baseline = run_walk_forward_backtest(baseline_preds, bars_meta, cfg)
        return 0.5, 0.5, baseline

    assert best_buy is not None and best_sell is not None
    logger.info(
        "sweep_thresholds: chose buy=%.2f / sell=%.2f — "
        "point Sharpe=%.3f, bootstrap-lower=%.3f (p%.0f, %d iters), "
        "trades=%d, win_rate=%.2f",
        best_buy, best_sell, best_result.sharpe, best_lower_sharpe,
        bootstrap_percentile, bootstrap_iterations,
        best_result.total_trades, best_result.win_rate,
    )
    return best_buy, best_sell, best_result


def apply_thresholds(
    probas: list[list[float]],
    buy_thresh: float,
    sell_thresh: float,
) -> list[int]:
    """Convert per-sample class-probability vectors to discrete
    BUY/HOLD/SELL predictions using the same gating rule that
    `sweep_thresholds` evaluates.

    Public so callers can replay a chosen (buy, sell) cutoff pair on a
    held-out slice for honest out-of-sample reporting after the sweep
    picked the cutoffs on a separate tuning slice.
    """
    out: list[int] = []
    for p in probas:
        buy_prob = p[_LABEL_BUY] if len(p) > _LABEL_BUY else 0.0
        sell_prob = p[_LABEL_SELL] if len(p) > _LABEL_SELL else 0.0
        if buy_prob >= buy_thresh and buy_prob >= sell_prob:
            out.append(_LABEL_BUY)
        elif sell_prob >= sell_thresh and sell_prob > buy_prob:
            out.append(_LABEL_SELL)
        else:
            out.append(_LABEL_HOLD)
    return out
