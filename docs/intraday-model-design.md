# Designing a Proper Intraday Model

> Status: **shelved (2026-05-24)** after a cheap baseline showed no
> net-of-cost edge — see "Baseline result" below. Originally captured why
> the current "intraday" model is really a next-day daily predictor and
> what a genuine intraday model would require. Kept for the record; revisit
> only if the universe / cost structure / data (e.g. order-flow) changes.

## Baseline result (2026-05-24) — decision: do NOT build

`backend/scripts/intraday_baseline.py` answered the "is there ANY
net-of-cost edge?" gate before investing in the full build. Setup: top 50
liquid names with ~1y of 5-min bars, XGBoost argmax, 60-min horizon,
sqrt(horizon)-scaled ATR barriers (2.08/1.04 x 5-min ATR), 1x-capital cap,
MIS cost stack + slippage, daily-aggregated equity.

| Features | Gross PnL/trade | Win rate | Net/trade |
|----------|-----------------|----------|-----------|
| Basic technicals (on 5-min) | -Rs 32.4 (~0%) | 28% | -Rs 117 |
| + intraday-specific (OR, session-VWAP, time-of-day, rel-volume) | -Rs 32.7 (~0%) | 28% | -Rs 112 |

The intraday-specific features moved gross edge by ~Rs 1.4/trade —
statistically nothing. Gross edge is flat coin-flip with *or without* the
features the design (section 3) bet on; the **0.085%/trade MIS cost wall**
turns flat-zero gross into a guaranteed net loss. This is a real "no edge
on this universe", not a tuning miss. The cheap baseline (a day of work)
saved the multi-week full build below.

Conditions that would justify revisiting: a different universe, materially
lower costs, or new data the daily features can't proxy (order-book
depth/imbalance, which is forward-only — see section 3). Until then,
**run swing-only modes (short_term / long_term); never `intraday` or
`balanced`** — the daily-trained "intraday" model has negative real edge
(argmax Sharpe ~ -7) and the Phase-0 honest-edge gate will retire it on
the next retrain.

## TL;DR

The model we ship as `intraday` today trains on **daily** OHLCV with a
**1-day lookahead** — it predicts *tomorrow's* daily direction, which is
close to a coin flip (~53% win, ~0.5 max conviction, ~48% out-of-sample
drawdown). It does **not** use the 5-minute bars we ingest; those feed live
position management only. A real intraday model has to be built on intraday
bars with intraday labels, features, leakage controls, and cost realism.
This is a project, not a config change.

## Why the current "intraday" model is not intraday

- `model_retrain` pulls training data from `db.get_training_dataset()`, which
  is hard-coded to `interval = 'daily'`. Both `intraday` and `swing` train on
  the **same daily dataset**.
- The only difference is the label horizon (`model_retrain.py`:
  `lookahead_map = {"intraday": 1, "swing": 10}`) and the ATR target/SL
  multipliers (`holding_periods.intraday` 0.6/0.3 vs `short_swing` 1.5/0.75).
- Proof in the logs: `intraday n≈724k` ≈ `swing n≈719k`. Intraday on 5-min
  bars would be ~75× more samples (≈75 five-minute bars per trading day).
- Predicting **next-day direction on daily bars** is near-random; that is the
  root cause of the low conviction and the unreachable thresholds, not a
  pipeline bug. (The pipeline is now honest — see "Lessons" below.)

5-min bars (`ohlcv` `interval='5minute'`) are currently consumed only by:
`position-monitor` volume-exhaustion exits, the live LTP/ticker feed, and
`backfill-intraday` that populates them. `retention.intraday_ohlcv_days`
defaults short (365d) because they were never meant for training.

## Goals for a real intraday model

1. Train on **intraday (5-min) bars** with **intraday labels** and a
   **same-day exit** assumption (MIS — no overnight hold).
2. Produce **decisive** predictions (high enough conviction to clear
   reachable thresholds) on a horizon that actually has signal
   (intra-session momentum / mean-reversion, not next-day noise).
3. Keep the **honesty discipline** we already built (see "Lessons").
4. Be **net-of-cost positive** — intraday lives or dies on slippage/brokerage.

## 1. Data

- New training path that pulls `interval='5minute'` (don't overload the
  daily `get_training_dataset`; add `get_intraday_training_dataset` or a
  `interval` arg).
- History constraint: we only keep ~365d of 5-min bars vs 5y of daily.
  Decide retention + backfill depth for intraday training
  (`market_data.intraday_backfill_days`, `retention.intraday_ohlcv_days`).
  Realistically start with **a liquid subset** (F&O names / high-ADV) rather
  than the full Nifty 500 — see "Memory".
- Per-day session boundaries matter: 5-min bars span 09:15–15:30 IST
  (~75 bars/day). Cumulative/session features must reset each day.

## 2. Labels

- Lookahead in **5-min bars**, not days. Candidate horizons: next 6–12 bars
  (30–60 min) or "to end of session."
- **Path-aware** on 5-min OHLC with intraday ATR-based target/SL (mirror the
  existing `_path_aware_label`, but on the 5-min series and with intraday
  multipliers).
- **Hard same-day close-out**: the label window must **not cross the session
  boundary** into the next day's bars. A trade that doesn't hit target/SL by
  ~square-off exits at the session-close price (MIS reality). This avoids the
  overnight-gap contamination that daily labels suffer.
- Consider a **"P(target before SL within session)"** binary/triple label
  rather than raw direction — it maps directly to the trade decision.

## 3. Features (intraday-specific — daily can't express these)

- **Time-of-day / session phase** (minutes since open; we already have
  `_minutes_since_open` / `day_phase` — reuse, but on the 5-min clock).
- **Opening range**: first 15–30 min high/low; breakout/breakdown distance.
- **Session VWAP distance** (proper running session VWAP — *not* the
  year-long P/V average; note the existing daily `vwap` is excluded from the
  daily model for this reason, and `mvwap_20d` is the daily replacement).
- **Relative volume by time-of-day**: cumulative session volume vs the
  typical profile for that minute-of-day (intraday volume is U-shaped).
- **Overnight gap** from prior close; **distance from prior-day H/L/close**
  (classic pivots).
- **Intraday momentum / RSI / ATR on the 5-min series**.
- **Realized intraday volatility** (last N 5-min bars).
- Optional, forward-only: **order-book depth / imbalance** (Kite L1/L2) —
  can't be backfilled, so it only helps after accumulation (same story as
  the F&O features).

## 4. Leakage & cross-validation (intraday-specific)

- **Datetime purge, not date purge**: with ~75 samples/day, the walk-forward
  CV / holdout split must purge by *timestamp* (the daily code purges by
  calendar date; that's too coarse intraday).
- **No cross-session label leakage** (see Labels) — and the train/holdout
  split must fall on a **day boundary**, never mid-session.
- Reuse the **final-scale holdout threshold tuning** we just built
  (train tuning model on early data, score a strict-future holdout, tune on
  first half / report on second half). It applies unchanged in spirit.

## 5. Backtest realism (this is where intraday strategies die)

- **Costs dominate** intraday. The walk-forward backtest must model per-trade
  brokerage (MIS), STT, exchange + GST, and **realistic intraday slippage**
  (wider than daily; spread-aware). Extend `compute_transaction_costs` /
  `walk_forward_backtest` for the MIS intraday cost stack.
- **Enforce square-off** in the backtest (no overnight carry).
- **Sharpe annualization**: many trades/day → aggregate to a **daily** equity
  curve first (we already do full-calendar daily Sharpe for daily; the
  intraday path must aggregate intra-day PnL to daily before annualizing, or
  the number explodes).
- Bench against a **null/cost-only baseline** — if net-of-cost edge ≈ 0,
  stop; intraday with retail costs is brutal.

## 6. Model / target formulation

- The 3-class softprob with ~60% HOLD prior **compresses conviction toward
  HOLD** — that's a big reason max probability sits ~0.5. For intraday,
  consider:
  - **2-class direction** (up/down) with an explicit no-trade gate, or
  - **meta-labeling**: a cheap primary trigger (e.g. opening-range breakout,
    VWAP reclaim) + a model that predicts *whether to take it*. Meta-labeling
    naturally yields higher, more usable probabilities.
- Keep the **reachable-threshold** discipline: tune thresholds against the
  deployed model's scale; don't ship cutoffs the live model can't reach.

## 7. Memory & compute

- 5-min × 500 symbols × 1y ≈ **~9–10M bars**; with a 200-bar window +
  per-sample feature computation the feature matrix is far larger than the
  daily 724k. Options:
  - **Reduce the universe** to liquid F&O names (the only realistic intraday
    tradables anyway),
  - shorter window / fewer features,
  - **chunked / out-of-core** training (XGBoost external memory, or train on
    a sampled subset),
  - downsample HOLD-heavy samples.
- This will not fit the daily retrain's memory envelope; budget for it
  explicitly (the offline big-box workflow we built helps here).

## 8. Operational integration

- A 5-min model slots into `generate-signals` for `intraday` mode using the
  **live 5-min bars** already flowing in (ingest-data + ticker).
- **Inference cadence**: predicting across the universe every 5-min bar is
  heavier than the 15-min (now 10-min) heartbeat — mind the rate limiter and
  concurrency.
- Respect `market_hours.intraday_cutoff` (no new MIS late in the day) and
  the EOD square-off.

## 9. Lessons already banked (don't regress these)

These were fixed for the daily models on branch `claude/model-hold-bias-a05c9`
and **must carry into any intraday build**:

- **Tune thresholds at the deployed model's scale** (final-scale holdout),
  not per-fold OOF — otherwise tuned cutoffs are unreachable and every signal
  collapses to HOLD.
- **Bound the threshold sweep** to the production-reachable range
  (`tuned_threshold_max_value` / `_max_diff`).
- **Decide deploy/promote on the bootstrapped lower-bound Sharpe**, not the
  noisy single-holdout point estimate.
- **Honest CV**: global date(time) sort + label-window purge; calibration
  with `TimeSeriesSplit`, never a shuffled split.
- **Report metrics on a held-out slice** the thresholds weren't chosen on.
- **Survivorship**: retention floor keeps exited/delisted names in the
  training window; intraday has the same issue.

## 10. Suggested phasing

1. **Plumbing**: `get_intraday_training_dataset`, intraday label fn (path-
   aware, same-day close-out, datetime purge), on a small liquid universe.
   Reuse the existing final-scale-holdout tuning + reachable-threshold caps.
2. **Features**: opening range, session VWAP, relative-volume-by-time,
   gaps/pivots, intraday momentum.
3. **Cost realism**: MIS cost stack + intraday slippage + square-off in the
   backtest; intraday→daily Sharpe aggregation.
4. **Formulation**: try meta-labeling / 2-class vs the current 3-class.
5. **Validate**: shadow against the (benched) next-day model; promote only on
   live scored-accuracy + lower-bound Sharpe.

## Open questions to resolve next session

- How much 5-min history can we realistically get/keep? (1y now; is that
  enough, or backfill more for a liquid subset?)
- Which universe for intraday training — F&O only? Top-N by ADV?
- Horizon: fixed N-bars vs to-session-close vs first-target-touch?
- Is the edge net-of-cost positive at all on this universe? (Decide early
  with a cheap baseline before investing in features.)

## Decisions & progress (2026-05-26)

- **Universe**: Nifty 100 (liquid large-caps; intraday MIS viability). Both
  `backfill-intraday` (5m) and `backfill-intraday-1m` default to it.
- **Horizon**: to session close — `intraday_triple_barrier_label` walked with
  a full-session (375min) horizon so the same-session boundary is the binding
  stop (MIS auto-squares EOD).
- **Integration**: the existing `intraday` model slot is retrained on 5m+1m
  (no new slot / no ml_signal refactor); `swing` stays daily. The old
  daily-bar intraday model is retired in place. Enters as **shadow** — the
  lower-bound-Sharpe + live-accuracy promotion gate still guards production.
- **Done**: `get_intraday_training_dataset`, dual-resolution labels,
  `_prepare_intraday_training_data` (prior-session broadcast, leak-free),
  `_build_intraday_matrix` (memory-safe symbol-chunked fetch + concat),
  execute() wiring (MIS-cost walk-forward already keyed off `product`).
- **Next**: confirm net-of-cost baseline Sharpe once the clean backfill lands
  (decide go/no-go before feature work); then live 5m inference in
  generate-signals (§8) + intraday-specific features (§2).
- **Open**: 5m can be backfilled deeper (`intraday_backfill_days`) than the
  1m window (`intraday_ohlcv_days`); only the 1m-covered span is labelable,
  so the older 5m is inert — fine, but keep the windows in mind.
