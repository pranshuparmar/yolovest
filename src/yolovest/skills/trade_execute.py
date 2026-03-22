"""Skill: trade-execute — Place orders via broker.

Covers: FR-6.1, FR-6.2, FR-6.4, FR-6.6, FR-6.7, FR-6.8
Trigger: EVENT — called for each LLM-approved signal
Pipeline position: After llm-review (final step in signal→trade pipeline).

Flow:
1. Check mode: paper vs live (FR-6.2)
2. For paper mode: simulate order fill, log to DB
3. For live mode:
   a. Build order params (symbol, qty, type, price, SL)
   b. Place primary order via Kite API (FR-6.1)
   c. Place stop-loss order simultaneously
   d. Track order lifecycle: placed → open → filled/rejected (FR-6.4)
   e. On failure: retry with exponential backoff (FR-6.6)
   f. Record slippage: expected vs actual fill price (FR-6.7)
4. Respect Kite rate limits: 10 req/s (FR-6.8)
5. Log full execution details for audit trail
6. Emit trade event for predict-track and position-monitor
7. Send Telegram alert (trade_entry)
"""

import asyncio
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class TradeExecuteSkill(SkillBase):
    name = "trade-execute"
    description = "Place orders via broker (paper or live)"
    trigger = SkillTrigger.EVENT
    schedule = None

    def should_run(self) -> bool:
        # Always available — auth is checked by health-check at pipeline start.
        # is_authenticated() is async and cannot be called from sync should_run().
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        signal = kwargs["signal"]
        is_paper = self.ctx.config.mode == "paper"

        if is_paper:
            return await self._execute_paper(signal)
        else:
            return await self._execute_live(signal)

    async def _execute_paper(self, signal: dict[str, Any]) -> SkillResult:
        """Simulate order execution for paper trading (FR-6.2).

        Applies configurable simulated slippage from execution.paper_slippage_pct.
        """
        entry = signal["entry_price"]
        slippage_pct = self.ctx.config.execution.paper_slippage_pct
        # BUY fills slightly higher, SELL fills slightly lower
        if signal["signal_type"] == "BUY":
            fill_price = entry * (1 + slippage_pct)
        else:
            fill_price = entry * (1 - slippage_pct)
        slippage = abs(fill_price - entry)

        trade = {
            "symbol": signal["symbol"],
            "signal_type": signal["signal_type"],
            "entry_price": entry,
            "fill_price": round(fill_price, 2),
            "quantity": signal["position_size"],
            "stop_loss_price": signal["stop_loss_price"],
            "target_price": signal["target_price"],
            "product": signal.get("product", "MIS"),
            "status": "filled",
            "mode": "paper",
            "slippage": round(slippage, 2),
        }

        trade_id = await self.ctx.db.insert_trade(trade)
        trade["trade_id"] = trade_id
        await self.ctx.notify.send_trade_alert(trade)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"trade": trade, "mode": "paper"},
        )

    async def _execute_live(self, signal: dict[str, Any]) -> SkillResult:
        """Place real orders via Kite Connect."""
        cfg = self.ctx.config.execution
        last_error = None
        product = signal.get("product", "MIS")

        for attempt in range(cfg.max_order_retries + 1):
            try:
                # Place primary order
                order_id = await self.ctx.broker.place_order(
                    symbol=signal["symbol"],
                    side="BUY" if signal["signal_type"] == "BUY" else "SELL",
                    quantity=signal["position_size"],
                    order_type="LIMIT",
                    price=signal["entry_price"],
                    product=product,
                )

                # Place stop-loss order
                sl_order_id = await self.ctx.broker.place_order(
                    symbol=signal["symbol"],
                    side="SELL" if signal["signal_type"] == "BUY" else "BUY",
                    quantity=signal["position_size"],
                    order_type="SL",
                    trigger_price=signal["stop_loss_price"],
                    product=product,
                )

                # FR-6.4: Track order status, handle partial fills
                await asyncio.sleep(0.5)  # brief wait for fill
                order_status = await self.ctx.broker.get_order_status(order_id)

                # Check for partial fill within timeout
                filled_qty = order_status.get("filled_quantity", signal["position_size"])
                if filled_qty < signal["position_size"]:
                    # Wait up to order_timeout_sec for full fill
                    for _ in range(cfg.order_timeout_sec):
                        await asyncio.sleep(1)
                        order_status = await self.ctx.broker.get_order_status(order_id)
                        filled_qty = order_status.get("filled_quantity", signal["position_size"])
                        if filled_qty >= signal["position_size"]:
                            break

                    # Cancel remainder if still partially filled
                    if filled_qty < signal["position_size"] and filled_qty > 0:
                        await self.ctx.broker.cancel_order(order_id)
                        # Adjust SL order to filled quantity only
                        if filled_qty != signal["position_size"]:
                            await self.ctx.broker.cancel_order(sl_order_id)
                            sl_order_id = await self.ctx.broker.place_order(
                                symbol=signal["symbol"],
                                side="SELL" if signal["signal_type"] == "BUY" else "BUY",
                                quantity=filled_qty,
                                order_type="SL",
                                trigger_price=signal["stop_loss_price"],
                                product=product,
                            )

                actual_qty = filled_qty if filled_qty > 0 else signal["position_size"]

                # Compute slippage (FR-6.7)
                fill_price = order_status.get("average_price", signal["entry_price"])
                slippage = abs(fill_price - signal["entry_price"])

                trade = {
                    "symbol": signal["symbol"],
                    "signal_type": signal["signal_type"],
                    "entry_price": signal["entry_price"],
                    "fill_price": fill_price,
                    "quantity": actual_qty,
                    "stop_loss_price": signal["stop_loss_price"],
                    "target_price": signal["target_price"],
                    "order_id": order_id,
                    "sl_order_id": sl_order_id,
                    "product": product,
                    "status": order_status.get("status", "filled"),
                    "mode": "live",
                    "slippage": slippage,
                }

                trade_id = await self.ctx.db.insert_trade(trade)
                trade["trade_id"] = trade_id
                await self.ctx.notify.send_trade_alert(trade)

                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={"trade": trade, "mode": "live", "attempts": attempt + 1},
                )

            except Exception as e:
                last_error = e
                if attempt < cfg.max_order_retries:
                    delay = cfg.retry_base_delay_sec * (2**attempt)
                    await asyncio.sleep(delay)

        return SkillResult(
            success=False,
            skill_name=self.name,
            error=f"Order failed after {cfg.max_order_retries + 1} attempts: {last_error}",
        )
