"""Swing conviction experiment — can we make the swing model tradeable?

CONTEXT
-------
The swing model has positive-sign edge (argmax Sharpe ~ +0.97) but its
probabilities are compressed: a 3-class softprob over a ~60%-HOLD label
distribution, with inverse-frequency class weights, caps the max class
probability around ~0.50. The tuned thresholds (0.55/0.60) then sit ABOVE
that ceiling, so live signals collapse to HOLD. The edge exists; the model
just never speaks loudly enough to act on.

This harness A/B-tests three OUTPUT FORMULATIONS on the SAME daily features
and swing labels, to answer: which one lifts conviction above a reachable
threshold while keeping (or improving) the net-of-cost edge?

  1. baseline_3class   - current approach: 3-class softprob, inverse-freq
                         class weights (reproduces the compressed conviction).
  2. downsample_hold   - 3-class, but HOLD rows down-sampled in TRAIN to the
                         directional-class count, no inverse-freq weighting.
  3. binary_abstain    - 2-class BUY-vs-SELL trained on directional samples
                         only; the "no-trade" decision becomes an explicit
                         conviction gate (abstain below threshold). Naturally
                         yields higher, more usable probabilities.

For each it reports the holdout max-probability distribution (does conviction
clear a reachable threshold?), the signal rate at a sane gate, and the
net-of-cost (CNC) edge of the signalled trades. If even binary_abstain can't
lift conviction past ~0.55 with positive net edge, the limit is feature
signal, not formulation — an equally important finding.

USAGE (inside the backend container; reads /app/data/yolovest.db)
-----------------------------------------------------------------
    docker cp backend/scripts/swing_conviction_test.py yolovest-backend:/tmp/
    docker exec yolovest-backend python /tmp/swing_conviction_test.py \
        --max-symbols 150

This is a comparison tool: the RELATIVE conviction/edge across formulations
is the signal, not any single absolute number.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("swing_conviction")

try:
    import numpy as np
    import xgboost as xgb

    from yolovest.costs import compute_transaction_costs
    from yolovest.data.features import (
        MODEL_FEATURE_EXCLUSIONS,
        IndicatorConfig,
        compute_features,
    )
    from yolovest.models.schemas import OHLCVBar
except Exception as e:  # pragma: no cover - environment guard
    log.error("Import failed (run inside the backend container): %s", e)
    sys.exit(1)

_SELL, _HOLD, _BUY = 0, 1, 2


def daily_path_aware_label(
    *, bars: list[OHLCVBar], start_idx: int, lookahead: int,
    entry: float, target_pct: float, sl_pct: float,
) -> int:
    """Daily path-aware label (no session boundary — swing holds span days).
    First-winner disambiguation; same-bar cross-direction -> HOLD.
    Returns 2 BUY / 0 SELL / 1 HOLD."""
    buy_target, buy_sl = entry * (1 + target_pct), entry * (1 - sl_pct)
    sell_target, sell_sl = entry * (1 - target_pct), entry * (1 + sl_pct)
    bo = so = None
    bwb = swb = None
    end_idx = min(start_idx + lookahead, len(bars) - 1)
    for k in range(start_idx + 1, end_idx + 1):
        bar = bars[k]
        hi, lo = bar.high, bar.low
        if bo is None:
            tn, sn = hi >= buy_target, lo <= buy_sl
            if tn and sn:
                bo = "ambiguous"
            elif tn:
                bo, bwb = "win", k
            elif sn:
                bo = "loss"
        if so is None:
            tn, sn = lo <= sell_target, hi >= sell_sl
            if tn and sn:
                so = "ambiguous"
            elif tn:
                so, swb = "win", k
            elif sn:
                so = "loss"
        if bo is not None and so is not None:
            break
    bw, sw = bo == "win", so == "win"
    if bw and sw:
        if bwb is not None and swb is not None:
            if bwb < swb:
                return _BUY
            if swb < bwb:
                return _SELL
        return _HOLD
    if bw:
        return _BUY
    if sw:
        return _SELL
    return _HOLD


def simulate_swing_exit(
    bars: list[OHLCVBar], start_idx: int, direction: int,
    entry: float, target_pct: float, sl_pct: float, lookahead: int,
) -> float:
    """First target/SL touch over the next `lookahead` daily bars (no
    session cap — swing carries overnight); else last bar's close.
    Same-bar tie -> SL (conservative)."""
    if direction == _BUY:
        target, sl = entry * (1 + target_pct), entry * (1 - sl_pct)
    else:
        target, sl = entry * (1 - target_pct), entry * (1 + sl_pct)
    end_idx = min(start_idx + lookahead, len(bars) - 1)
    last_close = entry
    for k in range(start_idx + 1, end_idx + 1):
        bar = bars[k]
        last_close = bar.close
        hit_t = bar.high >= target if direction == _BUY else bar.low <= target
        hit_s = bar.low <= sl if direction == _BUY else bar.high >= sl
        if hit_t and hit_s:
            return sl
        if hit_s:
            return sl
        if hit_t:
            return target
    return last_close


def load_daily_bars(
    db_path: str, days: int, min_bars: int, max_symbols: int,
) -> dict[str, list[OHLCVBar]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = "interval = 'daily'"
    params: list = []
    if days > 0:
        where += " AND timestamp >= date('now', ?)"
        params.append(f"-{days} day")
    rows = conn.execute(
        f"SELECT symbol, timestamp, open, high, low, close, volume "
        f"FROM ohlcv WHERE {where} ORDER BY symbol, timestamp",
        params,
    ).fetchall()
    conn.close()
    by_symbol: dict[str, list[OHLCVBar]] = defaultdict(list)
    for r in rows:
        ts = r["timestamp"]
        dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        by_symbol[r["symbol"]].append(OHLCVBar(
            symbol=r["symbol"], timestamp=dt, open=r["open"], high=r["high"],
            low=r["low"], close=r["close"], volume=r["volume"],
        ))
    counts = {s: len(b) for s, b in by_symbol.items() if len(b) >= min_bars}
    top = sorted(counts, key=counts.get, reverse=True)[:max_symbols]
    log.info("Daily: %d symbols >=%d bars; using top %d",
             len(counts), min_bars, len(top))
    return {s: by_symbol[s] for s in top}


def build_samples(
    by_symbol: dict[str, list[OHLCVBar]], *, window: int, lookahead: int,
    target_mult: float, sl_mult: float,
) -> tuple[list[dict], list[str]]:
    cfg = IndicatorConfig()
    samples: list[dict] = []
    feat_names: list[str] = []
    feat_set: set[str] = set()
    for si, (sym, bars) in enumerate(by_symbol.items(), 1):
        if len(bars) < window + lookahead + 2:
            continue
        for i in range(window, len(bars) - lookahead - 1):
            feats = compute_features(bars[i - window: i + 1], cfg)
            if not feats:
                continue
            atr_pct = feats.get("atr_pct") or 0.0
            next_open = bars[i + 1].open
            if atr_pct <= 0 or next_open <= 0:
                continue
            label = daily_path_aware_label(
                bars=bars, start_idx=i, lookahead=lookahead, entry=next_open,
                target_pct=atr_pct * target_mult, sl_pct=atr_pct * sl_mult,
            )
            row = {k: float(v) for k, v in feats.items()
                   if k not in MODEL_FEATURE_EXCLUSIONS}
            for k in row:
                if k not in feat_set:
                    feat_set.add(k)
                    feat_names.append(k)
            samples.append({
                "feats": row, "label": label, "sym": sym, "idx": i,
                "entry": next_open, "atr_pct": atr_pct,
                "date": bars[i + 1].timestamp.date(),
            })
        if si % 25 == 0:
            log.info("  features: %d/%d symbols, %d samples",
                     si, len(by_symbol), len(samples))
    return samples, feat_names


def _matrix(rows: list[dict], feat_names: list[str]) -> np.ndarray:
    return np.array([[s["feats"].get(f, 0.0) for f in feat_names]
                     for s in rows], dtype=np.float32)


def run_formulation(
    name: str, train: list[dict], test: list[dict], feat_names: list[str],
    *, gate: float, lookahead: int, target_mult: float, sl_mult: float,
    by_symbol: dict, capital: float, slippage: float, rng: np.random.Generator,
) -> dict:
    """Train one formulation, return conviction + net-of-cost edge stats."""
    if name == "binary_abstain":
        tr = [s for s in train if s["label"] != _HOLD]
        y_tr = np.array([1 if s["label"] == _BUY else 0 for s in tr])
        params = dict(objective="binary:logistic", eval_metric="logloss")
        num_class = 2
    else:
        tr = list(train)
        if name == "downsample_hold":
            holds = [s for s in tr if s["label"] == _HOLD]
            dirs = [s for s in tr if s["label"] != _HOLD]
            keep_n = min(len(holds), max(1, len(dirs) // 2))
            idx = rng.choice(len(holds), size=keep_n, replace=False)
            tr = dirs + [holds[i] for i in idx]
        y_tr = np.array([s["label"] for s in tr])
        params = dict(objective="multi:softprob", num_class=3,
                      eval_metric="mlogloss")
        num_class = 3

    X_tr = _matrix(tr, feat_names)
    # Inverse-frequency weights only for the baseline (the others rebalance
    # via the data itself / binary split).
    if name == "baseline_3class":
        c = Counter(int(v) for v in y_tr)
        tot, K = sum(c.values()), len([k for k in c.values() if k > 0])
        w = {k: tot / (K * n) if n else 0.0 for k, n in c.items()}
        sw = np.array([w.get(int(v), 1.0) for v in y_tr], dtype=np.float32)
    else:
        sw = None

    model = xgb.XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.05, subsample=0.8,
        colsample_bytree=0.8, n_jobs=-1, tree_method="hist", **params,
    )
    model.fit(X_tr, y_tr, sample_weight=sw)

    X_te = _matrix(test, feat_names)
    proba = model.predict_proba(X_te)

    # Decision per formulation.
    # binary: conviction = max(p, 1-p); dir = BUY if p>=0.5 else SELL.
    # 3-class: conviction = max prob; dir = argmax (HOLD => no trade).
    convictions = []
    signals = []  # (sample, direction) for gated trades
    for s, pr in zip(test, proba, strict=False):
        if num_class == 2:
            p_buy = float(pr[1])
            conv = max(p_buy, 1 - p_buy)
            direction = _BUY if p_buy >= 0.5 else _SELL
        else:
            lab = int(np.argmax(pr))
            conv = float(pr[lab])
            direction = lab
        convictions.append(conv)
        if direction != _HOLD and conv >= gate:
            signals.append((s, direction))

    convictions = np.array(convictions)
    pct_55 = float(np.mean(convictions >= 0.55))
    pct_60 = float(np.mean(convictions >= 0.60))

    # Net-of-cost backtest of gated signals (CNC, 1x cap, per-trade return).
    rets = []
    wins = 0
    gross_sum = net_sum = 0.0
    for s, direction in signals:
        bars = by_symbol[s["sym"]]
        slip = 1 + slippage if direction == _BUY else 1 - slippage
        entry = s["entry"] * slip
        qty = int(capital / entry)  # 1x notional cap
        if qty <= 0:
            continue
        exit_px = simulate_swing_exit(
            bars, s["idx"], direction, entry,
            s["atr_pct"] * target_mult, s["atr_pct"] * sl_mult, lookahead,
        )
        gross = (exit_px - entry) * qty * (1 if direction == _BUY else -1)
        costs = compute_transaction_costs(entry, exit_px, qty, product="CNC")
        net = gross - costs
        gross_sum += gross
        net_sum += net
        rets.append(net / capital)
        if net > 0:
            wins += 1

    n_trades = len(rets)
    arr = np.array(rets) if rets else np.array([0.0])
    # Per-trade Sharpe scaled to a yearly figure by trades/year. Swing holds
    # overlap, so this is a COMPARISON number across formulations, not a
    # deployable Sharpe.
    sharpe = (float(np.mean(arr)) / float(np.std(arr)) * np.sqrt(252 / lookahead)
              if len(arr) > 1 and np.std(arr) > 0 else 0.0)
    return {
        "name": name,
        "train_n": len(tr),
        "pct_conv_55": pct_55,
        "pct_conv_60": pct_60,
        "signal_rate": n_trades / max(1, len(test)),
        "n_trades": n_trades,
        "win_rate": wins / n_trades if n_trades else 0.0,
        "net_per_trade_bps": (net_sum / n_trades / capital * 1e4) if n_trades else 0.0,
        "gross_per_trade_bps": (gross_sum / n_trades / capital * 1e4) if n_trades else 0.0,
        "sharpe_cmp": sharpe,
    }


def run_walk_forward(
    samples: list[dict], feat_names: list[str], *, folds: int, gate: float,
    lookahead: int, target_mult: float, sl_mult: float, by_symbol: dict,
    capital: float, slippage: float, rng: np.random.Generator,
) -> None:
    """Walk-forward validate baseline_3class across `folds` sequential
    expanding-window train->test folds, with a `lookahead`-day purge gap so
    the train tail's label window can't peek into the test fold. Tells us
    whether the single-split edge holds across regimes or was a one-window
    mirage."""
    all_dates = sorted({s["date"] for s in samples})
    nseg = folds + 1
    bounds = [(j * len(all_dates)) // nseg for j in range(nseg + 1)]
    by_date: dict = defaultdict(list)
    for s in samples:
        by_date[s["date"]].append(s)

    rows = []
    for i in range(1, nseg):
        test_dates = all_dates[bounds[i]:bounds[i + 1]]
        if not test_dates:
            continue
        purge_idx = max(0, bounds[i] - lookahead)  # drop last `lookahead` train days
        train_dates = set(all_dates[:purge_idx])
        test_dset = set(test_dates)
        train = [s for s in samples if s["date"] in train_dates]
        test = [s for s in samples if s["date"] in test_dset]
        if len(train) < 2000 or len(test) < 500:
            log.info("Fold %d skipped (train=%d test=%d too small)",
                     i, len(train), len(test))
            continue
        log.info("Fold %d: train=%d test=%d (%s -> %s)",
                 i, len(train), len(test), test_dates[0], test_dates[-1])
        r = run_formulation(
            "baseline_3class", train, test, feat_names, gate=gate,
            lookahead=lookahead, target_mult=target_mult, sl_mult=sl_mult,
            by_symbol=by_symbol, capital=capital, slippage=slippage, rng=rng,
        )
        r["fold"] = i
        r["test_lo"] = str(test_dates[0])
        r["test_hi"] = str(test_dates[-1])
        rows.append(r)

    print("\n" + "=" * 96)
    print(f"SWING WALK-FORWARD (baseline_3class) — {len(rows)} folds, gate={gate:.2f}, "
          f"lookahead={lookahead}d, target/SL={target_mult:.2f}/{sl_mult:.2f}")
    print("=" * 96)
    print(f"{'fold':<5}{'test window':<26}{'%conv>=.55':>11}{'trades':>8}"
          f"{'win%':>7}{'gross_bps':>11}{'net_bps':>9}{'sharpe*':>9}")
    print("-" * 96)
    for r in rows:
        print(f"{r['fold']:<5}{r['test_lo'] + '..' + r['test_hi']:<26}"
              f"{r['pct_conv_55']:>10.1%}{r['n_trades']:>8d}{r['win_rate']:>6.1%}"
              f"{r['gross_per_trade_bps']:>11.1f}{r['net_per_trade_bps']:>9.1f}"
              f"{r['sharpe_cmp']:>9.2f}")
    print("-" * 96)
    if rows:
        nets = [r["net_per_trade_bps"] for r in rows]
        wins = [r["win_rate"] for r in rows]
        pos = sum(1 for x in nets if x > 0)
        print(f"AGG: {pos}/{len(rows)} folds net-positive | "
              f"mean net_bps={float(np.mean(nets)):.1f} | "
              f"mean win%={float(np.mean(wins)):.1%} | "
              f"min net_bps={min(nets):.1f}")
    print("=" * 96)
    print("Reading: edge is real & deployable only if MOST folds are net-positive "
          "with consistent win% — not one fold carrying the average. A single\n"
          "strong fold + several flat/negative folds means the single-split "
          "number was a regime mirage.")
    print("=" * 96)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/data/yolovest.db")
    ap.add_argument("--days", type=int, default=1500)
    ap.add_argument("--min-bars", type=int, default=400)
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--window", type=int, default=200)
    ap.add_argument("--lookahead", type=int, default=10, help="daily bars (swing)")
    ap.add_argument("--target-mult", type=float, default=1.5)
    ap.add_argument("--sl-mult", type=float, default=0.75)
    ap.add_argument("--train-frac", type=float, default=0.8)
    ap.add_argument("--gate", type=float, default=0.55,
                    help="conviction threshold for a tradeable signal")
    ap.add_argument("--folds", type=int, default=1,
                    help="if >1, walk-forward validate baseline_3class across N "
                         "sequential expanding-window folds instead of the "
                         "single-split 3-formulation comparison")
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--slippage", type=float, default=0.0005)
    args = ap.parse_args()

    by_symbol = load_daily_bars(args.db, args.days, args.min_bars, args.max_symbols)
    if not by_symbol:
        log.error("No symbols with enough daily history. Lower --min-bars.")
        return

    samples, feat_names = build_samples(
        by_symbol, window=args.window, lookahead=args.lookahead,
        target_mult=args.target_mult, sl_mult=args.sl_mult,
    )
    if len(samples) < 2000:
        log.error("Only %d samples — too few. Lower --min-bars/--max-symbols.",
                  len(samples))
        return
    dist = Counter(s["label"] for s in samples)
    n = len(samples)
    log.info("Samples: %d | labels BUY=%.1f%% HOLD=%.1f%% SELL=%.1f%% | %d features",
             n, 100 * dist[_BUY] / n, 100 * dist[_HOLD] / n,
             100 * dist[_SELL] / n, len(feat_names))

    rng = np.random.default_rng(42)

    if args.folds > 1:
        run_walk_forward(
            samples, feat_names, folds=args.folds, gate=args.gate,
            lookahead=args.lookahead, target_mult=args.target_mult,
            sl_mult=args.sl_mult, by_symbol=by_symbol, capital=args.capital,
            slippage=args.slippage, rng=rng,
        )
        return

    dates = sorted({s["date"] for s in samples})
    cut = dates[int(len(dates) * args.train_frac)]
    train = [s for s in samples if s["date"] < cut]
    test = [s for s in samples if s["date"] > cut]
    log.info("Split: %d train / %d test (cut %s, gate=%.2f)",
             len(train), len(test), cut, args.gate)

    results = []
    for name in ("baseline_3class", "downsample_hold", "binary_abstain"):
        log.info("Training formulation: %s ...", name)
        results.append(run_formulation(
            name, train, test, feat_names, gate=args.gate,
            lookahead=args.lookahead, target_mult=args.target_mult,
            sl_mult=args.sl_mult, by_symbol=by_symbol, capital=args.capital,
            slippage=args.slippage, rng=rng,
        ))

    print("\n" + "=" * 92)
    print(f"SWING CONVICTION A/B — gate={args.gate:.2f}, "
          f"lookahead={args.lookahead}d, "
          f"target/SL={args.target_mult:.2f}/{args.sl_mult:.2f} ATR")
    print("=" * 92)
    hdr = (f"{'formulation':<18}{'%conv>=.55':>11}{'%conv>=.60':>11}"
           f"{'sig_rate':>10}{'trades':>8}{'win%':>7}"
           f"{'gross_bps':>11}{'net_bps':>9}{'sharpe*':>9}")
    print(hdr)
    print("-" * 92)
    for r in results:
        print(f"{r['name']:<18}{r['pct_conv_55']:>10.1%}{r['pct_conv_60']:>11.1%}"
              f"{r['signal_rate']:>10.2%}{r['n_trades']:>8d}{r['win_rate']:>6.1%}"
              f"{r['gross_per_trade_bps']:>11.1f}{r['net_per_trade_bps']:>9.1f}"
              f"{r['sharpe_cmp']:>9.2f}")
    print("=" * 92)
    print("Reading: a tradeable formulation needs %conv>=.55 well above ~0 (so "
          "signals clear a reachable threshold) AND net_bps > 0 (net-of-cost\n"
          "edge survives). sharpe* is a cross-formulation comparison number, "
          "not a deployable Sharpe. If even binary_abstain can't lift\n"
          "conviction with positive net_bps, the limit is feature signal, not "
          "formulation.")
    print("=" * 92)


if __name__ == "__main__":
    main()
