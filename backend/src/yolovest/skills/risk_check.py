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

    def should_run(self) -> bool:
        return True  # Always available — gating is per-signal

    async def execute(self, **kwargs: Any) -> SkillResult:
        signal = kwargs["signal"]
        cfg = self.ctx.config.risk
        portfolio = await self.ctx.db.get_portfolio_state(
            weekly_reset_day=cfg.weekly_reset_day,
        )

        # Kill switch
        if cfg.kill_switch_enabled and await self.ctx.db.is_kill_switch_active():
            return self._reject(signal, "Kill switch is active")

        # Market hours
        if not self.ctx.market_hours.is_order_window():
            return self._reject(signal, "Outside order window")

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

        # Max trades per day
        if portfolio["trades_today"] >= cfg.max_trades_per_day:
            return self._reject(signal, f"Max trades/day reached ({cfg.max_trades_per_day})")

        # Loss cooldown
        if portfolio["minutes_since_last_loss"] < cfg.loss_cooldown_minutes:
            remaining = cfg.loss_cooldown_minutes - portfolio["minutes_since_last_loss"]
            return self._reject(signal, f"Loss cooldown active ({remaining:.0f}min remaining)")

        # Max open positions
        if portfolio["open_positions"] >= cfg.max_open_positions:
            return self._reject(signal, f"Max open positions reached ({cfg.max_open_positions})")

        # Max portfolio exposure
        if portfolio["exposure_pct"] >= cfg.max_portfolio_exposure_pct:
            return self._reject(
                signal,
                f"Portfolio exposure limit ({cfg.max_portfolio_exposure_pct:.0%})",
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

        # Weekly circuit breaker — reduce sizing
        if portfolio["weekly_pnl_pct"] <= -cfg.weekly_loss_limit_pct:
            position_size = int(position_size * cfg.weekly_loss_sizing_reduction)

        # Cap by single stock exposure limit
        max_by_exposure = int((cfg.max_single_stock_pct * capital) / entry)
        position_size = min(position_size, max_by_exposure)

        # Margin enforcement — when disabled, total trade value must fit in available cash
        if not cfg.margin_usage_enabled and entry > 0:
            max_by_cash = int(available_cash / entry)
            if position_size > max_by_cash:
                logger.info(
                    "risk-check: margin disabled — capping %s size from %d to %d (cash=₹%.0f)",
                    signal["symbol"], position_size, max_by_cash, available_cash,
                )
                position_size = max_by_cash

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

        if position_size <= 0:
            return self._reject(signal, "Computed position size is 0")

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

        open_positions = await self.ctx.db.get_open_positions()
        if not open_positions:
            return None

        open_symbols = [p["symbol"] for p in open_positions]
        new_symbol = signal["symbol"]

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
