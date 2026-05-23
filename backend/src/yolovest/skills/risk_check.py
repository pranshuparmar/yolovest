"""Skill: risk-check — Validate signals against all configurable risk rules.

Trigger: EVENT — called for each signal from generate-signals
Pipeline position: After generate-signals, before llm-review.

Flow:
1. Load current portfolio state (positions, daily PnL, weekly PnL)
2. Check kill switch state — reject all if paused
3. Check market hours enforcement
4. Check daily loss circuit breaker — stop if exceeded
5. Check weekly loss circuit breaker — reduce sizing if exceeded
6. Check max trades per day
7. Check loss cooldown
8. Check max open positions
9. Check max portfolio exposure
10. Check max single stock exposure
11. Check sector correlation
12. Validate stop-loss is present
13. Compute position size based on max risk per trade
14. Apply weekly sizing reduction if breaker active
15. Return: approved (with adjusted size) or rejected (with reason)

All thresholds read from config.risk.* — zero hardcoded values.
"""

import logging
from datetime import datetime
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class RiskCheckSkill(SkillBase):
    name = "risk-check"
    description = "Validate trade signal against all risk rules"
    trigger = SkillTrigger.EVENT
    schedule = None

    # Cache live-regime per heartbeat — recomputing it for each
    # candidate signal would do the same cross-sectional scan
    # multiple times. 5-min TTL is fine (heartbeats are 15-min).
    _regime_ttl_sec: float = 300.0

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._regime: dict[str, float] | None = None
        self._regime_at: float = 0.0

    def should_run(self) -> bool:
        return True  # Always available — gating is per-signal

    async def _get_live_regime(self) -> dict[str, float]:
        import time as _time
        now = _time.monotonic()
        if (
            self._regime is not None
            and (now - self._regime_at) < self._regime_ttl_sec
        ):
            return self._regime
        try:
            self._regime = await self.ctx.db.compute_live_regime()
        except Exception:
            logger.debug("compute_live_regime failed", exc_info=True)
            self._regime = {"breadth": 0.5, "avg_return": 0.0, "sample_size": 0}
        self._regime_at = now
        return self._regime

    async def execute(self, **kwargs: Any) -> SkillResult:
        signal = kwargs["signal"]
        cfg = self.ctx.config.risk
        portfolio = await self.ctx.db.get_portfolio_state(
            weekly_reset_day=cfg.weekly_reset_day,
            mode=self.ctx.config.mode,
        )

        # Kill switch
        if cfg.kill_switch_enabled and await self.ctx.db.is_kill_switch_active():
            return self._reject(signal, "Kill switch is active")

        # Market hours
        if not self.ctx.market_hours.is_order_window():
            return self._defer(signal, "Outside order window")

        # Early close day — block new MIS positions if close to square-off
        if (self.ctx.market_hours.is_early_close_day()
                and signal.get("product", "MIS") == "MIS"):
            from yolovest.timezone import now_ist

            now = now_ist()
            sq_time = self.ctx.market_hours.get_square_off_time(now.date())
            minutes_to_sq = (
                datetime.combine(now.date(), sq_time) - now.replace(tzinfo=None)
            ).total_seconds() / 60
            # Block new MIS if less than 30 min to early square-off
            if minutes_to_sq < 30:
                return self._reject(
                    signal,
                    f"Early close day: only {minutes_to_sq:.0f}min to square-off",
                )

        # Daily circuit breaker
        if portfolio["daily_pnl_pct"] <= -cfg.daily_loss_limit_pct:
            return self._reject(signal, f"Daily loss limit hit ({cfg.daily_loss_limit_pct:.0%})")

        # Max open positions (system-generated trades only; adopted holdings
        # are pre-existing investments and don't count toward the trading limit).
        # Include pending approvals to prevent over-generation in manual mode.
        pending: list[dict[str, Any]] = []
        if self.ctx.config.execution.transaction_mode == "manual":
            try:
                pending = await self.ctx.db.get_pending_trades()
            except Exception:
                logger.debug("Failed to load pending trades", exc_info=True)
        pending_count = len(pending)

        # Max trades per day — count executed today PLUS pending awaiting
        # approval. Without this, a single heartbeat that emits multiple
        # signals can queue them all (each sees trades_today=0 because no
        # row has executed yet); the user then approves them and the
        # daily cap silently overshoots.
        effective_today = portfolio["trades_today"] + pending_count
        if effective_today >= cfg.max_trades_per_day:
            return self._reject(
                signal,
                f"Max trades/day reached ({effective_today} = "
                f"{portfolio['trades_today']} executed + {pending_count} pending, "
                f"limit={cfg.max_trades_per_day})",
            )

        # Per-product daily cap (MIS vs CNC). Optional; when set,
        # acts on top of the combined cap so users can have e.g. 10
        # MIS entries per day but only 1 CNC.
        signal_product = (signal.get("product") or "MIS").upper()
        if signal_product == "MIS":
            product_limit = cfg.max_mis_trades_per_day
            product_executed = portfolio.get("mis_trades_today", 0)
        elif signal_product == "CNC":
            product_limit = cfg.max_cnc_trades_per_day
            product_executed = portfolio.get("cnc_trades_today", 0)
        else:
            product_limit = None
            product_executed = 0
        if product_limit is not None:
            product_pending = sum(
                1 for t in pending
                if (t.get("product") or "MIS").upper() == signal_product
            )
            effective_product_today = product_executed + product_pending
            if effective_product_today >= product_limit:
                return self._reject(
                    signal,
                    f"Max {signal_product} trades/day reached "
                    f"({effective_product_today} = {product_executed} executed "
                    f"+ {product_pending} pending, limit={product_limit})",
                )

        # Loss cooldown — portfolio-wide (any losing trade pauses everything)
        if portfolio["minutes_since_last_loss"] < cfg.loss_cooldown_minutes:
            remaining = cfg.loss_cooldown_minutes - portfolio["minutes_since_last_loss"]
            return self._reject(signal, f"Loss cooldown active ({remaining:.0f}min remaining)")

        # Loss cooldown — per-symbol. Without this, the intraday model can
        # re-signal the same symbol minutes after it SL'd, walking the user
        # straight back into the same losing setup before regime has changed.
        if cfg.loss_cooldown_minutes > 0:
            sym_loss_age = await self.ctx.db.minutes_since_last_loss_for_symbol(
                signal["symbol"], mode=self.ctx.config.mode,
            )
            if sym_loss_age < cfg.loss_cooldown_minutes:
                remaining = cfg.loss_cooldown_minutes - sym_loss_age
                return self._reject(
                    signal,
                    f"Symbol cooldown active ({signal['symbol']} lost "
                    f"{sym_loss_age:.0f}min ago, {remaining:.0f}min remaining)",
                )

        system_positions = portfolio.get("system_positions", portfolio["open_positions"])
        adopted_positions = portfolio.get("adopted_positions", 0)
        effective_positions = system_positions + pending_count
        if effective_positions >= cfg.max_open_positions:
            return self._reject(
                signal,
                f"Max open positions reached ({effective_positions} = "
                f"{system_positions} system + {pending_count} pending, "
                f"limit={cfg.max_open_positions}; {adopted_positions} adopted not counted)",
            )

        # Max portfolio exposure — include pending-trade notional in manual mode.
        # Without this, manual mode can queue a stack of pending trades whose
        # combined notional far exceeds the cap (open positions report 0%
        # until each pending trade is approved). Approving them all in one
        # batch would then exceed max_portfolio_exposure_pct.
        capital_for_exposure = portfolio["total_capital"]
        pending_notional = sum(
            float(t.get("entry_price") or 0) * float(t.get("position_size") or 0)
            for t in pending
        )
        pending_exposure_pct = (
            pending_notional / capital_for_exposure if capital_for_exposure > 0 else 0
        )
        effective_exposure_pct = portfolio["exposure_pct"] + pending_exposure_pct
        if effective_exposure_pct >= cfg.max_portfolio_exposure_pct:
            return self._reject(
                signal,
                f"Portfolio exposure limit ({effective_exposure_pct:.0%} = "
                f"{portfolio['exposure_pct']:.0%} open + "
                f"{pending_exposure_pct:.0%} pending, "
                f"cap={cfg.max_portfolio_exposure_pct:.0%})",
            )

        # Max single stock exposure
        stock_exposure = portfolio["stock_exposures"].get(signal["symbol"], 0)
        if stock_exposure >= cfg.max_single_stock_pct:
            return self._reject(signal, f"Single stock limit ({cfg.max_single_stock_pct:.0%})")

        # Sector correlation
        stock_sector = await self.ctx.db.get_stock_sector(signal["symbol"])
        sector_count = portfolio["sector_counts"].get(stock_sector, 0)
        if sector_count >= cfg.max_same_sector_positions:
            return self._reject(
                signal,
                f"Sector limit ({stock_sector}: {cfg.max_same_sector_positions})",
            )

        # Correlation-aware position limit (beyond simple sector counts)
        if cfg.correlation_limit.enabled:
            corr_rejection = await self._check_correlation_limit(
                signal, cfg.correlation_limit,
            )
            if corr_rejection:
                return self._reject(signal, corr_rejection)

        # Depth-imbalance gate — scale position size down when the live
        # order book opposes the signal. Only meaningful with the paid
        # Kite feed (jugaad/yfinance can't return depth qty).
        depth_size_multiplier = 1.0
        if (
            cfg.depth_gate.enabled
            and self.ctx.config.market_data.kite_data_enabled
        ):
            depth_size_multiplier = await self._check_depth_gate(signal, cfg.depth_gate)

        # Regime gate — refuse BUYs on broadly-red days, SELLs on
        # broadly-green days. Computed once per heartbeat via a
        # cross-sectional scan of today's vs yesterday's daily closes.
        regime_size_multiplier = 1.0
        if cfg.regime_gate.enabled:
            regime = await self._get_live_regime()
            regime_size_multiplier = self._apply_regime_gate(
                signal, regime, cfg.regime_gate,
            )
            if regime_size_multiplier == 0.0:
                return self._reject(
                    signal,
                    f"Regime gate: breadth={regime['breadth']:.2f} opposes "
                    f"{signal.get('signal_type', 'BUY')} "
                    f"(thresholds: BUY≥{cfg.regime_gate.min_breadth_for_buy}, "
                    f"SELL≤{cfg.regime_gate.max_breadth_for_sell})",
                )

        # Mandatory stop-loss
        if cfg.mandatory_stop_loss and not signal.get("stop_loss_price"):
            return self._reject(signal, "No stop-loss set (mandatory)")

        # Capital exhaustion — check if remaining cash can cover min trade
        capital = portfolio["total_capital"]
        available_cash = portfolio.get("available_cash", capital)
        min_trade_value = signal["entry_price"]  # at least 1 share
        if available_cash < min_trade_value:
            return self._reject(
                signal,
                "Capital exhaustion: "
                f"cash ₹{available_cash:,.0f} < min trade ₹{min_trade_value:,.0f}",
            )

        # Validate entry price against fresh LTP
        entry = signal["entry_price"]
        sl = signal["stop_loss_price"]
        drift_max = self.ctx.config.execution.price_drift_max_pct
        try:
            fresh_ltp = await self.ctx.market_data.get_ltp(signal["symbol"])
            drift_pct = abs(fresh_ltp - entry) / entry if entry > 0 else 0
            if drift_pct > drift_max:
                return self._reject(
                    signal,
                    f"Entry price drift too high: signal=₹{entry:.2f}, "
                    f"current=₹{fresh_ltp:.2f} ({drift_pct:.1%})",
                )
            if drift_pct > 0.005:
                logger.info(
                    "risk-check: price drift for %s: signal=%.2f, current=%.2f (%.1f%%)",
                    signal["symbol"], entry, fresh_ltp, drift_pct * 100,
                )
        except Exception:
            logger.debug("LTP unavailable for %s price drift check", signal["symbol"])

        # Position sizing based on max risk per trade
        risk_amount = capital * cfg.max_risk_per_trade_pct
        risk_per_share = abs(entry - sl)

        if risk_per_share <= 0:
            return self._reject(signal, "Invalid stop-loss (risk_per_share <= 0)")

        position_size = int(risk_amount / risk_per_share)
        # Capture base size for the cumulative audit log below. Every
        # gate that modifies position_size (slippage penalty, conviction,
        # regime, depth, institutional flow, confidence-scaled slot,
        # effective-risk clamp, margin shrink) effectively contributes
        # a multiplier off this base — the final log line shows the
        # net effect so "why was my size this number?" is a one-grep
        # diagnosis instead of a trace through six skills.
        base_position_size = position_size

        # Weekly circuit breaker — reduce sizing
        if portfolio["weekly_pnl_pct"] <= -cfg.weekly_loss_limit_pct:
            position_size = int(position_size * cfg.weekly_loss_sizing_reduction)

        # Cap by single stock exposure limit
        max_by_exposure = int((cfg.max_single_stock_pct * capital) / entry)
        position_size = min(position_size, max_by_exposure)

        # Per-signal pacing cap. Keeps the first 1-2 signals of a
        # heartbeat from saturating the daily portfolio budget,
        # leaving room for higher-conviction setups later in the
        # day. Optionally scaled by ML confidence so a 0.95-conf
        # signal gets a bigger slot than a 0.75 one (interpolated
        # linearly between confidence_scaled_min_factor at threshold
        # and 1.0 at conf=0.95+).
        signal_slot_pct = cfg.max_pct_per_signal
        if cfg.confidence_scaled_sizing_enabled:
            conf = float(signal.get("confidence_score") or 0)
            # threshold = lower of the BUY/SELL minimums so we don't
            # accidentally scale below the floor for a SELL when the
            # BUY threshold is set higher (or vice versa).
            sig_type = signal.get("signal_type", "BUY")
            sig_holding = str(
                signal.get("expected_holding_period")
                or signal.get("holding_period")
                or ""
            )
            base_threshold = cfg.resolve_min_confidence(sig_holding, sig_type)
            top = 0.95
            if conf <= base_threshold:
                factor = cfg.confidence_scaled_min_factor
            elif conf >= top:
                factor = 1.0
            else:
                span = top - base_threshold
                progress = (conf - base_threshold) / span if span > 0 else 1.0
                factor = (
                    cfg.confidence_scaled_min_factor
                    + (1.0 - cfg.confidence_scaled_min_factor) * progress
                )
            signal_slot_pct = cfg.max_pct_per_signal * factor
        max_by_signal = int((signal_slot_pct * capital) / entry)
        if max_by_signal < position_size:
            logger.info(
                "risk-check: pacing cap for %s — size %d -> %d "
                "(slot=%.1f%% of ₹%.0f capital)",
                signal["symbol"], position_size, max_by_signal,
                signal_slot_pct * 100, capital,
            )
            position_size = max_by_signal

        # Margin enforcement.
        #
        # If margin_usage_enabled is False (default), every rupee of
        # notional must fit in available cash — accurate for CNC, and a
        # safe conservative choice for MIS (where Zerodha would give
        # leverage but we choose not to use it).
        #
        # If margin_usage_enabled is True, ask the broker for the
        # canonical margin via kite.order_margins. For MIS that returns
        # the real ~5× leveraged requirement; for CNC it returns the
        # full notional plus any STT/duty add-ons. We pick the broker
        # number when available, else fall back to notional.
        product = signal.get("product", "CNC")
        if entry > 0 and position_size > 0:
            margin_required: float | None = None
            if cfg.margin_usage_enabled:
                try:
                    legs = [{
                        "exchange": "NSE",
                        "tradingsymbol": signal["symbol"],
                        "transaction_type": signal["signal_type"],
                        "variety": "regular",
                        "product": product,
                        "order_type": "LIMIT",
                        "quantity": int(position_size),
                        "price": float(entry),
                    }]
                    est = await self.ctx.broker.estimate_margin(legs)
                    if est and est.get("total", 0) > 0:
                        margin_required = float(est["total"])
                except Exception:
                    logger.debug("estimate_margin failed; falling back to notional", exc_info=True)

            if margin_required is None:
                # Notional fallback (also used when margin_usage_enabled is False)
                margin_required = entry * position_size

            if margin_required > available_cash and position_size > 0:
                # Shrink to whatever fits, scaling proportionally
                shrink = available_cash / margin_required
                new_size = max(0, int(position_size * shrink))
                logger.info(
                    "risk-check: capping %s size from %d to %d "
                    "(margin ₹%.0f vs cash ₹%.0f, source=%s)",
                    signal["symbol"], position_size, new_size,
                    margin_required, available_cash,
                    "broker" if cfg.margin_usage_enabled and margin_required != entry * position_size else "notional",
                )
                position_size = new_size

        # Slippage feedback — reduce sizing for high-slippage symbols
        slippage_penalty = await self._get_slippage_penalty(signal["symbol"])
        if slippage_penalty > 0:
            position_size = int(position_size * (1 - slippage_penalty))
            logger.info(
                "Slippage penalty for %s: %.1f%% size reduction",
                signal["symbol"], slippage_penalty * 100,
            )

        # Conviction-based sizing — scale position by ML confidence
        if cfg.conviction_sizing.enabled:
            multiplier = self._compute_conviction_multiplier(
                signal, cfg.conviction_sizing,
            )
            position_size = max(1, int(position_size * multiplier))
            logger.info(
                "risk-check: conviction sizing for %s — confidence=%.2f, multiplier=%.2f",
                signal["symbol"],
                signal.get("confidence_score", 0),
                multiplier,
            )

        # Regime-aware up-sizing in strongly-favourable regimes.
        # Applied after conviction sizing, capped by max_single_stock_pct.
        if cfg.regime_gate.enabled and regime_size_multiplier != 1.0:
            scaled = int(position_size * regime_size_multiplier)
            position_size = min(scaled, max_by_exposure)
            logger.info(
                "risk-check: regime size multiplier %.2f for %s -> %d",
                regime_size_multiplier, signal["symbol"], position_size,
            )

        # Depth-imbalance size reduction — book opposed the signal but
        # not so severely that we veto entirely; enter smaller instead.
        if depth_size_multiplier != 1.0 and position_size > 0:
            scaled = int(position_size * depth_size_multiplier)
            position_size = max(1, min(scaled, max_by_exposure))
            logger.info(
                "risk-check: depth-gate size multiplier %.2f for %s -> %d",
                depth_size_multiplier, signal["symbol"], position_size,
            )

        # Institutional-flow conviction multiplier — uses NSE
        # bulk/block deals (per-symbol) and FII net flow (market-wide)
        # which we now persist on every ingest-data cycle. Read at
        # signal-evaluation time so changes show up immediately, no
        # retrain required.
        if cfg.institutional_flow.enabled and position_size > 0:
            inst_mult = await self._compute_institutional_flow_multiplier(
                signal, cfg.institutional_flow,
            )
            if inst_mult != 1.0:
                scaled = int(position_size * inst_mult)
                position_size = max(1, min(scaled, max_by_exposure))
                logger.info(
                    "risk-check: institutional-flow multiplier %.2f for %s -> %d",
                    inst_mult, signal["symbol"], position_size,
                )

        # Effective-risk re-clamp. The conviction / regime /
        # institutional multipliers stack multiplicatively above, so a
        # strongly-favourable signal (1.5 × 1.5 × 1.2 = 2.7×) can blow
        # through max_risk_per_trade_pct in actual rupees-at-stake even
        # when notional caps haven't fired. risk_uplift_cap is the
        # ceiling on how far that stack is allowed to push effective
        # risk above the base — default 1.5× means a 2% base risk can
        # grow to 3% on a hot stack but no further.
        if risk_per_share > 0 and position_size > 0:
            effective_risk = position_size * risk_per_share
            max_allowed_risk = (
                capital * cfg.max_risk_per_trade_pct * cfg.risk_uplift_cap
            )
            if effective_risk > max_allowed_risk:
                clamped = max(1, int(max_allowed_risk / risk_per_share))
                logger.info(
                    "risk-check: effective-risk clamp for %s — "
                    "size %d -> %d (risk ₹%.0f -> ₹%.0f, cap %.2f× base)",
                    signal["symbol"], position_size, clamped,
                    effective_risk, max_allowed_risk, cfg.risk_uplift_cap,
                )
                position_size = clamped

        # Cumulative size-multiplier audit. Logs the net effect of
        # every gate that touched position_size since base_position_size
        # was computed. Helps debug "why is my size X?" without
        # threading through six separate skill log lines.
        if base_position_size > 0:
            net_mult = position_size / base_position_size
            logger.info(
                "risk-check: %s final size %d (base %d, net multiplier %.2fx, "
                "confidence %.2f)",
                signal["symbol"], position_size, base_position_size,
                net_mult, float(signal.get("confidence_score") or 0),
            )

        # Liquidity gate — refuse to be more than max_pct_of_top5 of
        # the order book's near-the-touch side. Stops you eating your
        # own slippage on thinly traded names.
        if (
            cfg.liquidity_gate.enabled
            and self.ctx.config.market_data.kite_data_enabled
            and position_size > 0
        ):
            liq_rejection = await self._check_liquidity_gate(
                signal, position_size, cfg.liquidity_gate,
            )
            if liq_rejection:
                return self._reject(signal, liq_rejection)

        if position_size <= 0:
            return self._reject(signal, "Computed position size is 0")

        # Cost-adjusted reward:risk gate. The model fires plenty of
        # "0.6 × ATR target on a sub-₹200 stock at small qty" setups
        # whose gross 2:1 collapses to ~1.3:1 after Zerodha brokerage,
        # STT, GST, and exchange fees — leaving no margin for slippage.
        # Reject them before they reach LLM review / pending queue.
        if cfg.min_net_rr > 0:
            target = float(signal.get("target_price") or 0)
            if target > 0 and entry > 0:
                from yolovest.costs import evaluate_net_rr
                net_rr, costs, reason = evaluate_net_rr(
                    signal_type=signal.get("signal_type", "BUY"),
                    entry_price=entry,
                    target_price=target,
                    stop_loss_price=sl,
                    quantity=position_size,
                    product=product,
                    cost_config=getattr(self.ctx.config, "transaction_costs", None),
                )
                if reason is not None:
                    return self._reject(signal, reason)
                if net_rr is not None and net_rr < cfg.min_net_rr:
                    direction = 1 if signal.get("signal_type") == "BUY" else -1
                    gross_win = (target - entry) * direction * position_size
                    gross_loss = (entry - sl) * direction * position_size
                    return self._reject(
                        signal,
                        f"Net R:R {net_rr:.2f} < {cfg.min_net_rr:.2f} "
                        f"(gross ₹{gross_win:.0f} win / ₹{gross_loss:.0f} loss, "
                        f"costs ₹{costs:.0f} round-trip on {position_size} qty)",
                    )

        logger.info(
            "risk-check: APPROVED %s — size=%d (risk=₹%.0f, slippage_penalty=%.1f%%)",
            signal["symbol"], position_size, risk_amount, slippage_penalty * 100,
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "approved": True,
                "symbol": signal["symbol"],
                "original_size": signal.get("position_size"),
                "adjusted_size": position_size,
                "risk_amount": risk_amount,
                "weekly_breaker_active": portfolio["weekly_pnl_pct"] <= -cfg.weekly_loss_limit_pct,
                "slippage_penalty": slippage_penalty,
                "signal": {**signal, "position_size": position_size},
            },
        )

    async def _get_slippage_penalty(self, symbol: str) -> float:
        """Compute position sizing penalty based on historical slippage.

        Returns a reduction factor (0.0 to 0.3). If avg slippage > 0.5% of entry,
        reduce size proportionally, capped at 30%.
        """
        try:
            stats = await self.ctx.db.get_slippage_stats(symbol=symbol, days=30)
            if stats["total_trades"] < 3:
                return 0.0

            avg_slippage_pct = stats["avg_slippage_pct"]
            # Threshold: start penalizing above 0.2% slippage
            threshold = 0.002
            if avg_slippage_pct <= threshold:
                return 0.0

            # Scale penalty: 0.2% -> 0%, 0.5% -> 10%, 1% -> 27%, cap at 30%
            excess = avg_slippage_pct - threshold
            penalty = min(excess * 10, 0.30)
            return penalty
        except Exception:
            logger.debug("Slippage penalty calc failed for %s", symbol, exc_info=True)
            return 0.0

    def _reject(self, signal: dict[str, Any], reason: str) -> SkillResult:
        logger.info("risk-check: REJECTED %s — %s", signal["symbol"], reason)
        return SkillResult(
            success=True,  # skill ran fine, trade was rejected by design
            skill_name=self.name,
            data={
                "approved": False,
                "symbol": signal["symbol"],
                "rejection_reason": reason,
            },
        )

    def _defer(self, signal: dict[str, Any], reason: str) -> SkillResult:
        """Block the signal for a transient time-based reason (currently
        only "Outside order window"). Distinct from `_reject` so the
        orchestrator can route this to the `time_blocked` disposition
        instead of `risk_rejected` — the symbol stays eligible for
        re-evaluation on the next heartbeat *without* burning a
        max_risk_rejected_retries_per_day slot. Genuine risk decisions
        (exposure, cooldown, depth, correlation) still go through
        `_reject` and consume retries as before.
        """
        logger.info("risk-check: DEFERRED %s — %s", signal["symbol"], reason)
        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "approved": False,
                "deferred": True,
                "symbol": signal["symbol"],
                "rejection_reason": reason,
            },
        )

    def _compute_conviction_multiplier(
        self,
        signal: dict[str, Any],
        cfg: Any,
    ) -> float:
        """Linear interpolation of position size multiplier based on confidence.

        Maps confidence_floor -> min_multiplier and
        confidence_ceiling -> max_multiplier.
        Values outside the range are clamped to min/max.
        """
        confidence = signal.get("confidence_score", 0.0)

        if confidence <= cfg.confidence_floor:
            return cfg.min_multiplier
        if confidence >= cfg.confidence_ceiling:
            return cfg.max_multiplier

        # Linear interpolation
        ratio = (confidence - cfg.confidence_floor) / (
            cfg.confidence_ceiling - cfg.confidence_floor
        )
        return cfg.min_multiplier + ratio * (cfg.max_multiplier - cfg.min_multiplier)

    async def _compute_institutional_flow_multiplier(
        self,
        signal: dict[str, Any],
        cfg: Any,
    ) -> float:
        """Return a sizing multiplier in [1/M, M] based on:

        - Recent bulk/block deals on the symbol (last N days): if
          net-buy bulk count >= 2 and signal is BUY, scale up by
          `bulk_deal_size_multiplier`. Mirror for SELL with net-sell
          deals. Opposite alignment scales down by 1/multiplier.
        - Today's FII net flow (₹ crore): when |fii_net| crosses
          `fii_net_threshold_cr`, agreeing signal direction gets a
          multiplicative bonus, opposing gets a discount.

        Both factors compose multiplicatively. Returns 1.0 when no
        data is available (graceful degradation).
        """
        symbol = signal["symbol"]
        signal_type = signal.get("signal_type", "BUY")
        multiplier = 1.0

        # Bulk-deal alignment.
        try:
            counts = await self.ctx.db.count_recent_bulk_deals(
                symbol, lookback_days=cfg.bulk_deal_lookback_days,
            )
        except Exception:
            logger.debug("count_recent_bulk_deals failed for %s", symbol, exc_info=True)
            counts = {"buy_count": 0, "sell_count": 0}
        net = counts["buy_count"] - counts["sell_count"]
        if signal_type == "BUY":
            if net >= 2:
                multiplier *= cfg.bulk_deal_size_multiplier
            elif net <= -2:
                multiplier /= cfg.bulk_deal_size_multiplier
        else:  # SELL
            if net <= -2:
                multiplier *= cfg.bulk_deal_size_multiplier
            elif net >= 2:
                multiplier /= cfg.bulk_deal_size_multiplier

        # FII regime alignment.
        try:
            fii = await self.ctx.db.get_latest_fii_dii()
        except Exception:
            logger.debug("get_latest_fii_dii failed", exc_info=True)
            fii = None
        if fii:
            fii_net = fii.get("fii_net", 0.0)
            if fii_net >= cfg.fii_net_threshold_cr:
                if signal_type == "BUY":
                    multiplier *= cfg.fii_aligned_size_multiplier
                else:
                    multiplier /= cfg.fii_aligned_size_multiplier
            elif fii_net <= -cfg.fii_net_threshold_cr:
                if signal_type == "SELL":
                    multiplier *= cfg.fii_aligned_size_multiplier
                else:
                    multiplier /= cfg.fii_aligned_size_multiplier
        return multiplier

    def _apply_regime_gate(
        self,
        signal: dict[str, Any],
        regime: dict[str, float],
        cfg: Any,
    ) -> float:
        """Return position-size multiplier to apply, or 0.0 to reject.

        - Reject (return 0.0) when regime opposes direction.
        - Return >1.0 when regime strongly favours direction (size up).
        - Else return 1.0 (no change).

        Small sample sizes (<10 symbols with two consecutive daily
        bars) fall back to neutral — we don't have a reliable signal.
        """
        if regime.get("sample_size", 0) < 10:
            return 1.0
        breadth = regime["breadth"]
        signal_type = signal.get("signal_type", "BUY")
        if signal_type == "BUY":
            if breadth < cfg.min_breadth_for_buy:
                return 0.0
            if breadth >= cfg.bullish_breadth_threshold:
                return cfg.bullish_size_multiplier
            return 1.0
        if signal_type == "SELL":
            if breadth > cfg.max_breadth_for_sell:
                return 0.0
            if breadth <= cfg.bearish_breadth_threshold:
                return cfg.bearish_size_multiplier
            return 1.0
        return 1.0

    async def _check_liquidity_gate(
        self,
        signal: dict[str, Any],
        position_size: int,
        cfg: Any,
    ) -> str | None:
        """Reject when position_size would consume more than
        max_pct_of_top5 of the relevant side of the order book.
        Quote fetch failure is non-blocking (returns None).
        """
        try:
            quote = await self.ctx.market_data.get_quote(signal["symbol"])
        except Exception:
            logger.debug(
                "risk-check: liquidity-gate quote fetch failed for %s",
                signal["symbol"], exc_info=True,
            )
            return None
        signal_type = signal.get("signal_type", "BUY")
        # BUY consumes the ask (top-5 sell), SELL consumes the bid.
        side_qty_key = "top5_sell_qty" if signal_type == "BUY" else "top5_buy_qty"
        side_qty = int(quote.get(side_qty_key) or 0)
        if side_qty <= 0:
            return None  # No depth available — let it through.
        if position_size > side_qty * cfg.max_pct_of_top5:
            return (
                f"Liquidity gate: size {position_size} > "
                f"{cfg.max_pct_of_top5:.0%} of top-5 {signal_type} depth "
                f"({side_qty})"
            )
        return None

    async def _check_depth_gate(
        self,
        signal: dict[str, Any],
        cfg: Any,
    ) -> float:
        """Return a position-size multiplier in [min_size_multiplier, 1.0].

        Neutral or favourable book → 1.0 (no change).
        Opposed book → linearly scaled down toward cfg.min_size_multiplier.
        Quote fetch failures → 1.0 so infra issues never silently shrink size.

        Imbalance = (buy_qty - sell_qty) / (buy_qty + sell_qty), [-1, +1].
        For a BUY signal the hostile extreme is -1.0 (all sell-side depth);
        for a SELL signal it is +1.0. The neutral point for each direction
        is 0.0 (balanced book). The multiplier ramps linearly from 1.0 at
        the neutral point down to min_size_multiplier at the hostile extreme.
        """
        try:
            quote = await self.ctx.market_data.get_quote(signal["symbol"])
        except Exception:
            logger.debug(
                "risk-check: depth-gate quote fetch failed for %s",
                signal["symbol"], exc_info=True,
            )
            return 1.0

        buy_qty = float(quote.get("total_buy_quantity") or 0)
        sell_qty = float(quote.get("total_sell_quantity") or 0)
        if buy_qty + sell_qty <= 0:
            return 1.0  # No depth available — full size.

        imbalance = (buy_qty - sell_qty) / (buy_qty + sell_qty)
        signal_type = signal.get("signal_type", "BUY")
        min_mult = cfg.min_size_multiplier
        scale = 1.0 - min_mult  # range available for scaling

        if signal_type == "BUY":
            # hostile direction is negative imbalance; neutral is 0.0
            adverse = max(0.0, -imbalance)  # 0 when balanced/buy-heavy
        else:
            # hostile direction is positive imbalance; neutral is 0.0
            adverse = max(0.0, imbalance)  # 0 when balanced/sell-heavy

        multiplier = max(min_mult, 1.0 - adverse * scale)

        if multiplier < 1.0:
            logger.info(
                "risk-check: depth-gate %s imbalance=%+.2f -> size multiplier=%.2f",
                signal["symbol"], imbalance, multiplier,
            )
        return multiplier

    async def _check_correlation_limit(
        self,
        signal: dict[str, Any],
        cfg: Any,
    ) -> str | None:
        """Check if adding this symbol would exceed the correlated-positions limit.

        Returns a rejection reason string if the limit is breached, or None if OK.
        """
        try:
            import numpy as np
        except ImportError:
            logger.warning(
                "risk-check: numpy not installed — skipping correlation limit check",
            )
            return None

        open_positions = await self.ctx.db.get_open_positions(mode=self.ctx.config.mode)
        # Pending BUYs/SELLs from the same heartbeat batch count too —
        # the original failure mode (3-correlated-trades in one batch)
        # slipped past this check because none had executed yet.
        new_symbol = signal["symbol"]
        new_signal_type = signal.get("signal_type", "BUY")
        try:
            pending = await self.ctx.db.get_pending_trades()
        except Exception:
            pending = []
        pending_symbols = [
            t["symbol"] for t in pending
            if t.get("symbol") and t["symbol"] != new_symbol
            and (t.get("signal_type") or "BUY") == new_signal_type
        ]

        open_symbols = [p["symbol"] for p in open_positions] + pending_symbols
        if not open_symbols:
            return None

        # Fetch daily close prices for the new symbol
        try:
            new_bars = await self.ctx.market_data.get_ohlcv(
                new_symbol, days=cfg.lookback_days,
            )
        except Exception:
            logger.warning(
                "risk-check: could not fetch OHLCV for %s — skipping correlation check",
                new_symbol,
            )
            return None

        if not new_bars or len(new_bars) < 10:
            return None

        new_closes = [b["close"] if isinstance(b, dict) else b.close for b in new_bars]

        correlated_count = 0
        correlated_symbols: list[str] = []

        for sym in open_symbols:
            try:
                sym_bars = await self.ctx.market_data.get_ohlcv(
                    sym, days=cfg.lookback_days,
                )
            except Exception:
                logger.debug("Failed to get OHLCV for correlation check: %s", sym)
                continue

            if not sym_bars:
                continue

            sym_closes = [
                b["close"] if isinstance(b, dict) else b.close for b in sym_bars
            ]

            # Align lengths to the shorter series
            min_len = min(len(new_closes), len(sym_closes))
            if min_len < 10:
                continue

            a = np.array(new_closes[:min_len], dtype=float)
            b = np.array(sym_closes[:min_len], dtype=float)

            # Pearson correlation
            corr_matrix = np.corrcoef(a, b)
            corr = float(corr_matrix[0, 1])

            if abs(corr) >= cfg.correlation_threshold:
                correlated_count += 1
                correlated_symbols.append(f"{sym}({corr:.2f})")

        if correlated_count >= cfg.max_correlated_positions:
            return (
                f"Correlation limit: {new_symbol} highly correlated with "
                f"{correlated_count} open positions "
                f"(max={cfg.max_correlated_positions}): "
                f"{', '.join(correlated_symbols)}"
            )

        return None
