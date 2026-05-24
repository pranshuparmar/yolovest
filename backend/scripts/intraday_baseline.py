"""Cheap go/no-go baseline for a real (5-minute) intraday model.

PURPOSE
-------
Before investing in the full intraday rebuild (intraday-specific features,
cost-realistic backtest, meta-labeling — see docs/intraday-model-design.md),
answer ONE question: does an XGBoost model trained on the *existing*
technical features, but on **5-minute bars** with **intraday same-session
labels**, have any **net-of-cost** edge on its natural (argmax) decisions?

If the argmax Sharpe here is clearly negative or ~0, the full project is
likely not worth it on this universe and we stop. If it's promisingly
positive, the fancy intraday features (opening range, session VWAP,
relative-volume-by-time, pivots) become worth building.

This is deliberately CHEAP and APPROXIMATE:
  - reuses the daily technical features on a rolling 5-min window
    (no intraday-specific features, no session resets on VWAP/OBV —
    those raw cumulative features are excluded from the model anyway);
  - a single chronological train/holdout split (not full walk-forward);
  - argmax decisions only (no threshold tuning — we want the honest edge);
  - MIS cost stack + entry slippage; same-session close-out exits;
  - intra-day PnL aggregated to a DAILY equity curve before annualising
    Sharpe (many trades/day would otherwise explode the number).

Treat the individual numbers as rough. The SIGN and rough MAGNITUDE of the
net-of-cost argmax Sharpe is the decision signal.

USAGE (run inside the backend container; reads /app/data/yolovest.db)
---------------------------------------------------------------------
    docker cp backend/scripts/intraday_baseline.py yolovest-backend:/tmp/
    docker exec yolovest-backend python /tmp/intraday_baseline.py \
        --max-symbols 50 --stride 3

Tune --max-symbols / --stride for speed vs coverage. Defaults are sized to
finish in a few minutes on a modest box.
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("intraday_baseline")

# yolovest is importable inside the container (src on PYTHONPATH).
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


# Class label ints, matching the training pipeline: 0=SELL, 1=HOLD, 2=BUY.
_SELL, _HOLD, _BUY = 0, 1, 2


def intraday_path_aware_label(
    *, bars: list[OHLCVBar], start_idx: int, lookahead: int,
    entry: float, target_pct: float, sl_pct: float,
) -> int:
    """Inlined copy of skills.model_retrain.intraday_path_aware_label so
    this script runs against the deployed image (which predates that
    function). Same-session close-out: the forward walk stops at the
    first bar whose date differs from the entry bar's. Returns
    2 BUY / 0 SELL / 1 HOLD."""
    if start_idx + 1 >= len(bars):
        return _HOLD
    session_date = bars[start_idx + 1].timestamp.date()
    buy_target, buy_sl = entry * (1 + target_pct), entry * (1 - sl_pct)
    sell_target, sell_sl = entry * (1 - target_pct), entry * (1 + sl_pct)
    bo = so = None
    bwb = swb = None
    end_idx = min(start_idx + lookahead, len(bars) - 1)
    for k in range(start_idx + 1, end_idx + 1):
        bar = bars[k]
        if bar.timestamp.date() != session_date:
            break
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


def load_bars(
    db_path: str, days: int, min_bars: int, max_symbols: int,
) -> dict[str, list[OHLCVBar]]:
    """Load 5-minute bars grouped by symbol, keeping the most liquid names
    (by bar count) that clear `min_bars`."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = "interval = '5minute'"
    params: list = []
    if days > 0:
        where += " AND timestamp >= datetime('now', ?)"
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
            symbol=r["symbol"], timestamp=dt,
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"],
        ))

    counts = {s: len(b) for s, b in by_symbol.items() if len(b) >= min_bars}
    top = sorted(counts, key=counts.get, reverse=True)[:max_symbols]
    log.info(
        "Loaded %d symbols with >=%d bars; using top %d by depth",
        len(counts), min_bars, len(top),
    )
    return {s: by_symbol[s] for s in top}


def simulate_exit(
    bars: list[OHLCVBar], start_idx: int, direction: int,
    entry: float, target_pct: float, sl_pct: float, lookahead: int,
) -> float:
    """Same-session exit price for a trade entered at bars[start_idx+1].open.

    Walks forward up to `lookahead` bars, stopping at the session boundary.
    Returns the first barrier touched (conservative same-bar tie = SL), or
    the last in-session bar's close if neither is hit (MIS close-out).
    """
    if start_idx + 1 >= len(bars):
        return entry
    session_date = bars[start_idx + 1].timestamp.date()
    if direction == _BUY:
        target = entry * (1 + target_pct)
        sl = entry * (1 - sl_pct)
    else:
        target = entry * (1 - target_pct)
        sl = entry * (1 + sl_pct)

    end_idx = min(start_idx + lookahead, len(bars) - 1)
    last_close = entry
    for k in range(start_idx + 1, end_idx + 1):
        bar = bars[k]
        if bar.timestamp.date() != session_date:
            break
        last_close = bar.close
        hit_target = bar.high >= target if direction == _BUY else bar.low <= target
        hit_sl = bar.low <= sl if direction == _BUY else bar.high >= sl
        if hit_target and hit_sl:
            return sl  # conservative: assume SL fills first
        if hit_sl:
            return sl
        if hit_target:
            return target
    return last_close


def build_samples(
    by_symbol: dict[str, list[OHLCVBar]], *, window: int, stride: int,
    lookahead: int, target_mult: float, sl_mult: float,
) -> tuple[list[dict], list[str]]:
    """Return per-sample dicts (features + label + trade meta) and the
    stable, exclusion-filtered feature-name list."""
    cfg = IndicatorConfig()
    samples: list[dict] = []
    feat_names: list[str] = []
    feat_set: set[str] = set()

    for si, (sym, bars) in enumerate(by_symbol.items(), 1):
        if len(bars) < window + lookahead + 2:
            continue
        for i in range(window, len(bars) - lookahead - 1, stride):
            feats = compute_features(bars[i - window: i + 1], cfg)
            if not feats:
                continue
            atr_pct = feats.get("atr_pct") or 0.0
            next_open = bars[i + 1].open
            if atr_pct <= 0 or next_open <= 0:
                continue
            label = intraday_path_aware_label(
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
        if si % 10 == 0:
            log.info("  features: %d/%d symbols, %d samples",
                     si, len(by_symbol), len(samples))
    return samples, feat_names


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/data/yolovest.db")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--min-bars", type=int, default=10000)
    ap.add_argument("--max-symbols", type=int, default=50)
    ap.add_argument("--stride", type=int, default=3,
                    help="sample every Nth bar (cheapness knob)")
    ap.add_argument("--window", type=int, default=200)
    ap.add_argument("--lookahead", type=int, default=12, help="bars (~60min)")
    ap.add_argument("--target-mult", type=float, default=0.6)
    ap.add_argument("--sl-mult", type=float, default=0.3)
    ap.add_argument("--train-frac", type=float, default=0.8)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    ap.add_argument("--slippage", type=float, default=0.0005)
    args = ap.parse_args()

    by_symbol = load_bars(args.db, args.days, args.min_bars, args.max_symbols)
    if not by_symbol:
        log.error("No symbols with enough 5-min history. Lower --min-bars.")
        return

    # Scale the ATR multipliers to the HOLDING HORIZON, not a single bar.
    # The expected move over H bars is ~ATR(5min)*sqrt(H); a stop sized as
    # a fraction of one bar's ATR sits inside the first bar's noise and is
    # tripped instantly. target_mult/sl_mult are therefore fractions of the
    # horizon's expected move (mirrors how the daily model's 0.6/0.3 are
    # fractions of a daily-ATR move over a ~1-day hold).
    horizon_scale = math.sqrt(args.lookahead)
    eff_target_mult = args.target_mult * horizon_scale
    eff_sl_mult = args.sl_mult * horizon_scale
    log.info(
        "Effective ATR(5min) mults over %d bars: target=%.2f sl=%.2f "
        "(raw %.2f/%.2f x sqrt(%d))",
        args.lookahead, eff_target_mult, eff_sl_mult,
        args.target_mult, args.sl_mult, args.lookahead,
    )

    samples, feat_names = build_samples(
        by_symbol, window=args.window, stride=args.stride,
        lookahead=args.lookahead, target_mult=eff_target_mult,
        sl_mult=eff_sl_mult,
    )
    if len(samples) < 1000:
        log.error("Only %d samples — too few to trust. Lower --stride/--min-bars.",
                  len(samples))
        return

    dist = Counter(s["label"] for s in samples)
    n = len(samples)
    log.info("Samples: %d | label dist BUY=%.1f%% HOLD=%.1f%% SELL=%.1f%%",
             n, 100 * dist[_BUY] / n, 100 * dist[_HOLD] / n, 100 * dist[_SELL] / n)

    # Chronological split by DATE with a one-day purge gap so the train
    # tail's label window can't peek into the holdout.
    dates = sorted({s["date"] for s in samples})
    cut = dates[int(len(dates) * args.train_frac)]
    train = [s for s in samples if s["date"] < cut]
    test = [s for s in samples if s["date"] > cut]  # gap: drop the cut day
    log.info("Split: %d train / %d test (cut at %s)", len(train), len(test), cut)

    def matrix(rows: list[dict]) -> np.ndarray:
        return np.array([[s["feats"].get(f, 0.0) for f in feat_names]
                         for s in rows], dtype=np.float32)

    X_train, y_train = matrix(train), np.array([s["label"] for s in train])
    X_test = matrix(test)

    # Inverse-frequency class weights (mirror the production pipeline).
    tc = Counter(int(v) for v in y_train)
    total, K = sum(tc.values()), len([c for c in tc.values() if c > 0])
    w = {c: (total / (K * cnt)) if cnt else 0.0 for c, cnt in tc.items()}
    sw = np.array([w.get(int(v), 1.0) for v in y_train], dtype=np.float32)

    model = xgb.XGBClassifier(
        objective="multi:softprob", num_class=3, tree_method="hist",
        n_estimators=300, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, n_jobs=-1, eval_metric="mlogloss",
    )
    log.info("Training XGBoost on %d samples x %d features...",
             len(y_train), len(feat_names))
    model.fit(X_train, y_train, sample_weight=sw)

    preds = model.predict(X_test)
    pred_dist = Counter(int(p) for p in preds)
    log.info("Holdout argmax predictions: BUY=%d HOLD=%d SELL=%d",
             pred_dist[_BUY], pred_dist[_HOLD], pred_dist[_SELL])

    # Backtest the argmax decisions with MIS costs + same-session exits.
    pnl_by_date: dict = defaultdict(float)
    wins = trades = 0
    gross_total = net_total = 0.0
    for s, p in zip(test, preds, strict=False):
        direction = int(p)
        if direction == _HOLD:
            continue
        bars = by_symbol[s["sym"]]
        slip = 1 + args.slippage if direction == _BUY else 1 - args.slippage
        entry = s["entry"] * slip
        sl_dist = max(entry * s["atr_pct"] * eff_sl_mult, 0.01)
        qty = int((args.risk_pct * args.capital) / sl_dist)
        # Cap at 1x capital — no leverage. With a tight intraday stop the
        # risk-based size can blow past the account; without this cap a
        # bad gross edge gets amplified into a meaningless Sharpe.
        qty = min(qty, int(args.capital / entry))
        if qty <= 0:
            continue
        exit_px = simulate_exit(
            bars, s["idx"], direction, entry,
            s["atr_pct"] * eff_target_mult, s["atr_pct"] * eff_sl_mult,
            args.lookahead,
        )
        gross = (exit_px - entry) * qty * (1 if direction == _BUY else -1)
        costs = compute_transaction_costs(entry, exit_px, qty, product="MIS")
        net = gross - costs
        pnl_by_date[s["date"]] += net
        gross_total += gross
        net_total += net
        trades += 1
        if net > 0:
            wins += 1

    if trades == 0:
        log.warning("No trades taken on the holdout (model predicted only HOLD).")
        return

    # Daily equity → daily returns → annualised Sharpe.
    series = [pnl_by_date[d] for d in sorted(pnl_by_date)]
    rets = np.array(series, dtype=np.float64) / args.capital
    sharpe = (float(np.mean(rets)) / float(np.std(rets)) * np.sqrt(252)
              if len(rets) > 1 and np.std(rets) > 0 else 0.0)

    print("\n" + "=" * 64)
    print("INTRADAY BASELINE — net-of-cost argmax edge")
    print("=" * 64)
    print(f"symbols={len(by_symbol)}  samples={n}  holdout_trades={trades}")
    print(f"win_rate (net)        : {wins / trades:.1%}")
    print(f"gross PnL             : Rs {gross_total:,.0f}")
    print(f"net PnL (after costs) : Rs {net_total:,.0f}")
    print(f"cost drag             : Rs {gross_total - net_total:,.0f}")
    print(f"trading days          : {len(series)}")
    print(f"ARGMAX SHARPE (daily-aggregated, annualised): {sharpe:.2f}")
    print("=" * 64)
    if sharpe <= 0 or net_total <= 0:
        print("VERDICT: no net-of-cost edge on this cheap baseline. The full "
              "intraday rebuild is likely NOT worth it on this universe — "
              "costs/noise dominate. Reconsider before investing in features.")
    else:
        print("VERDICT: positive net-of-cost edge even on the cheap baseline. "
              "Building the intraday-specific features is justified — they "
              "should only improve on this.")
    print("=" * 64)


if __name__ == "__main__":
    main()
