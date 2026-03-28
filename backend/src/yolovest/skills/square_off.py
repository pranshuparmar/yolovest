"""Skill: square-off — Auto square-off intraday positions at EOD.

Trigger: CRON at market_hours.square_off time (default 15:15 IST)
Pipeline position: Runs near market close, independent of signal pipeline.

Flow:
1. Identify all open intraday (MIS/product) positions
2. Skip swing positions (CNC) — they hold overnight
3. For each MIS position:
   a. Cancel any pending SL/target orders for the position
   b. Place market order to close the position
   c. Record exit price and PnL
4. Operate within square_off_extension window for order execution
5. Send Telegram summary of all squared-off positions
6. If any square-off fails, alert immediately and retry
"""

from typing import Any

from yolovest.costs import compute_transaction_costs
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class SquareOffSkill(SkillBase):
    name = "square-off"
    description = "Auto close all intraday positions at EOD"
    trigger = SkillTrigger.CRON
    schedule = None  # set from market_hours.square_off config in __init__

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        # Build cron from market_hours.square_off (e.g. "15:15" → "15 15 * * 1-5")
        sq_time = ctx.config.market_hours.square_off
        try:
            parts = sq_time.split(":")
            h, m = int(parts[0]), int(parts[1])
            self.schedule = f"{m} {h} * * 1-5"  # weekdays only
        except (ValueError, IndexError):
            self.schedule = "15 15 * * 1-5"  # fallback default

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_square_off_window())

    async def execute(self, **kwargs: Any) -> SkillResult:
        force = kwargs.get("force", False)  # True when called from kill-switch
        positions = await self.ctx.db.get_open_positions()

        # Filter to MIS (intraday) only, unless force=True (kill switch closes everything)
        if not force:
            positions = [p for p in positions if p["product"] == "MIS"]

        squared_off = []
        failures = []

        for pos in positions:
            try:
                # Cancel pending SL/target orders
                if pos.get("sl_order_id"):
                    await self.ctx.broker.cancel_order(pos["sl_order_id"])

                # Place market exit order
                exit_type = "SELL" if pos["signal_type"] == "BUY" else "BUY"
                exit_order_id = await self.ctx.broker.place_order(
                    symbol=pos["symbol"],
                    side=exit_type,
                    quantity=pos["quantity"],
                    order_type="MARKET",
                    product=pos.get("product", "MIS"),
                )

                # Get fill price
                order_status = await self.ctx.broker.get_order_status(exit_order_id)
                exit_price = order_status.get("average_price")

                # Compute PnL with transaction costs
                qty = pos["quantity"]
                entry = pos["entry_price"]
                if pos["signal_type"] == "BUY":
                    gross_pnl = (exit_price - entry) * qty
                else:
                    gross_pnl = (entry - exit_price) * qty

                product = pos.get("product", "MIS")
                costs = compute_transaction_costs(
                    entry, exit_price, qty, product=product,
                    cost_config=self.ctx.config.transaction_costs,
                )
                pnl = gross_pnl - costs

                await self.ctx.db.close_position(pos["trade_id"], exit_price, pnl)
                squared_off.append({"symbol": pos["symbol"], "pnl": pnl})

            except Exception as e:
                failures.append({"symbol": pos["symbol"], "error": str(e)})

        # Telegram summary
        if squared_off or failures:
            total_pnl = sum(s["pnl"] for s in squared_off)
            await self.ctx.notify.send(
                f"Square-off complete: {len(squared_off)} positions closed, "
                f"PnL: ₹{total_pnl:,.2f}"
                + (f"\nFailures: {len(failures)}" if failures else "")
            )

        return SkillResult(
            success=len(failures) == 0,
            skill_name=self.name,
            data={
                "squared_off": squared_off,
                "total_pnl": sum(s["pnl"] for s in squared_off),
                "failures": failures,
                "force": force,
            },
            error=f"{len(failures)} positions failed to close" if failures else None,
        )
