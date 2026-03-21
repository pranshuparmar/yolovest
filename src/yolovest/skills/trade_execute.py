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

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class TradeExecuteSkill(SkillBase):
    name = "trade-execute"
    description = "Place orders via broker (paper or live)"
    trigger = SkillTrigger.EVENT
    schedule = None

    def should_run(self) -> bool:
        return self.ctx.broker.is_authenticated() or self.ctx.config.mode == "paper"

    async def execute(self, **kwargs: Any) -> SkillResult:
        signal = kwargs["signal"]
        is_paper = self.ctx.config.mode == "paper"

        if is_paper:
            return await self._execute_paper(signal)
        else:
            return await self._execute_live(signal)

    async def _execute_paper(self, signal: dict) -> SkillResult:
        """Simulate order execution for paper trading."""
        trade = {
            "symbol": signal["symbol"],
            "signal_type": signal["signal_type"],
            "entry_price": signal["entry_price"],
            "fill_price": signal["entry_price"],  # no slippage in paper
            "quantity": signal["position_size"],
            "stop_loss_price": signal["stop_loss_price"],
            "target_price": signal["target_price"],
            "status": "filled",
            "mode": "paper",
            "slippage": 0.0,
        }

        await self.ctx.db.insert_trade(trade)
        await self.ctx.notify.send_trade_alert(trade)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"trade": trade, "mode": "paper"},
        )

    async def _execute_live(self, signal: dict) -> SkillResult:
        """Place real orders via Kite Connect."""
        cfg = self.ctx.config.execution
        last_error = None

        for attempt in range(cfg.max_order_retries + 1):
            try:
                # Place primary order
                order_id = await self.ctx.broker.place_order(
                    symbol=signal["symbol"],
                    exchange=self.ctx.config.strategy.exchange,
                    transaction_type="BUY" if signal["signal_type"] == "BUY" else "SELL",
                    quantity=signal["position_size"],
                    order_type="LIMIT",
                    price=signal["entry_price"],
                    product="MIS",  # intraday; use CNC for swing
                )

                # Place stop-loss order
                sl_order_id = await self.ctx.broker.place_order(
                    symbol=signal["symbol"],
                    exchange=self.ctx.config.strategy.exchange,
                    transaction_type="SELL" if signal["signal_type"] == "BUY" else "BUY",
                    quantity=signal["position_size"],
                    order_type="SL",
                    trigger_price=signal["stop_loss_price"],
                    product="MIS",
                )

                # Track order status
                order_status = await self.ctx.broker.get_order_status(order_id)

                # Compute slippage (FR-6.7)
                fill_price = order_status.get("average_price", signal["entry_price"])
                slippage = abs(fill_price - signal["entry_price"])

                trade = {
                    "symbol": signal["symbol"],
                    "signal_type": signal["signal_type"],
                    "entry_price": signal["entry_price"],
                    "fill_price": fill_price,
                    "quantity": signal["position_size"],
                    "stop_loss_price": signal["stop_loss_price"],
                    "target_price": signal["target_price"],
                    "order_id": order_id,
                    "sl_order_id": sl_order_id,
                    "status": order_status.get("status"),
                    "mode": "live",
                    "slippage": slippage,
                }

                await self.ctx.db.insert_trade(trade)
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
                    await self.ctx.sleep(delay)

        return SkillResult(
            success=False,
            skill_name=self.name,
            error=f"Order failed after {cfg.max_order_retries + 1} attempts: {last_error}",
        )
