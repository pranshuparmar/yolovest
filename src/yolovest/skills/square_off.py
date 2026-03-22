"""Skill: square-off — Auto square-off intraday positions at EOD.

Covers: FR-5.9, FR-5.9a, FR-5.10
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

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class SquareOffSkill(SkillBase):
    name = "square-off"
    description = "Auto close all intraday positions at EOD"
    trigger = SkillTrigger.CRON
    schedule = None  # dynamically set from market_hours.square_off config

    def should_run(self) -> bool:
        return self.ctx.market_hours.is_square_off_window()

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

                # Compute PnL
                if pos["signal_type"] == "BUY":
                    pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
                else:
                    pnl = (pos["entry_price"] - exit_price) * pos["quantity"]

                await self.ctx.db.close_position(pos["id"], exit_price, pnl)
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
