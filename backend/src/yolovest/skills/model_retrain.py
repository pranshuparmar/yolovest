"""Skill: model-retrain — Retrain ML models and manage versioning.

Trigger: CRON — configurable via retraining.schedule_cron (default: Saturday 6 AM)
Pipeline position: Offline — runs outside market hours.

Flow:
1. Load accumulated prediction vs actual data from DB
2. Load latest OHLCV + features data
3. Retrain both intraday and swing models
4. Version the new model artifacts with metrics
5. Compare new model metrics vs current production model
6. If improved: deploy to shadow mode for retraining.shadow_mode_days
7. If shadow model outperforms after N days: promote to production
8. If shadow model underperforms: rollback to previous version
9. Use Gemini to analyze prediction failures
10. Store analysis for dashboard display
"""

import logging
from typing import Any

from yolovest.data.features import IndicatorConfig, compute_features, merge_feedback_features
from yolovest.models.schemas import OHLCVBar
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class ModelRetrainSkill(SkillBase):
    name = "model-retrain"
    description = "Retrain ML models, version artifacts, A/B test"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.ctx.config.retraining.schedule_cron

    def should_run(self) -> bool:
        return not self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        if self.ctx.ml is None:
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"reason": "no_ml_provider"},
            )

        cfg = self.ctx.config.retraining
        min_samples = self.ctx.config.strategy.min_training_samples

        # Step 1-2: Load training data and feedback. max_training_days
        # caps history so the feature matrix fits in RAM on small
        # hosts (a 2 GB EC2 instance OOMs on 5 years × ~500 symbols).
        training_data = await self.ctx.db.get_training_dataset(
            max_days=cfg.max_training_days,
        )
        predictions_vs_actual = await self.ctx.db.get_prediction_outcomes()

        # Sector map for sector-relative momentum features. A stock
        # outperforming its sector is a stronger signal than just
        # outperforming the universe; the model gets both.
        unique_symbols = sorted({
            row.get("symbol", "") for row in training_data.get("bars", [])
            if row.get("symbol")
        })
        try:
            sector_map = await self.ctx.db.get_symbol_sectors_map(unique_symbols)
        except Exception:
            logger.warning("Failed to load sector map; sector features will be neutral", exc_info=True)
            sector_map = {}

        # Bulk-deal lookup for ML features. We pre-build once because
        # _prepare_training_data is sync and one DB query per sample
        # would be prohibitively slow on a year of training data.
        bulk_deal_lookup: dict[tuple[str, str], dict[str, int]] = {}
        try:
            deals_timeline = await self.ctx.db.get_bulk_deals_timeline()
            for d in deals_timeline:
                key = (d["symbol"], d["deal_date"])
                counts = bulk_deal_lookup.setdefault(key, {"buy": 0, "sell": 0})
                bs = str(d.get("buy_sell", "")).upper()
                if bs == "BUY":
                    counts["buy"] += 1
                elif bs == "SELL":
                    counts["sell"] += 1
        except Exception:
            logger.warning(
                "Failed to load bulk-deals timeline; bulk-deal features will be 0",
                exc_info=True,
            )

        # Load feedback data for the ML feedback loop
        feedback_cfg = self.ctx.config.strategy.feedback
        feedback_data: dict[str, dict[str, float]] | None = None
        if feedback_cfg.enabled:
            feedback_data = await self.ctx.db.get_feedback_data(
                lookback_days=feedback_cfg.lookback_days,
            )
            logger.info(
                "Feedback loop: loaded data for %d symbols (lookback=%dd)",
                len(feedback_data), feedback_cfg.lookback_days,
            )

        # Guard: minimum training data
        bar_count = len(training_data.get("bars", []))
        if bar_count < min_samples:
            logger.warning(
                "Insufficient training data (%d bars, need %d), skipping retrain",
                bar_count, min_samples,
            )
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"reason": "insufficient_data", "bar_count": bar_count},
            )

        # Step 3: Retrain models
        results: dict[str, Any] = {}
        shadow_deployed = []

        # Lookahead periods: intraday uses 1-bar, swing uses 5-bar returns
        lookahead_map = {"intraday": 1, "swing": 5}

        # Match each model's path-aware label geometry to the holding
        # bucket it actually trades at runtime: intraday uses the tight
        # MIS multipliers (0.6 / 0.3 by default), swing uses the wider
        # short-swing CNC multipliers (1.5 / 0.75). Keeping label
        # geometry in sync with runtime geometry is the whole point of
        # path-aware labels — otherwise the model learns one game and
        # plays a different one.
        hp = self.ctx.config.strategy.holding_periods
        atr_mult_map = {
            "intraday": (hp.intraday.target, hp.intraday.stop_loss),
            "swing": (hp.short_swing.target, hp.short_swing.stop_loss),
        }

        for model_type in ("intraday", "swing"):
            # Build feature matrix with model-specific labeling + feedback features
            lookahead = lookahead_map[model_type]
            target_mult, sl_mult = atr_mult_map[model_type]
            X, y, feat_names, sample_weights, bars_meta = self._prepare_training_data(
                training_data, lookahead_bars=lookahead, feedback_data=feedback_data,
                target_atr_mult=target_mult, sl_atr_mult=sl_mult,
                sector_map=sector_map,
                bulk_deal_lookup=bulk_deal_lookup,
            )
            if len(y) < min_samples:
                logger.warning(
                    "Insufficient %s feature samples (%d, need %d), skipping",
                    model_type, len(y), min_samples,
                )
                results[model_type] = {
                    "error": f"insufficient_features ({len(y)} < {min_samples})"
                }
                continue

            # Label distribution — settles "is BUY even represented in
            # training?" when the live model is producing zero BUYs.
            # Class ints: 0=SELL, 1=HOLD, 2=BUY (per _path_aware_label).
            label_counts = {"SELL": 0, "HOLD": 0, "BUY": 0}
            for label in y:
                if label == 0:
                    label_counts["SELL"] += 1
                elif label == 1:
                    label_counts["HOLD"] += 1
                elif label == 2:
                    label_counts["BUY"] += 1
            total = sum(label_counts.values()) or 1
            label_pct = {
                k: round(v / total * 100, 1) for k, v in label_counts.items()
            }
            logger.info(
                "Label distribution for %s: BUY=%d (%.1f%%), HOLD=%d (%.1f%%), SELL=%d (%.1f%%) [n=%d]",
                model_type,
                label_counts["BUY"], label_pct["BUY"],
                label_counts["HOLD"], label_pct["HOLD"],
                label_counts["SELL"], label_pct["SELL"],
                total,
            )

            try:
                await self.broadcast("retrain_progress", {
                    "model_type": model_type,
                    "status": "training",
                    "samples": len(y),
                })
                train_params: dict[str, Any] = {}
                if sample_weights:
                    train_params["sample_weights"] = sample_weights
                # Real-PnL backtest config: intraday model uses MIS for
                # cost calc (lower STT); swing model uses CNC.
                train_params["bars_meta"] = bars_meta
                train_params["backtest_product"] = (
                    "MIS" if model_type == "intraday" else "CNC"
                )
                # Bound the backtest's concurrent-positions count to
                # the same cap the live engine enforces. Without
                # this, the simulator treats every signal as
                # independently fillable and inflates Sharpe (e.g.
                # 12.98 intraday on the user's last run).
                train_params["backtest_max_positions"] = (
                    self.ctx.config.risk.max_open_positions
                )
                metrics = await self.ctx.ml.train(
                    model_type, X, y, train_params, feature_names=feat_names,
                )
                # Stash label distribution so MLModelsPage can show
                # whether a given checkpoint was trained on a class-
                # balanced sample or a heavily-skewed one. Lives next
                # to the existing numeric metrics in metrics_json.
                metrics["label_counts"] = label_counts
                metrics["label_pct"] = label_pct
                version = await self.ctx.ml.save_model(model_type, metrics=metrics)
                await self.broadcast("retrain_progress", {
                    "model_type": model_type,
                    "status": "completed",
                    "version": version,
                    "sharpe": metrics.get("sharpe"),
                    "win_rate": metrics.get("win_rate"),
                })
                await self.ctx.db.save_model_version(
                    model_type, version, f"models/{model_type}_{version}.pkl", metrics
                )

                # Step 5: Compare with production
                current = await self.ctx.db.get_production_model(model_type)
                current_sharpe = (current.get("sharpe_ratio") or 0) if current else 0

                improved = metrics.get("sharpe", 0) > current_sharpe
                if improved:
                    shadow_deployed.append(model_type)

                results[model_type] = {
                    "version": version,
                    "metrics": metrics,
                    "improved": improved,
                }
            except Exception as e:
                logger.warning("Retrain failed for %s: %s", model_type, e)
                results[model_type] = {"error": str(e)}
            # Free per-model scratch (feature matrix + bars_meta) before
            # the next model's _prepare_training_data allocates its
            # own copy. Without this the intraday and swing matrices
            # would briefly coexist and OOM the process on a 2 GB host.
            X = y = feat_names = sample_weights = bars_meta = None  # type: ignore[assignment]
            import gc as _gc
            _gc.collect()

        # Free training_data eagerly — _check_shadow_promotions doesn't
        # need it and it's the largest single resident structure
        # (~365K rows × 8 fields at the default 730-day cap, much more
        # if the user raised retraining.max_training_days).
        training_data = None  # type: ignore[assignment]
        _gc.collect()

        # Step 7: Check shadow promotions
        promotions = await self._check_shadow_promotions()

        # Step 9: Gemini failure analysis
        failure_analysis = None
        if predictions_vs_actual and self.ctx.config.llm.enabled:
            failures = [p for p in predictions_vs_actual if not p.get("direction_correct")]
            if failures:
                try:
                    failure_analysis = await self.ctx.llm.analyze_prediction_failures(failures)
                    await self.ctx.db.store_failure_analysis(failure_analysis)
                except Exception as e:
                    logger.warning("Failure analysis failed: %s", e)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "models": results,
                "shadow_deployed": shadow_deployed,
                "promotions": promotions,
                "failure_analysis_generated": failure_analysis is not None,
            },
        )

    def _prepare_training_data(
        self, training_data: dict[str, Any], lookahead_bars: int = 1,
        feedback_data: dict[str, dict[str, float]] | None = None,
        target_atr_mult: float = 1.5,
        sl_atr_mult: float = 0.75,
        sector_map: dict[str, str] | None = None,
        bulk_deal_lookup: dict[tuple[str, str], dict[str, int]] | None = None,
    ) -> tuple[
        list[list[float]], list[int], list[str], list[float],
        list[dict[str, Any]],
    ]:
        """Convert raw OHLCV bars into feature matrix X, labels y, and sample weights.

        Groups bars by symbol, computes technical features using a sliding window,
        and generates labels based on future price returns over lookahead_bars:
          - BUY (2): return > +0.5%
          - SELL (0): return < -0.5%
          - HOLD (1): otherwise

        When feedback_data is provided:
          - Merges per-symbol feedback features (prediction accuracy, trade win rate, etc.)
          - Computes sample weights: upweights symbols where the model recently performed poorly

        Args:
            training_data: Dict with "bars" key containing OHLCV row dicts.
            lookahead_bars: Number of bars to look ahead for labeling.
            feedback_data: Per-symbol feedback stats from get_feedback_data().
        """
        raw_bars = training_data.get("bars", [])
        weight_boost = self.ctx.config.strategy.feedback.sample_weight_boost

        # Group bars by symbol, preserving time order
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in raw_bars:
            sym = row["symbol"]
            by_symbol.setdefault(sym, []).append(row)

        indicator_cfg = IndicatorConfig(
            rsi=self.ctx.config.strategy.indicators.rsi,
            macd=self.ctx.config.strategy.indicators.macd,
            bollinger_bands=self.ctx.config.strategy.indicators.bollinger_bands,
            vwap=self.ctx.config.strategy.indicators.vwap,
            atr=self.ctx.config.strategy.indicators.atr,
            volume_profile=self.ctx.config.strategy.indicators.volume_profile,
            obv=self.ctx.config.strategy.indicators.obv,
            supertrend=self.ctx.config.strategy.indicators.supertrend,
            ema_periods=self.ctx.config.strategy.ema_periods,
        )

        # Minimum window size for feature computation (need enough bars for indicators)
        window_size = 50
        X: list[list[float]] = []
        y: list[int] = []
        sample_weights: list[float] = []
        feature_names: list[str] = []
        feature_names_set: set[str] = set()
        # Parallel to X/y — used by the walk-forward backtest to
        # simulate real PnL instead of the legacy +1%/-0.5% fiction.
        bars_meta: list[dict[str, Any]] = []

        # Cross-sectional market-regime features. For each timestamp
        # (date for daily bars, datetime for intraday) compute the
        # universe-wide breadth: fraction of stocks up vs prior close
        # and the average %-return. This proxies the "is today
        # broadly trending or chopping" context that the per-stock
        # features can't see, without requiring a separate NIFTY
        # ingest. Built once up-front, then looked up per-sample.
        regime_by_ts: dict[str, dict[str, float]] = self._compute_regime_index(by_symbol)

        # Sector-relative features. Compute per-(sector, ts) breadth
        # and avg-return plus a per-(symbol, ts) return so the sample
        # build can derive `relative_momentum` = stock_return -
        # sector_avg_return. A stock outperforming its sector index
        # is a stronger signal than just outperforming the universe.
        sector_map = sector_map or {}
        sector_regime, symbol_returns = self._compute_sector_index(
            by_symbol, sector_map,
        )

        # Bulk-deal lookup: (symbol, deal_date) -> {"buy", "sell"} counts.
        # Pre-built in execute() (async) and passed in via bulk_deal_lookup
        # so we only need one DB scan instead of one query per sample.
        bulk_deal_lookup = bulk_deal_lookup or {}
        # Per-symbol sorted deal-date list for fast 5-day window lookups.
        bulk_dates_by_sym: dict[str, list[str]] = {}
        for (sym_key, date_key) in bulk_deal_lookup.keys():
            bulk_dates_by_sym.setdefault(sym_key, []).append(date_key)
        for v in bulk_dates_by_sym.values():
            v.sort()

        for sym, rows in by_symbol.items():
            if len(rows) < window_size + 1:
                continue

            # Compute sample weight for this symbol based on recent performance
            sym_weight = 1.0
            if feedback_data and sym in feedback_data:
                fb = feedback_data[sym]
                # Upweight symbols where model accuracy was poor (< 50%)
                pred_acc = fb.get("pred_accuracy", 0.5)
                dry_acc = fb.get("dry_run_accuracy", 0.5)
                # Use worst accuracy signal to determine weight
                worst_acc = min(pred_acc, dry_acc)
                if worst_acc < 0.5:
                    sym_weight = weight_boost

            # Convert rows to OHLCVBar objects for compute_features
            bars = [
                OHLCVBar(
                    timestamp=r["timestamp"],
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                )
                for r in rows
            ]

            # Sliding window: compute features at position i, label from i+lookahead
            for i in range(window_size, len(bars) - lookahead_bars):
                window = bars[i - window_size : i + 1]
                features = compute_features(window, indicator_cfg)
                if not features:
                    continue

                # Merge feedback features for this symbol
                if feedback_data:
                    merge_feedback_features(features, sym, feedback_data)

                # Merge cross-sectional market-regime features for this
                # timestamp (universe breadth + avg %-return). Symbols
                # alone can't tell the model "today is a chop day" —
                # this layer does. Key is the YYYY-MM-DD date string
                # to match the index built by _compute_regime_index.
                _ts = bars[i].timestamp.strftime("%Y-%m-%d")
                _regime = regime_by_ts.get(_ts)
                if _regime:
                    features["universe_breadth"] = _regime["breadth"]
                    features["universe_avg_return"] = _regime["avg_return"]
                else:
                    features["universe_breadth"] = 0.5
                    features["universe_avg_return"] = 0.0

                # Sector-relative features. relative_momentum is the
                # main signal — stock's return minus its sector's
                # average return. Falls back to neutral when the
                # symbol's sector is unknown or has < 3 peers at this
                # timestamp.
                _sec = sector_map.get(sym)
                _sec_stats = sector_regime.get((_sec, _ts)) if _sec else None
                _stock_ret = symbol_returns.get((sym, _ts))
                if _sec_stats and _stock_ret is not None:
                    features["sector_breadth"] = _sec_stats["breadth"]
                    features["sector_avg_return"] = _sec_stats["avg_return"]
                    features["relative_momentum"] = (
                        _stock_ret - _sec_stats["avg_return"]
                    )
                else:
                    features["sector_breadth"] = 0.5
                    features["sector_avg_return"] = 0.0
                    features["relative_momentum"] = 0.0

                # Institutional flow features: bulk-deal net count and
                # average delivery % over the prior 5 bars. Both default
                # to 0 when no data is available (older training rows
                # predate the data sources). Compounds with the
                # institutional_flow risk-check multiplier so the model
                # learns to score these signals natively at inference.
                # bars[i].timestamp is a datetime; the bulk_deals table
                # stores deal_date as YYYY-MM-DD strings, so format
                # consistently before the lookup.
                _sample_date = bars[i].timestamp.strftime("%Y-%m-%d")
                _bulk_window_start = bars[max(0, i - 5)].timestamp.strftime("%Y-%m-%d")
                _bd_dates = bulk_dates_by_sym.get(sym, [])
                _bd_buy = _bd_sell = 0
                for d in _bd_dates:
                    if d > _sample_date:
                        break
                    if d >= _bulk_window_start:
                        counts = bulk_deal_lookup.get((sym, d), {})
                        _bd_buy += counts.get("buy", 0)
                        _bd_sell += counts.get("sell", 0)
                features["bulk_deal_buy_5d"] = float(_bd_buy)
                features["bulk_deal_sell_5d"] = float(_bd_sell)
                features["bulk_deal_net_5d"] = float(_bd_buy - _bd_sell)

                # delivery_pct rolling-5 average. bars[i] is the current
                # sample's bar; look back 5 bars including it. Rows
                # ingested before migration 038 have NULL → treated as 0.
                _delivery_values: list[float] = []
                for k in range(max(0, i - 4), i + 1):
                    dp = getattr(bars[k], "delivery_pct", None)
                    if dp is None and isinstance(rows[k], dict):
                        dp = rows[k].get("delivery_pct")
                    if dp is not None:
                        try:
                            _delivery_values.append(float(dp))
                        except (TypeError, ValueError):
                            pass
                if _delivery_values:
                    features["delivery_pct_avg_5d"] = (
                        sum(_delivery_values) / len(_delivery_values)
                    )
                else:
                    features["delivery_pct_avg_5d"] = 0.0

                # Path-aware label: BUY iff target hits before SL when
                # walking forward bar-by-bar, using the same ATR-based
                # geometry the live trades use. Replaces the legacy
                # close[i+N] vs close[i] ±0.5% rule, which was blind to
                # intra-window SL hits and didn't match runtime exits.
                current_close = bars[i].close
                future_close = bars[i + lookahead_bars].close
                atr_pct = features.get("atr_pct") or 0.0
                if current_close <= 0 or atr_pct <= 0:
                    label = 1
                else:
                    label = self._path_aware_label(
                        bars=bars,
                        start_idx=i,
                        lookahead=lookahead_bars,
                        entry=current_close,
                        target_pct=atr_pct * target_atr_mult,
                        sl_pct=atr_pct * sl_atr_mult,
                    )

                # Maintain a stable feature_names list across all samples.
                # Indicators that need more history (e.g. EMA-200) only
                # appear in features once enough bars are in the window —
                # so later samples may produce keys the first sample
                # didn't have. When that happens, extend feature_names
                # and backfill 0.0 into every prior row so np.array(X)
                # ends up rectangular instead of inhomogeneous.
                for k in features:
                    if k not in feature_names_set:
                        feature_names.append(k)
                        feature_names_set.add(k)
                        for existing in X:
                            existing.append(0.0)
                X.append([features.get(k, 0.0) for k in feature_names])
                y.append(label)
                sample_weights.append(sym_weight)
                # Capture the future-window high/low path so the
                # walk-forward backtest can exit at SL or target with
                # the same geometry as the path-aware label, instead of
                # mark-to-market at exit_close.
                window_end = min(i + lookahead_bars, len(bars) - 1)
                path_highs = [bars[k].high for k in range(i + 1, window_end + 1)]
                path_lows = [bars[k].low for k in range(i + 1, window_end + 1)]
                bars_meta.append({
                    "symbol": sym,
                    "entry_close": float(current_close),
                    "exit_close": float(future_close),
                    "path_highs": path_highs,
                    "path_lows": path_lows,
                    "target_pct": float(atr_pct * target_atr_mult),
                    "sl_pct": float(atr_pct * sl_atr_mult),
                    # YYYY-MM-DD — walk_forward_backtest aggregates by
                    # this to compute daily-equity-curve Sharpe instead
                    # of the per-trade approximation. Several trades on
                    # the same day get netted before the Sharpe stdev.
                    "entry_date": _sample_date,
                })

        return X, y, feature_names, sample_weights, bars_meta

    @staticmethod
    def _compute_regime_index(
        by_symbol: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, float]]:
        """For each unique timestamp across the training set, aggregate
        the universe to a {breadth, avg_return} dict.

        breadth = fraction of symbols whose close > prior close at that
                  timestamp (0..1). 0.5 = neutral, ≥0.6 strong up,
                  ≤0.4 strong down.
        avg_return = mean of (close − prev_close) / prev_close across
                     symbols at that timestamp.

        This is a cross-sectional proxy for "what is the broad market
        doing right now". It mirrors what a NIFTY 50 day-return feature
        would give but doesn't require a separate index ingest — the
        500-stock universe alone is more than enough breadth.
        """
        # Build per-timestamp aggregator. Date-string key (YYYY-MM-DD)
        # so it matches sample-time lookups that derive the key from
        # bars[i].timestamp (a datetime, formatted to date-only).
        # Without the explicit date prefix, full ISO strings and
        # datetime objects miss each other and every sample falls back
        # to the neutral 0.5 breadth — silently neutering the feature.
        agg: dict[str, list[float]] = {}
        for rows in by_symbol.values():
            prev_close: float | None = None
            for r in rows:
                raw_ts = r.get("timestamp")
                ts = str(raw_ts)[:10] if raw_ts else ""
                c = r.get("close") or 0.0
                if ts and prev_close and prev_close > 0:
                    ret = (c - prev_close) / prev_close
                    agg.setdefault(ts, []).append(ret)
                prev_close = c

        # Need at least 5 symbols at a timestamp for a meaningful breadth
        # reading — otherwise sparse-data timestamps would dominate with
        # noisy 0/1 fractions.
        out: dict[str, dict[str, float]] = {}
        for ts, returns in agg.items():
            if len(returns) < 5:
                continue
            avg_ret = sum(returns) / len(returns)
            up = sum(1 for r in returns if r > 0)
            out[ts] = {
                "breadth": up / len(returns),
                "avg_return": avg_ret,
            }
        return out

    @staticmethod
    def _compute_sector_index(
        by_symbol: dict[str, list[dict[str, Any]]],
        sector_map: dict[str, str],
    ) -> tuple[
        dict[tuple[str, str], dict[str, float]],
        dict[tuple[str, str], float],
    ]:
        """Aggregate per-(sector, ts) breadth + avg_return, and emit
        the per-(symbol, ts) return series so the sample builder can
        compute relative_momentum cheaply.

        Returns: (sector_stats, symbol_returns) where
          sector_stats[(sector, ts)] = {"breadth": .., "avg_return": ..}
          symbol_returns[(symbol, ts)] = return  (close - prev) / prev

        Sectors with < 3 peers at a given timestamp are dropped — small
        cohorts produce noisy breadth and the model is better served
        falling back to neutral than learning from noise.
        """
        # Date-string keys (YYYY-MM-DD), see _compute_regime_index for
        # why — sample-time lookups derive the key from a datetime and
        # mismatched key types silently zero the features out.
        sector_agg: dict[tuple[str, str], list[float]] = {}
        symbol_returns: dict[tuple[str, str], float] = {}
        for sym, rows in by_symbol.items():
            sector = sector_map.get(sym)
            prev_close: float | None = None
            for r in rows:
                raw_ts = r.get("timestamp")
                ts = str(raw_ts)[:10] if raw_ts else ""
                c = r.get("close") or 0.0
                if ts and prev_close and prev_close > 0:
                    ret = (c - prev_close) / prev_close
                    symbol_returns[(sym, ts)] = ret
                    if sector:
                        sector_agg.setdefault((sector, ts), []).append(ret)
                prev_close = c

        sector_stats: dict[tuple[str, str], dict[str, float]] = {}
        for key, returns in sector_agg.items():
            if len(returns) < 3:
                continue
            up = sum(1 for x in returns if x > 0)
            sector_stats[key] = {
                "breadth": up / len(returns),
                "avg_return": sum(returns) / len(returns),
            }
        return sector_stats, symbol_returns

    @staticmethod
    def _path_aware_label(
        *,
        bars: list["OHLCVBar"],
        start_idx: int,
        lookahead: int,
        entry: float,
        target_pct: float,
        sl_pct: float,
    ) -> int:
        """Simulate hypothetical BUY and SELL trades from `start_idx`
        and label by which (if either) hits its target before its SL,
        walking forward bar-by-bar over `lookahead` future bars.

        - BUY:  target_hit when high ≥ entry × (1 + target_pct)
                 SL_hit    when low  ≤ entry × (1 − sl_pct)
        - SELL: target_hit when low  ≤ entry × (1 − target_pct)
                 SL_hit    when high ≥ entry × (1 + sl_pct)

        Both touched in the same bar is treated as ambiguous (HOLD)
        because daily OHLC can't tell us the intra-bar order.

        Returns: 2 BUY, 0 SELL, 1 HOLD.
        """
        buy_target = entry * (1 + target_pct)
        buy_sl = entry * (1 - sl_pct)
        sell_target = entry * (1 - target_pct)
        sell_sl = entry * (1 + sl_pct)

        buy_outcome: str | None = None  # "win" / "loss" / None
        sell_outcome: str | None = None

        end_idx = min(start_idx + lookahead, len(bars) - 1)
        for k in range(start_idx + 1, end_idx + 1):
            bar = bars[k]
            hi, lo = bar.high, bar.low

            # BUY trade leg
            if buy_outcome is None:
                target_now = hi >= buy_target
                sl_now = lo <= buy_sl
                if target_now and sl_now:
                    buy_outcome = "ambiguous"
                elif target_now:
                    buy_outcome = "win"
                elif sl_now:
                    buy_outcome = "loss"

            # SELL trade leg
            if sell_outcome is None:
                target_now = lo <= sell_target
                sl_now = hi >= sell_sl
                if target_now and sl_now:
                    sell_outcome = "ambiguous"
                elif target_now:
                    sell_outcome = "win"
                elif sl_now:
                    sell_outcome = "loss"

            if buy_outcome is not None and sell_outcome is not None:
                break

        # Decide label. Only label BUY/SELL when one side cleanly won
        # and the other didn't also win — otherwise HOLD.
        if buy_outcome == "win" and sell_outcome != "win":
            return 2
        if sell_outcome == "win" and buy_outcome != "win":
            return 0
        return 1

    async def _check_shadow_promotions(self) -> list[dict[str, Any]]:
        """Check if shadow models have completed trial period.

        Shadow models that have run for >= shadow_mode_days are evaluated:
        - If shadow metrics (Sharpe, win_rate) >= production metrics: promote
        - Otherwise: retire the shadow model (rollback)
        """
        cfg = self.ctx.config.retraining
        shadow_models = await self.ctx.db.get_shadow_models_ready(cfg.shadow_mode_days)
        promotions = []

        for shadow in shadow_models:
            model_type = shadow["model_type"]
            current = await self.ctx.db.get_production_model(model_type)

            # Compare: shadow must beat current production on Sharpe ratio
            shadow_sharpe = shadow.get("sharpe_ratio", 0) or 0
            current_sharpe = (current.get("sharpe_ratio", 0) or 0) if current else 0

            if shadow_sharpe >= current_sharpe:
                # Promote shadow to production
                await self.ctx.db.promote_model(model_type, shadow["version"])
                if self.ctx.ml:
                    try:
                        await self.ctx.ml.load_model(model_type, shadow["version"])
                    except Exception as e:
                        logger.warning(
                            "Failed to load promoted model %s/%s: %s",
                            model_type, shadow["version"], e,
                        )
                promotions.append({
                    "model_type": model_type,
                    "version": shadow["version"],
                    "action": "promoted",
                    "shadow_sharpe": shadow_sharpe,
                    "previous_sharpe": current_sharpe,
                })
                logger.info(
                    "Promoted shadow model %s/%s (Sharpe: %.2f > %.2f)",
                    model_type, shadow["version"], shadow_sharpe, current_sharpe,
                )
            else:
                # Retire underperforming shadow
                await self.ctx.db.retire_model(model_type, shadow["version"])
                promotions.append({
                    "model_type": model_type,
                    "version": shadow["version"],
                    "action": "retired",
                    "shadow_sharpe": shadow_sharpe,
                    "production_sharpe": current_sharpe,
                })
                logger.info(
                    "Retired shadow model %s/%s (Sharpe: %.2f < %.2f)",
                    model_type, shadow["version"], shadow_sharpe, current_sharpe,
                )

        return promotions
