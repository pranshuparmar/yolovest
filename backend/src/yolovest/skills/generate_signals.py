"""Skill: generate-signals — ML-based trade signal generation.

Trigger: HEARTBEAT during market hours
Pipeline position: After market-scan, before risk-check.

Flow:
1. Load current watchlist from DB
2. For each watchlist stock, compute features
3. Run appropriate ML model
4. Generate signal with required fields
5. Filter: only emit signals where confidence >= per-direction threshold
   (risk.min_confidence_buy for BUY, risk.min_confidence_sell for SELL)
6. Emit signals as events for risk-check skill to consume
"""

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any
from yolovest.data.features import IndicatorConfig, compute_features
from yolovest.data.fno_features import FNO_FEATURE_KEYS, compute_fno_features
from yolovest.data.news_features import NEWS_FEATURE_KEYS, compute_news_features
from yolovest.data.vix_features import VIX_FEATURE_KEYS, compute_vix_features
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import IST, now_ist

logger = logging.getLogger(__name__)


def _format_class_probs(prediction: Any) -> str:
    """Render a prediction's per-class probability vector for logs.

    Falls back to single-confidence form when the model didn't expose
    its class_probabilities (older shadow models, defensive paths).
    """
    probs = getattr(prediction, "class_probabilities", None)
    if not probs:
        return f"confidence {getattr(prediction, 'confidence', 0):.2f}"
    # Stable ordering with BUY first so a "zero BUYs" pattern is
    # impossible to miss in a long log block.
    return " ".join(
        f"{label}={probs[label]:.2f}"
        for label in ("BUY", "HOLD", "SELL")
        if label in probs
    )


class GenerateSignalsSkill(SkillBase):
    name = "generate-signals"
    description = "Run ML models on watchlist to produce trade signals"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_market_hours())

    async def execute(self, **kwargs: Any) -> SkillResult:
        watchlist = await self.ctx.db.get_combined_watchlist()
        # Apply quarantine policy to watchlist entries:
        #   - Quarantined + replacement → rewrite the entry's symbol
        #     to the replacement (preserves its composite scores).
        #   - Quarantined + no replacement → drop the entry.
        #   - Active symbol → keep as-is.
        # Algorithmic watchlist is already quarantine-clean (market-scan reads
        # from get_nse_universe which excludes quarantined), but user_watchlist
        # entries pinned before quarantine can otherwise leak through here.
        repl = await self.ctx.db.get_quarantine_replacements()
        quarantined = await self.ctx.db.get_all_quarantined_symbol_set()
        if repl or quarantined:
            filtered: list[dict[str, Any]] = []
            seen: set[str] = set()
            for w in watchlist:
                sym = w["symbol"]
                if sym in quarantined:
                    target = repl.get(sym)
                    if not target:
                        continue  # drop
                    w = {**w, "symbol": target}
                    sym = target
                if sym in seen:
                    continue
                seen.add(sym)
                filtered.append(w)
            watchlist = filtered
        signals_generated = []
        risk_cfg = self.ctx.config.risk
        min_confidence_buy = risk_cfg.min_confidence_buy
        min_confidence_sell = risk_cfg.min_confidence_sell
        # Legacy threshold kept for diagnostics
        min_confidence = min(min_confidence_buy, min_confidence_sell)
        skip_sell_on_holdings = risk_cfg.skip_sell_on_holdings
        rotation_cfg = self.ctx.config.scanning
        # Tracks per-symbol signal productivity for watchlist rotation
        outcome_tracker: dict[str, bool] = {}

        # Diagnostics: track why stocks get filtered out
        filter_counts = {
            "insufficient_bars": 0,
            "feature_computation_failed": 0,
            "hold_signal": 0,
            "low_confidence": 0,
            "error": 0,
            "passed": 0,
        }
        rejection_details: list[dict[str, str]] = []

        if not watchlist:
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={"watchlist_size": 0, "signals_generated": 0, "signals": []},
            )

        # Check if ML is available
        if self.ctx.ml is None:
            logger.warning("No ML provider configured, skipping signal generation")
            return SkillResult(
                success=True,
                skill_name=self.name,
                data={
                    "watchlist_size": len(watchlist),
                    "signals_generated": 0,
                    "signals": [],
                    "reason": "no_ml",
                },
            )

        strategy_cfg = self.ctx.config.strategy
        indicator_cfg = IndicatorConfig(
            ema_periods=self.ctx.config.strategy.ema_periods,
            rsi=self.ctx.config.strategy.indicators.rsi,
            macd=self.ctx.config.strategy.indicators.macd,
            bollinger_bands=self.ctx.config.strategy.indicators.bollinger_bands,
            vwap=self.ctx.config.strategy.indicators.vwap,
            atr=self.ctx.config.strategy.indicators.atr,
            volume_profile=self.ctx.config.strategy.indicators.volume_profile,
            obv=self.ctx.config.strategy.indicators.obv,
            supertrend=self.ctx.config.strategy.indicators.supertrend,
        )

        # Build set of currently held symbols (open positions)
        # Used to decide if SELL = exit-owned-stock (CNC ok) vs short-sell (force MIS)
        open_positions = await self.ctx.db.get_open_positions(mode=self.ctx.config.mode)
        held_symbols = {p["symbol"] for p in open_positions}

        # Load locked symbols — SELL signals for these will be skipped entirely
        locked_symbols = await self.ctx.db.get_locked_symbols()

        # Skip symbols that already have a signal or open position today.
        # Risk-rejected symbols are intentionally re-evaluated each
        # heartbeat (capped) — most rejection reasons are transient.
        already_signaled = await self.ctx.db.get_todays_signaled_symbols(
            mode=self.ctx.config.mode,
            risk_rejected_retry_cap=self.ctx.config.risk.max_risk_rejected_retries_per_day,
        )
        if already_signaled:
            filter_counts["already_signaled"] = 0
            logger.info(
                "generate-signals: %d symbols blocked by already_signaled dedup: %s",
                len(already_signaled), sorted(already_signaled),
            )

        # Load symbol cooldown/repeat data
        cooldown_days = self.ctx.config.risk.symbol_cooldown_days
        repeat_lookback = self.ctx.config.risk.symbol_repeat_lookback_days
        repeat_min_conf = self.ctx.config.risk.symbol_repeat_min_confidence
        recently_traded: dict[str, str] = {}
        if repeat_lookback > 0:
            recently_traded = await self.ctx.db.get_recently_traded_symbols(
                repeat_lookback, mode=self.ctx.config.mode,
            )

        now = now_ist()

        # India VIX is a broadcast series — every symbol on this run gets
        # the same trailing-window value. Load once, before the per-symbol
        # loop. Empty result → neutral VIX features at compute time.
        vix_timeline: list[tuple[str, float]] = []
        try:
            vix_timeline = await self.ctx.db.get_vix_timeline(
                date_from=(now - timedelta(days=40)).strftime("%Y-%m-%d"),
            )
        except Exception:
            logger.debug("VIX timeline load failed; defaulting to neutral", exc_info=True)
        _today_str = now.strftime("%Y-%m-%d")
        if vix_timeline:
            _vix_feats_today = compute_vix_features(vix_timeline, _today_str)
        else:
            _vix_feats_today = {k: 0.0 for k in VIX_FEATURE_KEYS}

        # F&O option-chain timeline. Per-symbol lookup; misses → neutral.
        # Only the last 3 days are needed to derive today's oi_change_pct
        # and oi_buildup vs yesterday — keep the read window tight.
        fno_lookup: dict[str, list[tuple[str, dict[str, float]]]] = {}
        try:
            fno_lookup = await self.ctx.db.get_fno_timeline(
                date_from=(now - timedelta(days=5)).strftime("%Y-%m-%d"),
            )
        except Exception:
            logger.debug("F&O timeline load failed; defaulting to neutral", exc_info=True)

        for stock in watchlist:
            symbol = stock["symbol"]
            # NOTE: We intentionally do NOT setdefault False here. Only
            # paths where the ML model actually evaluated the symbol
            # and produced no actionable signal (HOLD, low-conf,
            # repeat-low-conf, SELL-on-holding) write to
            # outcome_tracker. Skip-for-technical-reason paths
            # (already_signaled, cooldown, locked, insufficient_bars,
            # feature_computation_failed, intraday_cutoff, error)
            # leave outcome_tracker untouched so they don't accumulate
            # toward the rotation cooldown threshold. Previously
            # everything skipped here counted as a miss, which
            # benched ~80% of nifty500 within a day.

            if symbol in already_signaled:
                filter_counts.setdefault("already_signaled", 0)
                filter_counts["already_signaled"] += 1
                rejection_details.append({
                    "symbol": symbol, "reason": "already_signaled",
                    "detail": "signal or open position exists today",
                })
                continue

            # Symbol cooldown: hard block if traded within cooldown_days
            # With smart re-entry enabled, allow re-entry under specific conditions
            reentry_cfg = self.ctx.config.risk.reentry
            is_reentry = False
            if symbol in recently_traded and cooldown_days > 0:
                last_trade_str = recently_traded[symbol]
                try:
                    last_trade_dt = datetime.fromisoformat(last_trade_str)
                    if last_trade_dt.tzinfo is None:
                        last_trade_dt = last_trade_dt.replace(tzinfo=IST)
                    days_since = (now - last_trade_dt).days
                    if days_since < cooldown_days:
                        # Check if smart re-entry can override the cooldown
                        reentry_allowed = False
                        if reentry_cfg.enabled:
                            reentry_allowed = await self._check_reentry_conditions(
                                symbol, reentry_cfg,
                            )
                        if reentry_allowed:
                            is_reentry = True
                            logger.info(
                                "Re-entry allowed for %s: traded %dd ago (cooldown=%dd), conditions met",
                                symbol, days_since, cooldown_days,
                            )
                        else:
                            filter_counts.setdefault("cooldown", 0)
                            filter_counts["cooldown"] += 1
                            rejection_details.append({
                                "symbol": symbol, "reason": "cooldown",
                                "detail": f"traded {days_since}d ago, cooldown={cooldown_days}d",
                            })
                            logger.info(
                                "Cooldown for %s: traded %dd ago (cooldown=%dd)",
                                symbol, days_since, cooldown_days,
                            )
                            continue
                except (ValueError, TypeError):
                    logger.debug("Cooldown check parse error for %s", symbol, exc_info=True)

            try:
                # Step 2: Fetch OHLCV and compute features
                # Always use daily bars for feature computation — intraday bars
                # are too few for long-window indicators (EMA-50/200, MACD etc.)
                # which causes feature shape mismatches with the trained model.
                daily_bars = await self.ctx.db.get_ohlcv(symbol, "daily", days=365)

                if len(daily_bars) < 50:
                    filter_counts["insufficient_bars"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "insufficient_bars",
                        "detail": f"{len(daily_bars)} bars < 50 required",
                    })
                    logger.info("Insufficient daily data for %s (%d bars)", symbol, len(daily_bars))
                    continue

                # Staleness gate. If the latest persisted daily bar is
                # more than `max_signal_data_age_trading_days` trading
                # sessions behind the most recent completed session,
                # signals built on this data would be reasoning about
                # stale market state — reject so a delisted / fetch-
                # broken symbol can't produce stale signals every
                # heartbeat.
                latest_bar_date = daily_bars[-1].timestamp.date()
                expected_freshest = self.ctx.market_hours.most_recent_completed_trading_day(now)
                missing = self.ctx.market_hours.trading_days_missing_after(
                    latest_bar_date, expected_freshest,
                )
                max_age = self.ctx.config.market_data.max_signal_data_age_trading_days
                if missing > max_age:
                    filter_counts.setdefault("stale_data", 0)
                    filter_counts["stale_data"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "stale_data",
                        "detail": (
                            f"latest bar {latest_bar_date} is {missing} trading "
                            f"days behind {expected_freshest} (max {max_age})"
                        ),
                    })
                    logger.info(
                        "Skipping %s — latest bar %s is %d trading days "
                        "behind %s (threshold %d)",
                        symbol, latest_bar_date, missing, expected_freshest, max_age,
                    )
                    continue

                features = compute_features(daily_bars, indicator_cfg)
                if not features:
                    filter_counts["feature_computation_failed"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "feature_computation_failed",
                        "detail": "compute_features returned empty",
                    })
                    logger.info("Feature computation failed for %s", symbol)
                    continue

                # Merge live news-sentiment features. Pulls the symbol's
                # headlines from the last 7 days; compute_news_features
                # bucketises into 24h / 7d windows. Failures are silently
                # neutralised — model is robust to NEWS_FEATURE_KEYS=0.
                try:
                    news_from = (now - timedelta(days=7)).isoformat()
                    news_rows = await self.ctx.db.get_news_articles(
                        symbol=symbol, date_from=news_from, limit=500,
                    )
                    headlines: list[tuple[str, datetime]] = []
                    for row in news_rows:
                        pub_raw = row.get("published_at")
                        if not pub_raw:
                            continue
                        try:
                            dt = datetime.fromisoformat(pub_raw)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=IST)
                        except (ValueError, TypeError):
                            continue
                        headlines.append((row.get("headline", ""), dt))
                    features.update(compute_news_features(headlines, now))
                except Exception:
                    logger.debug("News-feature merge failed for %s", symbol, exc_info=True)
                    features.update({k: 0.0 for k in NEWS_FEATURE_KEYS})

                # India VIX regime features. Same dict for every symbol
                # on this run — the timeline was loaded once above.
                features.update(_vix_feats_today)

                # F&O derivatives features. Only F&O-eligible names have
                # a row; misses return is_fno_stock=0 + others 0. Use the
                # last two equity closes from the daily_bars window to
                # drive the oi_buildup classification.
                _sym_fno = fno_lookup.get(symbol)
                if _sym_fno:
                    _prior_close = (
                        daily_bars[-2].close if len(daily_bars) >= 2 else None
                    )
                    _current_close = daily_bars[-1].close
                    features.update(compute_fno_features(
                        _sym_fno, _today_str,
                        prior_stock_close=_prior_close,
                        current_stock_close=_current_close,
                    ))
                else:
                    features.update({k: 0.0 for k in FNO_FEATURE_KEYS})

                # Fetch fresh LTP for accurate entry/target/SL pricing
                current_price: float | None = None
                try:
                    current_price = await self.ctx.market_data.get_ltp(symbol)
                except Exception:
                    logger.debug("LTP unavailable for %s, falling back to bar close", symbol)

                # Decide holding period based on stock characteristics and strategy mode
                holding_period, product, expected_days = await self._decide_holding_period(
                    features, existing_positions=open_positions,
                )

                # Hard ATR-eligibility cap for intraday: a stock with daily
                # ATR > volatility.max_atr_pct_for_intraday_eligibility (5%
                # default) is too volatile to reliably square off in a
                # half-day session. `_is_intraday_viable` already enforces
                # this in balanced/dynamic mode (routing to swing); this
                # block covers the pure intraday strategy mode where the
                # `max_days == 0` shortcut bypasses that check.
                if holding_period == "intraday":
                    elig_cap = float(getattr(
                        self.ctx.config.strategy.volatility,
                        "max_atr_pct_for_intraday_eligibility",
                        0.0,
                    ))
                    sym_atr_pct = float(features.get("atr_pct", 0.0))
                    if elig_cap > 0 and sym_atr_pct > elig_cap:
                        filter_counts.setdefault(
                            "intraday_atr_ineligible", 0,
                        )
                        filter_counts["intraday_atr_ineligible"] += 1
                        rejection_details.append({
                            "symbol": symbol,
                            "reason": "intraday_atr_ineligible",
                            "detail": (
                                f"atr_pct {sym_atr_pct * 100:.2f}% > "
                                f"intraday eligibility cap "
                                f"{elig_cap * 100:.2f}% — stock too volatile "
                                f"to square off in a half-day session"
                            ),
                        })
                        logger.info(
                            "Intraday ATR ineligible: %s atr_pct=%.2f%% > "
                            "cap %.2f%% — skipping",
                            symbol, sym_atr_pct * 100, elig_cap * 100,
                        )
                        outcome_tracker[symbol] = False
                        continue

                use_intraday = holding_period == "intraday"
                is_balanced = self.ctx.config.strategy.mode == "balanced"

                # Use latest intraday price for feature close during market hours
                intraday_features = None
                if use_intraday or is_balanced:
                    intraday_bars = await self.ctx.db.get_ohlcv(symbol, "5minute", days=1)
                    if intraday_bars:
                        intraday_features = {**features, "close": intraday_bars[-1].close}

                # Step 3: Run ML model(s)
                if is_balanced:
                    # Balanced mode: run BOTH models, pick higher confidence
                    prediction, holding_period, product, expected_days = (
                        await self._predict_balanced(
                            symbol, features, intraday_features,
                            current_price, holding_period, product, expected_days,
                        )
                    )
                    use_intraday = holding_period == "intraday"
                elif use_intraday:
                    if intraday_features:
                        features = intraday_features
                    prediction = await self.ctx.ml.predict_intraday(
                        symbol, features, current_price=current_price,
                    )
                else:
                    prediction = await self.ctx.ml.predict_swing(
                        symbol, features, current_price=current_price,
                    )

                # Shadow inference (non-blocking, best-effort)
                model_type = "intraday" if use_intraday else "swing"
                if self.ctx.ml.has_shadow(model_type):
                    try:
                        if use_intraday:
                            shadow_pred = await self.ctx.ml.predict_shadow_intraday(
                                symbol, features, current_price=current_price,
                            )
                        else:
                            shadow_pred = await self.ctx.ml.predict_shadow_swing(
                                symbol, features, current_price=current_price,
                            )
                        if shadow_pred is not None:
                            await self.ctx.db.insert_shadow_prediction({
                                "symbol": symbol,
                                "predicted_direction": shadow_pred.signal_type,
                                "confidence": shadow_pred.confidence,
                                "predicted_target": shadow_pred.target_price,
                                "predicted_stop_loss": shadow_pred.stop_loss_price,
                                "expected_holding_period": shadow_pred.holding_period,
                                "model_version": shadow_pred.model_version,
                                "entry_price": shadow_pred.entry_price,
                                "mode": self.ctx.config.mode,
                            })
                    except Exception as e:
                        logger.debug("Shadow inference failed for %s: %s", symbol, e)

                # Step 4: Skip HOLD signals
                if prediction.signal_type == "HOLD":
                    filter_counts["hold_signal"] += 1
                    outcome_tracker[symbol] = False
                    rejection_details.append({
                        "symbol": symbol, "reason": "hold_signal",
                        "detail": f"HOLD @ confidence {prediction.confidence:.2f}",
                    })
                    logger.info(
                        "HOLD signal for %s (%s)",
                        symbol,
                        _format_class_probs(prediction),
                    )
                    continue

                # Skip SELL signals for locked holdings (user explicitly protected them)
                if prediction.signal_type == "SELL" and symbol in locked_symbols:
                    filter_counts.setdefault("locked_holding", 0)
                    filter_counts["locked_holding"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "locked_holding",
                        "detail": f"SELL blocked — {symbol} is locked",
                    })
                    logger.info("Locked holding: skipping SELL for %s", symbol)
                    continue

                # Skip SELL signals for any held symbol — position-monitor owns exits.
                # Prevents SELL spam on holdings and lets SL/target/trailing do their job.
                if (
                    skip_sell_on_holdings
                    and prediction.signal_type == "SELL"
                    and symbol in held_symbols
                ):
                    filter_counts.setdefault("sell_on_holding", 0)
                    filter_counts["sell_on_holding"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "sell_on_holding",
                        "detail": f"SELL skipped — position-monitor handles exit for {symbol}",
                    })
                    logger.info("Held symbol: skipping SELL for %s (monitor owns exits)", symbol)
                    outcome_tracker[symbol] = False
                    continue

                # Adjust SELL signals: force to MIS/intraday if user doesn't
                # hold the stock. Dropped when the per-symbol decision is
                # swing (long_term / short_term modes, or balanced mode where
                # the swing model won) — converting a swing-horizon SELL into
                # an intraday MIS short would mix geometries.
                from yolovest.strategy.holding_period import adjust_sell_for_holdings

                adjusted = adjust_sell_for_holdings(
                    prediction.signal_type, holding_period, product,
                    symbol, held_symbols, expected_days,
                )
                if adjusted is None:
                    filter_counts.setdefault("short_on_swing_horizon", 0)
                    filter_counts["short_on_swing_horizon"] += 1
                    rejection_details.append({
                        "symbol": symbol, "reason": "short_on_swing_horizon",
                        "detail": (
                            f"SELL on non-held {symbol} with "
                            f"holding_period='{holding_period}' would require "
                            f"intraday/MIS — dropped (only intraday-decided "
                            f"SELLs are eligible for retail shorting)"
                        ),
                    })
                    logger.info(
                        "Skipping SELL for non-held %s — would mix swing "
                        "geometry (%s, %dd) with intraday MIS short",
                        symbol, holding_period, expected_days,
                    )
                    outcome_tracker[symbol] = False
                    continue
                holding_period, product, expected_days = adjusted

                # Intraday cutoff: skip intraday signals after configured time
                if holding_period == "intraday":
                    cutoff_str = self.ctx.config.market_hours.intraday_cutoff
                    cutoff_parts = cutoff_str.split(":")
                    cutoff_time = time(int(cutoff_parts[0]), int(cutoff_parts[1]))
                    if datetime.now(IST).time() >= cutoff_time:
                        filter_counts.setdefault("intraday_cutoff", 0)
                        filter_counts["intraday_cutoff"] += 1
                        rejection_details.append({
                            "symbol": symbol, "reason": "intraday_cutoff",
                            "detail": f"intraday signal after {cutoff_str} cutoff",
                        })
                        logger.info(
                            "Intraday cutoff: skipping %s %s (after %s)",
                            prediction.signal_type, symbol, cutoff_str,
                        )
                        continue

                # Override target/SL with ATR multipliers interpolated for holding duration
                from yolovest.strategy.holding_period import interpolate_atr_multipliers

                entry = prediction.entry_price
                atr = features.get("atr_14", entry * 0.02)
                # Clamp the ATR used for intraday geometry. A high-ATR
                # stock (e.g. JAINREC at ~11.6% daily ATR) would otherwise
                # get a 6-7% intraday target with the default 0.6×
                # multiplier — unreachable in a half-day. Capping at 3.5%
                # (default) limits the implied target distance to ~2.1%
                # while leaving median large-caps (1-2% ATR) untouched.
                if holding_period == "intraday":
                    max_atr_pct = float(
                        self.ctx.config.strategy.holding_periods.intraday
                            .max_atr_pct_for_target
                    )
                    if max_atr_pct > 0:
                        atr_cap = entry * max_atr_pct
                        if atr > atr_cap:
                            logger.info(
                                "Clamping intraday ATR for %s: %.2f → "
                                "%.2f (entry=%.2f, max_atr_pct=%.3f)",
                                symbol, atr, atr_cap, entry, max_atr_pct,
                            )
                            atr = atr_cap
                target_mult, sl_mult = interpolate_atr_multipliers(
                    expected_days, self.ctx.config.strategy.holding_periods,
                )

                if prediction.signal_type == "BUY":
                    target_price = entry + target_mult * atr
                    stop_loss_price = entry - sl_mult * atr
                elif prediction.signal_type == "SELL":
                    target_price = entry - target_mult * atr
                    stop_loss_price = entry + sl_mult * atr
                else:
                    target_price = prediction.target_price
                    stop_loss_price = prediction.stop_loss_price

                target_price = max(target_price, 0.01)
                stop_loss_price = max(stop_loss_price, 0.01)

                # Reality-check intraday targets against today's session.
                # ATR-based targets can sit beyond today's high (or below
                # today's low for SELL), which means the trade needs a
                # new session extreme to win. When the paid quote feed is
                # active, fetch today's high/low/circuit and cap the
                # target accordingly. Only applies to intraday holds;
                # multi-day swing/CNC targets legitimately extend past
                # today's range.
                if (
                    holding_period == "intraday"
                    and self.ctx.config.market_data.kite_data_enabled
                ):
                    target_price, stop_loss_price = await self._reality_check_intraday(
                        symbol,
                        prediction.signal_type,
                        target_price,
                        stop_loss_price,
                    )

                signal = {
                    "symbol": symbol,
                    "signal_type": prediction.signal_type,
                    "entry_price": entry,
                    "target_price": round(target_price, 2),
                    "stop_loss_price": round(stop_loss_price, 2),
                    "position_size": prediction.position_size,
                    "expected_holding_period": holding_period,
                    "expected_holding_days": expected_days,
                    "product": product,
                    "confidence_score": prediction.confidence,
                    "features_snapshot": features,
                    "model_version": prediction.model_version,
                    "attribution": (
                        [
                            {
                                "feature": a.feature,
                                "value": a.value,
                                "contribution": a.contribution,
                            }
                            for a in prediction.attribution
                        ]
                        if prediction.attribution else None
                    ),
                }

                if is_reentry:
                    signal["reentry"] = True

                # Step 5: Confidence filter (asymmetric per signal type
                # AND per holding bucket — intraday and swing have very
                # different signal characteristics so a single floor
                # rarely fits both well).
                base_threshold = risk_cfg.resolve_min_confidence(
                    holding_period, prediction.signal_type,
                )
                effective_min = base_threshold
                is_repeat = symbol in recently_traded
                if is_repeat and repeat_lookback > 0:
                    effective_min = max(base_threshold, repeat_min_conf)

                if signal["confidence_score"] >= effective_min:
                    # Re-entry: require higher confidence than original trade
                    if is_reentry and reentry_cfg.require_higher_confidence:
                        orig_conf = await self._get_last_trade_confidence(symbol)
                        if orig_conf is not None and prediction.confidence <= orig_conf:
                            filter_counts.setdefault("reentry_low_confidence", 0)
                            filter_counts["reentry_low_confidence"] += 1
                            rejection_details.append({
                                "symbol": symbol, "reason": "reentry_low_confidence",
                                "detail": (
                                    f"re-entry {prediction.signal_type} @ {prediction.confidence:.2f} "
                                    f"<= original {orig_conf:.2f}"
                                ),
                            })
                            logger.info(
                                "Re-entry blocked for %s: confidence %.2f <= original %.2f",
                                symbol, prediction.confidence, orig_conf,
                            )
                            continue

                    filter_counts["passed"] += 1
                    outcome_tracker[symbol] = True
                    signal.setdefault("mode", self.ctx.config.mode)
                    signal_id = await self.ctx.db.insert_signal(signal)
                    if signal_id:
                        # Carry the row id forward so trade-execute can
                        # write it on the trade and the UNIQUE index
                        # rejects a duplicate execution of the same
                        # signal under restart / retry races.
                        signal["signal_id"] = signal_id
                    signals_generated.append(signal)
                    logger.info(
                        "PASSED %s for %s @ %.2f (%s)",
                        prediction.signal_type, symbol, prediction.confidence,
                        _format_class_probs(prediction),
                    )
                    # Subscribe the new symbol to KiteTicker immediately
                    # so the dashboard's RecommendationsPanel / Pending /
                    # Positions widgets show live LTP without waiting
                    # for the next heartbeat (position-monitor's
                    # subscribe pass). Idempotent inside the ticker.
                    ticker = getattr(self.ctx, "ticker", None)
                    if ticker is not None:
                        try:
                            await ticker.subscribe([symbol])
                        except Exception:
                            logger.debug(
                                "ticker subscribe failed for %s", symbol,
                                exc_info=True,
                            )
                    await self.broadcast("signal_generated", {
                        "symbol": symbol,
                        "signal_type": prediction.signal_type,
                        "confidence": prediction.confidence,
                        "entry_price": prediction.entry_price,
                    })
                elif is_repeat and signal["confidence_score"] >= base_threshold:
                    # Would have passed normal threshold but blocked by repeat rule
                    filter_counts.setdefault("repeat_low_confidence", 0)
                    filter_counts["repeat_low_confidence"] += 1
                    outcome_tracker[symbol] = False
                    rejection_details.append({
                        "symbol": symbol, "reason": "repeat_low_confidence",
                        "detail": (
                            f"{prediction.signal_type} @ {prediction.confidence:.2f} "
                            f"< {effective_min} (repeat threshold, base={base_threshold})"
                        ),
                    })
                    logger.info(
                        "Repeat confidence filter for %s: %s @ %.2f < %.2f (repeat, base=%.2f)",
                        symbol, prediction.signal_type, prediction.confidence,
                        effective_min, base_threshold,
                    )
                else:
                    filter_counts["low_confidence"] += 1
                    outcome_tracker[symbol] = False
                    rejection_details.append({
                        "symbol": symbol, "reason": "low_confidence",
                        "detail": f"{prediction.signal_type} @ confidence {prediction.confidence:.2f} < {base_threshold}",
                    })
                    logger.info(
                        "Low confidence for %s: %s @ %.2f < %.2f (%s)",
                        symbol, prediction.signal_type, prediction.confidence, base_threshold,
                        _format_class_probs(prediction),
                    )

            except Exception as e:
                filter_counts["error"] += 1
                rejection_details.append({
                    "symbol": symbol, "reason": "error", "detail": str(e),
                })
                logger.warning("Signal generation failed for %s: %s", symbol, e)

        logger.info(
            "generate-signals: %d signals from %d watchlist stocks — %s",
            len(signals_generated), len(watchlist), filter_counts,
        )

        # Persist rotation outcomes so market-scan can cooldown stale symbols.
        if rotation_cfg.rotation_enabled and outcome_tracker:
            threshold = rotation_cfg.rotation_no_signal_threshold
            cooldown_hours = rotation_cfg.rotation_cooldown_hours
            for sym, produced in outcome_tracker.items():
                try:
                    await self.ctx.db.record_signal_outcome(
                        sym, produced,
                        threshold=threshold, cooldown_hours=cooldown_hours,
                    )
                except Exception:
                    logger.exception("Failed to record signal outcome for %s", sym)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "watchlist_size": len(watchlist),
                "signals_generated": len(signals_generated),
                "signals": signals_generated,  # full signal dicts for downstream skills
                "diagnostics": {
                    "min_confidence_threshold": min_confidence,  # legacy (min of buy/sell)
                    "min_confidence_buy": min_confidence_buy,
                    "min_confidence_sell": min_confidence_sell,
                    "filter_counts": filter_counts,
                    "rejection_details": rejection_details,
                },
            },
        )

    async def _reality_check_intraday(
        self,
        symbol: str,
        signal_type: str,
        target: float,
        stop_loss: float,
    ) -> tuple[float, float]:
        """Cap intraday target/SL against the exchange's circuit limits
        using a live quote from the paid Kite feed.

        Failure is non-fatal — if the quote fetch errors, the original
        target/SL are returned unchanged.
        """
        from yolovest.strategy.holding_period import apply_session_caps

        try:
            quote = await self.ctx.market_data.get_quote(symbol)
        except Exception:
            logger.debug("reality-check: quote fetch failed for %s", symbol, exc_info=True)
            return target, stop_loss

        new_target, new_sl, adjustments = apply_session_caps(
            signal_type, target, stop_loss, quote,
        )
        if adjustments:
            logger.info(
                "reality-check %s %s: %s",
                signal_type, symbol, "; ".join(adjustments),
            )
        return new_target, new_sl

    async def _predict_balanced(
        self,
        symbol: str,
        daily_features: dict,
        intraday_features: dict | None,
        current_price: float | None,
        fallback_period: str,
        fallback_product: str,
        fallback_days: int,
    ) -> tuple[Any, str, str, int]:
        """Balanced mode: run both intraday and swing models, pick higher confidence.

        Returns (prediction, holding_period, product, expected_days).
        """
        from yolovest.config import _MODE_HOLDING_DAYS
        from yolovest.strategy.holding_period import decide_holding_period

        # Check intraday cutoff — if past cutoff, skip intraday model entirely
        cutoff_str = self.ctx.config.market_hours.intraday_cutoff
        cutoff_parts = cutoff_str.split(":")
        past_intraday_cutoff = datetime.now(IST).time() >= time(int(cutoff_parts[0]), int(cutoff_parts[1]))

        if past_intraday_cutoff:
            # Only run swing model
            try:
                swing_pred = await self.ctx.ml.predict_swing(symbol, daily_features, current_price=current_price)
            except Exception as e:
                logger.debug("Balanced: swing model failed for %s: %s", symbol, e)
                swing_pred = None
            if swing_pred and swing_pred.signal_type != "HOLD":
                mode_days = _MODE_HOLDING_DAYS.get("balanced", (0, 15))
                vol_cfg = self.ctx.config.strategy.volatility
                now_time = datetime.now(IST).time()
                _, product, expected_days = decide_holding_period(
                    daily_features, ["short_term", "long_term"], vol_cfg, now_time,
                    mode_days_range=(max(1, mode_days[0]), mode_days[1]),
                )
                label = "swing" if expected_days <= 5 else "positional" if expected_days <= 15 else "long_term"
                return swing_pred, label, "CNC", expected_days
            return swing_pred or intraday_features, fallback_period, fallback_product, fallback_days

        # Run both models concurrently
        intra_feat = intraday_features or daily_features
        intra_pred, swing_pred = await asyncio.gather(
            self.ctx.ml.predict_intraday(symbol, intra_feat, current_price=current_price),
            self.ctx.ml.predict_swing(symbol, daily_features, current_price=current_price),
            return_exceptions=True,
        )

        # Resolve exceptions to None
        if isinstance(intra_pred, BaseException):
            logger.debug("Balanced: intraday model failed for %s: %s", symbol, intra_pred)
            intra_pred = None
        if isinstance(swing_pred, BaseException):
            logger.debug("Balanced: swing model failed for %s: %s", symbol, swing_pred)
            swing_pred = None

        # Filter out HOLD signals (confidence is meaningless for HOLD).
        # Compare margin-above-threshold (not raw confidence): the
        # intraday model is trained on balanced labels (~29/43/28)
        # while swing is HOLD-dominated (~7/87/6), so raw confidence
        # is not comparable across them. Margin = how far above the
        # model's effective gating threshold the prediction sits,
        # which normalises for the per-model calibration.
        def _margin(pred: Any, model_type: str) -> float:
            if pred is None or pred.signal_type == "HOLD":
                return -1.0
            thresholds = self.ctx.ml.get_effective_thresholds(model_type)
            if not thresholds:
                # Legacy model without tuned thresholds — fall back to
                # raw confidence (matches the pre-tuning behaviour).
                return float(pred.confidence)
            key = pred.signal_type.lower()  # "buy" / "sell"
            threshold = float(thresholds.get(key, 0.5))
            return float(pred.confidence) - threshold

        intra_margin = _margin(intra_pred, "intraday")
        swing_margin = _margin(swing_pred, "swing")

        if intra_margin < 0 and swing_margin < 0:
            # Both HOLD or both failed — return the swing HOLD (or intraday if no swing)
            prediction = swing_pred or intra_pred
            return prediction, fallback_period, fallback_product, fallback_days

        if intra_margin >= swing_margin:
            # Intraday wins
            logger.debug(
                "Balanced %s: intraday wins (margin=%.3f %s @ %.2f) vs swing "
                "(margin=%.3f %s @ %.2f)",
                symbol, intra_margin,
                intra_pred.signal_type if intra_pred else "N/A",
                intra_pred.confidence if intra_pred else 0,
                swing_margin,
                swing_pred.signal_type if swing_pred else "N/A",
                swing_pred.confidence if swing_pred else 0,
            )
            return intra_pred, "intraday", "MIS", 0
        else:
            # Swing wins — compute proper holding days
            mode_days = _MODE_HOLDING_DAYS.get("balanced", (0, 15))
            vol_cfg = self.ctx.config.strategy.volatility
            now_time = datetime.now(IST).time()
            _, product, expected_days = decide_holding_period(
                daily_features,
                ["short_term", "long_term"],  # exclude intraday since swing won
                vol_cfg,
                now_time,
                mode_days_range=(max(1, mode_days[0]), mode_days[1]),
            )
            label = "swing" if expected_days <= 5 else "positional" if expected_days <= 15 else "long_term"
            logger.debug(
                "Balanced %s: swing wins (margin=%.3f %s @ %.2f) vs intraday "
                "(margin=%.3f %s @ %.2f) — %s (%dd)",
                symbol, swing_margin, swing_pred.signal_type,
                swing_pred.confidence if swing_pred else 0,
                intra_margin,
                intra_pred.signal_type if intra_pred else "N/A",
                intra_pred.confidence if intra_pred else 0,
                label, expected_days,
            )
            return swing_pred, label, "CNC", expected_days

    async def _decide_holding_period(
        self, features: dict, existing_positions: list[dict] | None = None,
    ) -> tuple[str, str, int]:
        """Decide holding period, product type, and expected days based on stock characteristics."""
        from yolovest.config import _MODE_HOLDING_DAYS
        from yolovest.strategy.holding_period import decide_holding_period

        allowed = self.ctx.config.strategy.allowed_holding_periods or ["intraday", "short_term", "long_term"]
        mode = self.ctx.config.strategy.mode
        mode_days = _MODE_HOLDING_DAYS.get(mode)
        now_time = datetime.now(IST).time()
        vol_cfg = self.ctx.config.strategy.volatility

        # Read market regime (persisted by market-scan skill)
        regime_cfg = self.ctx.config.strategy.market_regime
        regime = None
        bear_max = None
        if regime_cfg.enabled:
            regime = await self.ctx.db.get_system_state("market_regime")
            bear_max = regime_cfg.bear_max_holding_days

        return decide_holding_period(
            features, allowed, vol_cfg, now_time,
            mode_days_range=mode_days,
            existing_positions=existing_positions,
            market_regime=regime,
            bear_max_holding_days=bear_max,
        )

    async def _check_reentry_conditions(
        self, symbol: str, reentry_cfg: Any,
    ) -> bool:
        """Check whether smart re-entry conditions are met for a symbol in cooldown.

        Evaluates:
        1. min_bars_after_exit: enough bars have passed since the last trade closed
        2. min_price_move_pct: price has moved sufficiently from the exit price
        3. max_reentries_per_symbol: haven't exceeded re-entry limit for today

        Returns True if all conditions are met and re-entry should be allowed.
        """
        try:
            # Get last closed trade for this symbol
            trades = await self.ctx.db.get_symbol_trades(symbol, limit=5)
            closed_trades = [
                t for t in trades
                if t.get("closed_at") is not None and t.get("status") in ("closed", "filled", "squared_off")
            ]
            if not closed_trades:
                return False

            last_trade = closed_trades[0]  # most recent closed trade

            # Condition 1: min_bars_after_exit
            exit_date_str = last_trade.get("closed_at")
            if not exit_date_str:
                return False

            exit_dt = datetime.fromisoformat(exit_date_str)
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=IST)

            # Count bars since exit using OHLCV data
            bars = await self.ctx.db.get_ohlcv(symbol, "daily", days=reentry_cfg.min_bars_after_exit + 5)
            # Different providers (jugaad / yfinance / tvdatafeed / kite)
            # store OHLCV timestamps with mixed tz state — some naive,
            # some aware. exit_dt is always-aware now, so normalize each
            # bar before the > comparison to avoid TypeError.
            bars_after_exit = sum(
                1 for bar in bars
                if (
                    bar.timestamp.replace(tzinfo=IST)
                    if bar.timestamp.tzinfo is None
                    else bar.timestamp
                ) > exit_dt
            )
            if bars_after_exit < reentry_cfg.min_bars_after_exit:
                logger.debug(
                    "Re-entry blocked for %s: only %d bars after exit (need %d)",
                    symbol, bars_after_exit, reentry_cfg.min_bars_after_exit,
                )
                return False

            # Condition 2: min_price_move_pct
            exit_price = last_trade.get("fill_price") or last_trade.get("entry_price")
            if not exit_price or exit_price <= 0:
                return False

            try:
                current_price = await self.ctx.market_data.get_ltp(symbol)
            except Exception:
                logger.debug("LTP unavailable for %s re-entry check, using bar close", symbol)
                # Fall back to last bar close
                if bars:
                    current_price = bars[-1].close
                else:
                    return False

            price_move_pct = abs(current_price - exit_price) / exit_price
            if price_move_pct < reentry_cfg.min_price_move_pct:
                logger.debug(
                    "Re-entry blocked for %s: price move %.2f%% < %.2f%% required",
                    symbol, price_move_pct * 100, reentry_cfg.min_price_move_pct * 100,
                )
                return False

            # Condition 3: max_reentries_per_symbol
            todays_trades = await self.ctx.db.get_todays_trades()
            symbol_trades_today = sum(
                1 for t in todays_trades if t.get("symbol") == symbol
            )
            if symbol_trades_today >= reentry_cfg.max_reentries_per_symbol:
                logger.debug(
                    "Re-entry blocked for %s: %d trades today >= max %d",
                    symbol, symbol_trades_today, reentry_cfg.max_reentries_per_symbol,
                )
                return False

            # Note: require_higher_confidence is checked downstream during
            # the confidence filter, since we don't have the new signal's
            # confidence yet at this point. We store the original confidence
            # for comparison later.

            logger.info(
                "Re-entry conditions met for %s: bars_after_exit=%d, price_move=%.2f%%, "
                "today_trades=%d",
                symbol, bars_after_exit, price_move_pct * 100, symbol_trades_today,
            )
            return True

        except Exception as e:
            logger.warning("Re-entry condition check failed for %s: %s", symbol, e)
            return False

    async def _get_last_trade_confidence(self, symbol: str) -> float | None:
        """Get the confidence score of the last closed trade for a symbol.

        Used by the smart re-entry feature to enforce require_higher_confidence.
        Returns None if no trade is found or confidence is unavailable.
        """
        try:
            trades = await self.ctx.db.get_symbol_trades(symbol, limit=5)
            for t in trades:
                if t.get("closed_at") is not None:
                    conf = t.get("confidence_score")
                    if conf is not None:
                        return float(conf)
            return None
        except Exception:
            logger.debug("Failed to get last trade confidence for %s", symbol, exc_info=True)
            return None
