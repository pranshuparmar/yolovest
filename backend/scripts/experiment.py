"""Offline experiment harness — sweep training configurations on a DB
copy and print the staged-gate comparison table.

PURPOSE
-------
Iterate on the model with evidence instead of weekly live retrains. Each
combination of {window, time-decay, label-mode} runs the REAL training
pipeline (ModelRetrainSkill's matrix builder + XGBoostSignalModel.train,
including purges, embargo, early stopping, holdout threshold tuning and
the long-only swing backtest) and reports the gates a viable model must
clear, in order:

  Stage 1 — information:  oos_auc_buy >= ~0.55 and oos_buy_separation
                          >= ~0.02 (below this, nothing downstream
                          matters — the model can't rank winners).
  Stage 2 — economics:    argmax_sharpe > 0 (edge without threshold
                          cherry-picking) and deflated_sharpe >= 0.95.
  Stage 3 — deployability: tradeable (BUY) signal rate through the full
                          production path on the freshest samples.

USAGE (run on the training box against a COPY of the live DB)
--------------------------------------------------------------
  cd backend && PYTHONPATH=src python scripts/experiment.py \
      --db /path/to/yolovest-copy.db \
      --windows 1100,2000,4015 \
      --decays 1.0,0.4 \
      --label-modes relative,barrier \
      --n-jobs 4

Lanes: --lanes swing (default) and/or intraday. The intraday lane maps
label-mode "barrier" to its "triple_barrier" and is bounded by the
1-min/5-min retention window; expect ~20 min per combo just for the
chunked matrix build. Results print as they finish and are dumped to
experiment_results.json for later comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from yolovest.config import AppConfig, apply_db_config
from yolovest.data.db import Database
from yolovest.skills.model_retrain import ModelRetrainSkill, _time_decay_multipliers
from yolovest.strategy.ml_signal import XGBoostSignalModel


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


async def _deployment_config(db: Database) -> AppConfig:
    """The DEPLOYMENT's config, not code defaults: overlay the snapshot's
    own config table onto a fresh AppConfig via the same apply_db_config
    path the live app uses at startup. Without this, every non-swept knob
    silently reverted to code defaults — an intraday sweep once simulated
    exits at the default 0.6x/0.3x ATR geometry instead of the deployed
    8x/4x, turning every trade into a sub-cost grinder (Sharpe -48) and
    invalidating the economics columns."""
    base = AppConfig(broker={"api_key": "x", "api_secret": "x"})
    try:
        rows = await db.get_all_config()
        return apply_db_config(base, rows)
    except Exception as e:
        print(f"WARNING: could not load the snapshot's config table ({e}); "
              "falling back to code defaults — verify geometry/retention!")
        return base


async def run_combo(
    db: Database,
    *,
    lane: str,
    window: int,
    decay: float,
    label_mode: str,
    n_jobs: int,
    quantile: float,
) -> dict[str, Any]:
    cfg = await _deployment_config(db)
    cfg.retraining.max_training_days = window
    cfg.strategy.time_decay_last_weight = decay
    cfg.strategy.relative_label_quantile = quantile
    cfg.strategy.feedback.enabled = False

    ctx = SimpleNamespace(config=cfg, db=db, ml=None, notify=None)
    skill = ModelRetrainSkill(ctx)

    t0 = time.monotonic()
    training_data = await db.get_training_dataset(max_days=window)
    sector_map: dict[str, str] = {}
    try:
        symbols = sorted({
            r.get("symbol", "") for r in training_data.get("bars", [])
            if r.get("symbol")
        })
        sector_map = await db.get_symbol_sectors_map(symbols)
    except Exception:
        pass

    hp = cfg.strategy.holding_periods
    if lane == "intraday":
        _ret = int(getattr(cfg.database.retention, "intraday_ohlcv_days", 365))
        print(
            f"  [{lane}] effective: geometry {hp.intraday.target}x/"
            f"{hp.intraday.stop_loss}x ATR(5m), intraday retention {_ret}d "
            f"-> window {min(window, _ret)}d"
        )
        # Intraday: "barrier" on the CLI means the lane's triple-barrier.
        intraday_mode = (
            "triple_barrier" if label_mode in ("barrier", "triple_barrier")
            else "relative"
        )
        X, y, names, weights, meta = await skill._build_intraday_matrix(
            training_data,
            horizon_minutes=375,
            target_atr_mult=hp.intraday.target,
            sl_atr_mult=hp.intraday.stop_loss,
            label_mode=intraday_mode,
            sector_map=sector_map,
        )
        lookahead = 1
        product, long_only = "MIS", False
    else:
        X, y, names, weights, meta = skill._prepare_training_data(
            training_data,
            lookahead_bars=10,
            target_atr_mult=hp.short_swing.target,
            sl_atr_mult=hp.short_swing.stop_loss,
            label_mode=label_mode,
            sector_map=sector_map,
        )
        lookahead = 10
        product, long_only = "CNC", True
    n = len(y)
    dist = {c: y.count(lbl) for lbl, c in ((2, "BUY"), (1, "HOLD"), (0, "SELL"))}
    if n < 500:
        return {"error": f"only {n} samples", "n": n}

    # Time-decay + inverse-frequency class weights (mirrors execute()).
    if not weights:
        weights = [1.0] * n
    if decay < 1.0 and n > 1:
        weights = [
            w * m
            for w, m in zip(weights, _time_decay_multipliers(n, decay), strict=False)
        ]
    k = sum(1 for c in dist.values() if c > 0)
    cw = {lbl: (n / (k * dist[c])) if dist[c] else 0.0
          for lbl, c in ((2, "BUY"), (1, "HOLD"), (0, "SELL"))}
    weights = [w * cw.get(int(lbl), 1.0) for w, lbl in zip(weights, y, strict=False)]

    # train() consumes X in place — capture the production-path guard
    # slice FIRST (same lesson the post-train guard learned).
    guard_x = [row[:] for row in X[-1000:]]

    ml = XGBoostSignalModel(model_dir="/tmp/experiment_models", config=cfg)
    metrics = await ml.train(
        lane, X, y,
        {
            "bars_meta": meta,
            "lookahead_bars": lookahead,
            "backtest_product": product,
            "backtest_long_only": long_only,
            "backtest_max_positions": cfg.risk.max_open_positions,
            "sample_weights": weights,
            "n_jobs": n_jobs,
        },
        feature_names=names,
    )

    # Tradeable (BUY) rate through the full production path.
    rate = None
    prod_dist = None
    try:
        labels = ml.predict_labels_batch(guard_x, lane)
        if labels:
            prod_dist = {
                "BUY": sum(1 for p in labels if p == 2),
                "HOLD": sum(1 for p in labels if p == 1),
                "SELL": sum(1 for p in labels if p == 0),
            }
            tradeable = prod_dist["BUY"] + (
                prod_dist["SELL"] if lane == "intraday" else 0
            )
            rate = tradeable / len(labels)
    except Exception:
        pass

    return {
        "n": n,
        "label_dist": dist,
        "auc_buy": metrics.get("oos_auc_buy"),
        "buy_sep": metrics.get("oos_buy_separation"),
        "argmax_sharpe": metrics.get("argmax_sharpe"),
        "tuned_sharpe": metrics.get("tuned_sharpe"),
        "sharpe_lower": metrics.get("sharpe_lower"),
        "deflated": metrics.get("deflated_sharpe"),
        "win_rate": metrics.get("win_rate"),
        "trades": metrics.get("total_trades"),
        "tuned_buy": metrics.get("tuned_buy_threshold"),
        "tradeable_rate": rate,
        "prod_dist": prod_dist,
        "per_year": metrics.get("per_year_oos"),
        "secs": round(time.monotonic() - t0, 1),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Sweep training configs and print the staged-gate table",
    )
    ap.add_argument("--db", required=True, help="Path to a COPY of yolovest.db")
    ap.add_argument("--lanes", default="swing",
                    help="comma list: swing,intraday")
    ap.add_argument("--windows", default="1100,2000,4015")
    ap.add_argument("--decays", default="1.0,0.4")
    ap.add_argument("--label-modes", default="relative,barrier")
    ap.add_argument("--quantile", type=float, default=0.20)
    ap.add_argument("--n-jobs", type=int, default=4,
                    help="XGBoost threads (offline box — parallelism is fine)")
    ap.add_argument("--out", default="experiment_results.json")
    args = ap.parse_args()

    db = Database(args.db)
    await db.initialize()

    combos = [
        (lane, w, d, m)
        for lane in args.lanes.split(",")
        for w in (int(x) for x in args.windows.split(","))
        for d in (float(x) for x in args.decays.split(","))
        for m in args.label_modes.split(",")
    ]
    header = (
        f"{'lane':>8} {'window':>6} {'decay':>5} {'label':>8} | {'n':>8} {'AUCb':>5} "
        f"{'sep':>6} {'argmax':>7} {'tuned':>6} {'lower':>6} {'DSR':>5} "
        f"{'win':>5} {'trades':>6} {'buy_thr':>7} {'rate':>6} {'secs':>6}"
    )
    print(header)
    print("-" * len(header))
    results = []
    for lane, w, d, m in combos:
        try:
            r = await run_combo(
                db, lane=lane, window=w, decay=d, label_mode=m,
                n_jobs=args.n_jobs, quantile=args.quantile,
            )
        except Exception as e:  # keep sweeping; report the failure
            r = {"error": str(e)}
        r.update({"lane": lane, "window": w, "decay": d, "label_mode": m})
        results.append(r)
        if "error" in r:
            print(f"{lane:>8} {w:>6} {d:>5} {m:>8} | ERROR: {r['error']}")
            continue
        print(
            f"{lane:>8} {w:>6} {d:>5} {m:>8} | {r['n']:>8} {_fmt(r['auc_buy']):>5} "
            f"{_fmt(r['buy_sep'], 4):>6} {_fmt(r['argmax_sharpe'], 2):>7} "
            f"{_fmt(r['tuned_sharpe'], 2):>6} {_fmt(r['sharpe_lower'], 2):>6} "
            f"{_fmt(r['deflated'], 2):>5} {_fmt(r['win_rate'], 2):>5} "
            f"{_fmt(r['trades'], 0):>6} {_fmt(r['tuned_buy'], 2):>7} "
            f"{_fmt(r['tradeable_rate'], 3):>6} {_fmt(r['secs'], 0):>6}"
        )

    with open(args.out, "w") as f:
        json.dump(results, f, indent=1, default=str)
    print(f"\nfull results -> {args.out}")
    print(
        "\nGates: viable = AUCb >= 0.55 AND sep >= 0.02 (information), "
        "then argmax > 0 AND DSR >= 0.95 (economics), then rate in a "
        "sane band (deployability). Pick the simplest config that "
        "clears all three; ties go to the shorter window."
    )
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
