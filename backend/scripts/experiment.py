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

Swing lane only (the intraday lane has its own cheap baseline script —
see intraday_baseline.py). Results print as they finish and are dumped
to experiment_results.json for later comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from yolovest.config import AppConfig
from yolovest.data.db import Database
from yolovest.skills.model_retrain import ModelRetrainSkill, _time_decay_multipliers
from yolovest.strategy.ml_signal import XGBoostSignalModel


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


async def run_combo(
    db: Database,
    *,
    window: int,
    decay: float,
    label_mode: str,
    n_jobs: int,
    quantile: float,
) -> dict[str, Any]:
    cfg = AppConfig(broker={"api_key": "x", "api_secret": "x"})
    cfg.retraining.max_training_days = window
    cfg.strategy.time_decay_last_weight = decay
    cfg.strategy.swing_label_mode = label_mode  # type: ignore[assignment]
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
    X, y, names, weights, meta = skill._prepare_training_data(
        training_data,
        lookahead_bars=10,
        target_atr_mult=hp.short_swing.target,
        sl_atr_mult=hp.short_swing.stop_loss,
        label_mode=label_mode,
        sector_map=sector_map,
    )
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
        "swing", X, y,
        {
            "bars_meta": meta,
            "lookahead_bars": 10,
            "backtest_product": "CNC",
            "backtest_long_only": True,
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
        labels = ml.predict_labels_batch(guard_x, "swing")
        if labels:
            prod_dist = {
                "BUY": sum(1 for p in labels if p == 2),
                "HOLD": sum(1 for p in labels if p == 1),
                "SELL": sum(1 for p in labels if p == 0),
            }
            rate = prod_dist["BUY"] / len(labels)
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
        (w, d, m)
        for w in (int(x) for x in args.windows.split(","))
        for d in (float(x) for x in args.decays.split(","))
        for m in args.label_modes.split(",")
    ]
    header = (
        f"{'window':>6} {'decay':>5} {'label':>8} | {'n':>8} {'AUCb':>5} "
        f"{'sep':>6} {'argmax':>7} {'tuned':>6} {'lower':>6} {'DSR':>5} "
        f"{'win':>5} {'trades':>6} {'buy_thr':>7} {'rate':>6} {'secs':>6}"
    )
    print(header)
    print("-" * len(header))
    results = []
    for w, d, m in combos:
        try:
            r = await run_combo(
                db, window=w, decay=d, label_mode=m,
                n_jobs=args.n_jobs, quantile=args.quantile,
            )
        except Exception as e:  # keep sweeping; report the failure
            r = {"error": str(e)}
        r.update({"window": w, "decay": d, "label_mode": m})
        results.append(r)
        if "error" in r:
            print(f"{w:>6} {d:>5} {m:>8} | ERROR: {r['error']}")
            continue
        print(
            f"{w:>6} {d:>5} {m:>8} | {r['n']:>8} {_fmt(r['auc_buy']):>5} "
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
