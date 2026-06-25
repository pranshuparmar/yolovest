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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


@dataclass
class _Sizing:
    """Carrier for the base-size phase of risk-check. `rejection` short-
    circuits the pipeline; the remaining fields feed the multiplier and
    post-size phases."""

    rejection: SkillResult | None = None
    position_size: int = 0
    base_position_size: int = 0
    max_by_exposure: int = 0
    risk_amount: float = 0.0
    risk_per_share: float = 0.0
    entry: float = 0.0
    sl: float = 0.0
    capital: float = 0.0
    # Largest size that fits available cash / margin at the rate the margin
    # check used. The post-margin multiplier stack must not grow size past
    # this, or the margin/cash clamp stops being the last word on affordability.
    affordable_size: int | None = None



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
        self._market_trend: dict[str, Any] | None = None
        self._market_trend_at: float = 0.0
        # Per-heartbeat beta cache. Inputs to compute_symbol_beta are
        # identical for every signal in the same cycle (last 60 days
        # of daily bars across the universe), so caching here saves
        # one full-universe scan per signal. Cleared on the next
        # cycle by virtue of being instance state — same TTL pattern
        # as _regime above.
        self._beta_cache: dict[str, float | None] = {}
        self._beta_cache_at: float = 0.0

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
            logger.warning("regime gate degraded (fail-open): compute_live_regime failed", exc_info=True)
            self._regime = {"breadth": 0.5, "avg_return": 0.0, "sample_size": 0}
        self._regime_at = now
        return self._regime

    async def _get_market_trend(self, ma_window: int) -> dict[str, Any]:
        import time as _time
        now = _time.monotonic()
        if (
            self._market_trend is not None
            and (now - self._market_trend_at) < self._regime_ttl_sec
        ):
            return self._market_trend
        try:
            self._market_trend = await self.ctx.db.compute_market_trend(ma_window)
        except Exception:
            logger.warning("market-trend filter degraded (fail-open): compute_market_trend failed", exc_info=True)
            self._market_trend = {
                "in_uptrend": True, "index_level": 1.0, "ma": 1.0,
                "sample_size": 0, "ma_window": ma_window,
            }
        self._market_trend_at = now
        return self._market_trend

    async def _get_symbol_beta(self, symbol: str) -> float | None:
        """Per-heartbeat cached beta lookup. Falls through to the DB
        only on the first call per cycle per symbol."""
        import time as _time
        now = _time.monotonic()
        if (now - self._beta_cache_at) >= self._regime_ttl_sec:
            self._beta_cache = {}
            self._beta_cache_at = now
        if symbol in self._beta_cache:
            return self._beta_cache[symbol]
        try:
            beta = await self.ctx.db.compute_symbol_beta(symbol)
        except Exception:
            logger.warning("portfolio-beta gate degraded (fail-open): compute_symbol_beta failed for %s", symbol, exc_info=True)
            beta = None
        self._beta_cache[symbol] = beta
        return beta


    async def execute(self, **kwargs: Any) -> SkillResult:
        signal = kwargs["signal"]
        cfg = self.ctx.config.risk
        portfolio = await self.ctx.db.get_portfolio_state(
            weekly_reset_day=cfg.weekly_reset_day,
            mode=self.ctx.config.mode,
        )

        # Phase 1 — hard entry gates, in original order (kill switch,
        # windows, breakers, caps, cooldowns, exposure, sector,
        # blackout, correlation).
        rejection = await self._check_entry_gates(signal, cfg, portfolio)
        if rejection is not None:
            return rejection

        # Phase 1b — gates that also produce sizing inputs: the depth
        # gate maps book imbalance to a size multiplier; the regime gate
        # can reject outright or scale; trend / SL / capital / drift
        # checks ride in the same pass.
        rejection, depth_size_multiplier, regime_size_multiplier = (
            await self._check_market_condition_gates(signal, cfg, portfolio)
        )
        if rejection is not None:
            return rejection

        # Phase 2 — base size from the risk budget, then the notional
        # caps (weekly breaker, single-stock, pacing slot, margin).
        sizing = await self._compute_base_size(signal, cfg, portfolio)
        if sizing.rejection is not None:
            return sizing.rejection

        # Phase 3 — stack conviction / regime / depth / institutional
        # multipliers, then re-clamp effective rupees-at-risk.
        position_size, slippage_penalty = await self._apply_size_multipliers(
            signal, cfg, sizing,
            depth_size_multiplier=depth_size_multiplier,
            regime_size_multiplier=regime_size_multiplier,
        )

        # Phase 4 — gates that need the final size: portfolio-beta cap,
        # liquidity gate, zero-size, cost-adjusted R:R.
        rejection = await self._check_post_size_gates(
            signal, cfg, sizing, position_size,
        )
        if rejection is not None:
            return rejection

        logger.info(
            "risk-check: APPROVED %s — size=%d (risk=₹%.0f, slippage_penalty=%.1f%%)",
            signal["symbol"], position_size, sizing.risk_amount, slippage_penalty * 100,
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "approved": True,
                "symbol": signal["symbol"],
                "original_size": signal.get("position_size"),
                "adjusted_size": position_size,
                "risk_amount": sizing.risk_amount,
                "weekly_breaker_active": portfolio["weekly_pnl_pct"] <= -cfg.weekly_loss_limit_pct,
                "slippage_penalty": slippage_penalty,
                "signal": {**signal, "position_size": position_size},
            },
        )

    async def _check_entry_gates(
        self, signal: dict[str, Any], cfg: Any, portfolio: dict[str, Any],
    ) -> SkillResult | None:
        """Hard pre-sizing gates. Returns a rejection (or deferral)
        SkillResult, or None to proceed. Order is load-bearing and
        unchanged from the original inline sequence."""
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
                logger.warning("pending trades unavailable — exposure/position caps may undercount this cycle", exc_info=True)
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

        # Earnings blackout. Block new entries within N calendar days
        # of a scheduled earnings / board-meeting announcement —
        # results gaps routinely move stocks ±5-20% overnight, wider
        # than any ATR-based SL can absorb. Off by default; opt in via
        # risk.earnings_blackout_days.
        if cfg.earnings_blackout_days > 0:
            try:
                events = await self.ctx.db.get_earnings_events(
                    symbol=signal["symbol"],
                    days=cfg.earnings_blackout_days,
                )
            except Exception:
                logger.warning(
                    "earnings-blackout gate degraded (fail-open): event lookup failed",
                    exc_info=True,
                )
                events = []
            if events:
                next_event = events[0]
                event_title = (
                    next_event.get("title") or "earnings event"
                )
                return self._reject(
                    signal,
                    f"Earnings blackout: {signal['symbol']} has "
                    f"\"{event_title}\" on {next_event.get('event_date')} "
                    f"(within {cfg.earnings_blackout_days}-day window)",
                )

        # Correlation-aware position limit (beyond simple sector counts)
        if cfg.correlation_limit.enabled:
            corr_rejection = await self._check_correlation_limit(
                signal, cfg.correlation_limit,
            )
            if corr_rejection:
                return self._reject(signal, corr_rejection)
        return None

    async def _check_market_condition_gates(
        self, signal: dict[str, Any], cfg: Any, portfolio: dict[str, Any],
    ) -> tuple[SkillResult | None, float, float]:
        """Depth/regime/trend gates plus mandatory-SL, capital-exhaustion
        and entry-drift checks. Returns (rejection-or-None,
        depth_size_multiplier, regime_size_multiplier); the multipliers
        feed the phase-3 sizing stack."""
        capital = portfolio["total_capital"]
        available_cash = portfolio.get("available_cash", capital)
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
                ), depth_size_multiplier, regime_size_multiplier

        # Market-trend circuit breaker — stand aside on NEW long entries
        # when the broad index is below its moving average (a downtrend).
        # Long-only bear protection; exits are never blocked. Fail-open
        # when there isn't enough history (sample_size == 0).
        if (
            cfg.market_trend_filter.enabled
            and signal.get("signal_type", "BUY") == "BUY"
        ):
            trend = await self._get_market_trend(cfg.market_trend_filter.ma_window)
            if trend.get("sample_size", 0) > 0 and not trend.get("in_uptrend", True):
                return self._reject(
                    signal,
                    f"Market-trend filter: index {trend['index_level']:.3f} below "
                    f"{trend['ma_window']}d MA {trend['ma']:.3f} — standing aside "
                    f"on new longs (market downtrend)",
                ), depth_size_multiplier, regime_size_multiplier

        # Mandatory stop-loss
        if cfg.mandatory_stop_loss and not signal.get("stop_loss_price"):
            return (
                self._reject(signal, "No stop-loss set (mandatory)"),
                depth_size_multiplier, regime_size_multiplier,
            )

        # Capital exhaustion — check if remaining cash can cover min trade
        capital = portfolio["total_capital"]
        available_cash = portfolio.get("available_cash", capital)
        min_trade_value = signal["entry_price"]  # at least 1 share
        if available_cash < min_trade_value:
            return self._reject(
                signal,
                "Capital exhaustion: "
                f"cash ₹{available_cash:,.0f} < min trade ₹{min_trade_value:,.0f}",
            ), depth_size_multiplier, regime_size_multiplier

        # Validate entry price against fresh LTP
        entry = signal["entry_price"]
        drift_max = self.ctx.config.execution.price_drift_max_pct
        try:
            fresh_ltp = await self.ctx.market_data.get_ltp(signal["symbol"])
            drift_pct = abs(fresh_ltp - entry) / entry if entry > 0 else 0
            if drift_pct > drift_max:
                return self._reject(
                    signal,
                    f"Entry price drift too high: signal=₹{entry:.2f}, "
                    f"current=₹{fresh_ltp:.2f} ({drift_pct:.1%})",
                ), depth_size_multiplier, regime_size_multiplier
            if drift_pct > 0.005:
                logger.info(
                    "risk-check: price drift for %s: signal=%.2f, current=%.2f (%.1f%%)",
                    signal["symbol"], entry, fresh_ltp, drift_pct * 100,
                )
        except Exception:
            logger.debug("LTP unavailable for %s price drift check", signal["symbol"])

        return None, depth_size_multiplier, regime_size_multiplier

    async def _compute_base_size(
        self, signal: dict[str, Any], cfg: Any, portfolio: dict[str, Any],
    ) -> _Sizing:
        """Base position size from max_risk_per_trade_pct, then the
        notional caps: weekly-breaker reduction, single-stock exposure,
        per-signal pacing slot, and margin/cash enforcement."""
        capital = portfolio["total_capital"]
        available_cash = portfolio.get("available_cash", capital)
        entry = signal["entry_price"]
        sl = signal["stop_loss_price"]

        # Position sizing based on max risk per trade
        risk_amount = capital * cfg.max_risk_per_trade_pct
        risk_per_share = abs(entry - sl)

        if risk_per_share <= 0:
            return _Sizing(
                rejection=self._reject(signal, "Invalid stop-loss (risk_per_share <= 0)"),
            )

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
        # day. Confidence-based scaling lives in conviction_sizing
        # (the single confidence-scaling path).
        signal_slot_pct = cfg.max_pct_per_signal
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
        affordable_size: int | None = None
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
                    logger.info("estimate_margin failed; falling back to notional sizing (conservative)", exc_info=True)

            if margin_required is None:
                # Notional fallback (also used when margin_usage_enabled is False)
                margin_required = entry * position_size

            # Largest size that fits available cash at this margin rate
            # (linear approximation — exact for the notional path, where
            # margin_required = entry * size, so this reduces to
            # available_cash / entry; conservative under broker leverage).
            # Computed off the pre-shrink size so it respects margin_usage.
            if margin_required > 0:
                affordable_size = int(available_cash * position_size / margin_required)

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

        return _Sizing(
            position_size=position_size,
            base_position_size=base_position_size,
            max_by_exposure=max_by_exposure,
            risk_amount=risk_amount,
            risk_per_share=risk_per_share,
            entry=entry,
            sl=sl,
            capital=capital,
            affordable_size=affordable_size,
        )

    async def _apply_size_multipliers(
        self,
        signal: dict[str, Any],
        cfg: Any,
        sizing: _Sizing,
        *,
        depth_size_multiplier: float,
        regime_size_multiplier: float,
    ) -> tuple[int, float]:
        """Slippage penalty + conviction / regime / depth / institutional
        multipliers, then the effective-risk re-clamp (risk_uplift_cap)
        and the cumulative size audit line. Returns
        (position_size, slippage_penalty)."""
        position_size = sizing.position_size
        base_position_size = sizing.base_position_size
        max_by_exposure = sizing.max_by_exposure
        risk_per_share = sizing.risk_per_share
        capital = sizing.capital

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

        # Affordability clamp — the last word on size. The conviction /
        # regime / institutional multipliers above can grow the size past
        # what _compute_base_size's margin/cash check allowed (each is only
        # capped to max_by_exposure, which is derived from capital, not from
        # available cash). Re-apply the cash/margin ceiling here so an
        # up-multiplier can never re-inflate a position past what the account
        # can actually fund.
        if sizing.affordable_size is not None and position_size > sizing.affordable_size:
            logger.info(
                "risk-check: affordability clamp for %s — size %d -> %d "
                "(exceeds cash/margin-affordable size)",
                signal["symbol"], position_size, sizing.affordable_size,
            )
            position_size = sizing.affordable_size

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

        return position_size, slippage_penalty

    async def _check_post_size_gates(
        self,
        signal: dict[str, Any],
        cfg: Any,
        sizing: _Sizing,
        position_size: int,
    ) -> SkillResult | None:
        """Gates that need the final size: portfolio-beta cap, liquidity
        gate, zero-size guard, cost-adjusted net R:R."""
        capital = sizing.capital
        entry = sizing.entry
        sl = sizing.sl
        product = signal.get("product", "CNC")

        # Portfolio-beta cap. Sum of (notional × beta) over currently-
        # open positions + this candidate signal must stay under
        # max_portfolio_beta × total_capital. Catches the "every
        # position is a high-beta tech name" failure mode where a
        # single bad market day wipes through every SL simultaneously.
        # Off by default; opt in via risk.max_portfolio_beta > 0.
        if cfg.max_portfolio_beta > 0 and position_size > 0:
            cap_value = capital * cfg.max_portfolio_beta
            # Open positions' beta-weighted notional. Adopted positions
            # count because they share market-day downside even if they
            # weren't system-generated.
            open_positions = await self.ctx.db.get_open_positions(
                mode=self.ctx.config.mode,
            )
            beta_value = 0.0
            for p in open_positions:
                p_sym = p.get("symbol")
                p_qty = float(p.get("quantity") or 0)
                p_entry = float(p.get("fill_price") or p.get("entry_price") or 0)
                if not p_sym or p_qty <= 0 or p_entry <= 0:
                    continue
                p_beta = await self._get_symbol_beta(p_sym) or 1.0
                beta_value += p_qty * p_entry * abs(p_beta)
            # Candidate signal's contribution
            sig_beta = await self._get_symbol_beta(signal["symbol"]) or 1.0
            candidate_value = entry * position_size * abs(sig_beta)
            total = beta_value + candidate_value
            if total > cap_value:
                return self._reject(
                    signal,
                    f"Portfolio beta cap exceeded: open beta-weighted "
                    f"₹{beta_value:,.0f} + candidate ₹{candidate_value:,.0f} "
                    f"(β={sig_beta:.2f}) = ₹{total:,.0f} > "
                    f"₹{cap_value:,.0f} (cap = {cfg.max_portfolio_beta:.1f}× capital)",
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


        return None

    async def _get_slippage_penalty(self, symbol: str) -> float:
        """Compute position sizing penalty based on historical slippage.

        Returns a reduction factor (0.0 to 0.3). Above a 0.2% average-slippage
        threshold, size is reduced proportionally (excess fraction × 10),
        capped at 30%.
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

            # Scale penalty: excess fraction over the threshold × 10, capped
            # at 30%. avg_slippage_pct is a fraction (slippage/entry), so:
            # 0.2% -> 0%, 0.5% -> 3%, 1% -> 8%, 3.2%+ -> 30% (cap).
            excess = avg_slippage_pct - threshold
            penalty = min(excess * 10, 0.30)
            return penalty
        except Exception:
            logger.info("slippage penalty skipped for %s (calc failed)", symbol, exc_info=True)
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
            logger.warning("institutional-flow gate degraded (neutral): count_recent_bulk_deals failed for %s", symbol, exc_info=True)
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
            logger.warning("institutional-flow gate degraded (neutral): get_latest_fii_dii failed", exc_info=True)
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
            logger.warning(
                "liquidity gate degraded (fail-open): quote fetch failed for %s",
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
            logger.warning(
                "depth gate degraded (full size): quote fetch failed for %s",
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
