"""Skill: risk-check — Validate signals against all configurable risk rules.

Covers: FR-5.1 to FR-5.18
Trigger: EVENT — called for each signal from generate-signals
Pipeline position: After generate-signals, before llm-review.

Flow:
1. Load current portfolio state (positions, daily PnL, weekly PnL)
2. Check kill switch state (FR-5.14-5.15) — reject all if paused
3. Check market hours enforcement (FR-5.9)
4. Check daily loss circuit breaker (FR-5.5) — stop if exceeded
5. Check weekly loss circuit breaker (FR-5.6) — reduce sizing if exceeded
6. Check max trades per day (FR-5.17)
7. Check loss cooldown (FR-5.18)
8. Check max open positions (FR-5.3)
9. Check max portfolio exposure (FR-5.2)
10. Check max single stock exposure (FR-5.4)
11. Check sector correlation (FR-5.13)
12. Validate stop-loss is present (FR-5.7)
13. Compute position size based on max risk per trade (FR-5.1)
14. Apply weekly sizing reduction if breaker active (FR-5.6a)
15. Return: approved (with adjusted size) or rejected (with reason)

All thresholds read from config.risk.* — zero hardcoded values.
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


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
        portfolio = await self.ctx.db.get_portfolio_state()

        # FR-5.14/5.15: Kill switch
        if cfg.kill_switch_enabled and await self.ctx.db.is_kill_switch_active():
            return self._reject(signal, "Kill switch is active")

        # FR-5.9: Market hours
        if not self.ctx.market_hours.is_order_window():
            return self._reject(signal, "Outside order window")

        # FR-5.5: Daily circuit breaker
        if portfolio["daily_pnl_pct"] <= -cfg.daily_loss_limit_pct:
            return self._reject(signal, f"Daily loss limit hit ({cfg.daily_loss_limit_pct:.0%})")

        # FR-5.17: Max trades per day
        if portfolio["trades_today"] >= cfg.max_trades_per_day:
            return self._reject(signal, f"Max trades/day reached ({cfg.max_trades_per_day})")

        # FR-5.18: Loss cooldown
        if portfolio["minutes_since_last_loss"] < cfg.loss_cooldown_minutes:
            remaining = cfg.loss_cooldown_minutes - portfolio["minutes_since_last_loss"]
            return self._reject(signal, f"Loss cooldown active ({remaining:.0f}min remaining)")

        # FR-5.3: Max open positions
        if portfolio["open_positions"] >= cfg.max_open_positions:
            return self._reject(signal, f"Max open positions reached ({cfg.max_open_positions})")

        # FR-5.2: Max portfolio exposure
        if portfolio["exposure_pct"] >= cfg.max_portfolio_exposure_pct:
            return self._reject(
                signal,
                f"Portfolio exposure limit ({cfg.max_portfolio_exposure_pct:.0%})",
            )

        # FR-5.4: Max single stock exposure
        stock_exposure = portfolio["stock_exposures"].get(signal["symbol"], 0)
        if stock_exposure >= cfg.max_single_stock_pct:
            return self._reject(signal, f"Single stock limit ({cfg.max_single_stock_pct:.0%})")

        # FR-5.13: Sector correlation
        stock_sector = await self.ctx.db.get_stock_sector(signal["symbol"])
        sector_count = portfolio["sector_counts"].get(stock_sector, 0)
        if sector_count >= cfg.max_same_sector_positions:
            return self._reject(
                signal,
                f"Sector limit ({stock_sector}: {cfg.max_same_sector_positions})",
            )

        # FR-5.7: Mandatory stop-loss
        if cfg.mandatory_stop_loss and not signal.get("stop_loss_price"):
            return self._reject(signal, "No stop-loss set (mandatory)")

        # FR-9.3: Capital exhaustion — check if remaining cash can cover min trade
        capital = portfolio["total_capital"]
        available_cash = portfolio.get("available_cash", capital)
        min_trade_value = signal["entry_price"]  # at least 1 share
        if available_cash < min_trade_value:
            return self._reject(
                signal,
                "Capital exhaustion: "
                f"cash ₹{available_cash:,.0f} < min trade ₹{min_trade_value:,.0f}",
            )

        # FR-5.1: Position sizing based on max risk per trade
        risk_amount = capital * cfg.max_risk_per_trade_pct
        entry = signal["entry_price"]
        sl = signal["stop_loss_price"]
        risk_per_share = abs(entry - sl)

        if risk_per_share <= 0:
            return self._reject(signal, "Invalid stop-loss (risk_per_share <= 0)")

        position_size = int(risk_amount / risk_per_share)

        # FR-5.6/5.6a: Weekly circuit breaker — reduce sizing
        if portfolio["weekly_pnl_pct"] <= -cfg.weekly_loss_limit_pct:
            position_size = int(position_size * cfg.weekly_loss_sizing_reduction)

        # Cap by single stock exposure limit
        max_by_exposure = int((cfg.max_single_stock_pct * capital) / entry)
        position_size = min(position_size, max_by_exposure)

        if position_size <= 0:
            return self._reject(signal, "Computed position size is 0")

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
                "signal": {**signal, "position_size": position_size},
            },
        )

    def _reject(self, signal: dict[str, Any], reason: str) -> SkillResult:
        return SkillResult(
            success=True,  # skill ran fine, trade was rejected by design
            skill_name=self.name,
            data={
                "approved": False,
                "symbol": signal["symbol"],
                "rejection_reason": reason,
            },
        )
