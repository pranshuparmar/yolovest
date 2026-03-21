"""Skill: position-monitor — Monitor open positions, trail SLs, reconcile.

Covers: FR-6.5, FR-5.8, FR-5.8a, FR-5.8b, FR-6.7
Trigger: HEARTBEAT during market hours
Pipeline position: Runs continuously alongside generate-signals.

Flow:
1. Fetch current positions from broker (FR-6.5)
2. Reconcile broker state with local DB state — flag discrepancies
3. For each open position:
   a. Check if target hit → emit exit signal
   b. Check if SL hit → record loss
   c. Check trailing SL logic (FR-5.8):
      - If profit >= trailing_sl_trigger_multiple × risk → move SL to breakeven
      - Continue trailing SL upward in trailing_sl_step_pct increments
   d. Modify SL order on broker if trail triggered
4. Track unrealized PnL for portfolio state
5. Update slippage records (FR-6.7)
6. Alert on any discrepancies between local and broker state
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class PositionMonitorSkill(SkillBase):
    name = "position-monitor"
    description = "Reconcile positions, trail stop-losses, track PnL"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        cfg = self.ctx.config.risk
        local_positions = await self.ctx.db.get_open_positions()
        broker_positions = await self.ctx.broker.get_positions()

        discrepancies = self._reconcile(local_positions, broker_positions)
        if discrepancies:
            await self.ctx.notify.send(
                f"Position discrepancy detected:\n{discrepancies}"
            )

        trails_modified = 0
        targets_hit = []
        stops_hit = []

        for pos in local_positions:
            symbol = pos["symbol"]
            current_price = await self.ctx.market_data.get_ltp(symbol)
            entry = pos["entry_price"]
            sl = pos["stop_loss_price"]
            target = pos["target_price"]
            risk_per_share = abs(entry - sl)

            # Target hit?
            if (pos["signal_type"] == "BUY" and current_price >= target) or (
                pos["signal_type"] == "SELL" and current_price <= target
            ):
                targets_hit.append(symbol)
                continue

            # SL hit?
            if (pos["signal_type"] == "BUY" and current_price <= sl) or (
                pos["signal_type"] == "SELL" and current_price >= sl
            ):
                stops_hit.append(symbol)
                continue

            # Trailing SL (FR-5.8)
            if cfg.trailing_sl_enabled and risk_per_share > 0:
                profit = (current_price - entry) if pos["signal_type"] == "BUY" else (entry - current_price)
                profit_multiple = profit / risk_per_share

                if profit_multiple >= cfg.trailing_sl_trigger_multiple:
                    # Calculate new trailing SL
                    step = current_price * cfg.trailing_sl_step_pct
                    if pos["signal_type"] == "BUY":
                        new_sl = max(entry, current_price - step)  # at least breakeven
                    else:
                        new_sl = min(entry, current_price + step)

                    if self._is_better_sl(pos["signal_type"], new_sl, sl):
                        await self.ctx.broker.modify_sl_order(pos["sl_order_id"], new_sl)
                        await self.ctx.db.update_position_sl(pos["id"], new_sl)
                        trails_modified += 1

            # Update unrealized PnL
            await self.ctx.db.update_unrealized_pnl(pos["id"], current_price)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "positions_monitored": len(local_positions),
                "trails_modified": trails_modified,
                "targets_hit": targets_hit,
                "stops_hit": stops_hit,
                "discrepancies": len(discrepancies) if discrepancies else 0,
            },
        )

    def _reconcile(self, local: list[dict], broker: list[dict]) -> list[str]:
        """Compare local DB positions with broker positions. FR-6.5."""
        raise NotImplementedError

    def _is_better_sl(self, signal_type: str, new_sl: float, current_sl: float) -> bool:
        """Check if new SL is tighter (more protective) than current."""
        if signal_type == "BUY":
            return new_sl > current_sl
        return new_sl < current_sl
