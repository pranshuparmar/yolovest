"""Skill: trade-execute — Place orders via broker.

Trigger: EVENT — called for each LLM-approved signal
Pipeline position: After llm-review (final step in signal→trade pipeline).

Flow:
1. Check mode: paper vs live
2. For paper mode: simulate order fill, log to DB
3. For live mode:
   a. Build order params (symbol, qty, type, price, SL)
   b. Place primary order via Kite API
   c. Place stop-loss order simultaneously
   d. Track order lifecycle: placed → open → filled/rejected
   e. On failure: retry with exponential backoff
   f. Record slippage: expected vs actual fill price
4. Respect Kite rate limits: 10 req/s
5. Log full execution details for audit trail
6. Emit trade event for predict-track and position-monitor
7. Send Telegram alert (trade_entry)
"""

import asyncio
import hashlib
import logging
import math
from typing import Any

from yolovest.costs import compute_transaction_costs
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)


def _signal_dedup_key(signal: dict[str, Any]) -> str:
    """Generate a dedup key for a signal to prevent duplicate order placement.

    Key components: symbol + signal_type + date + entry_price (rounded).
    If the process crashes after placing a broker order but before recording
    the trade, the same signal re-entering this skill will be detected.
    """
    parts = (
        signal["symbol"],
        signal["signal_type"],
        now_ist().strftime("%Y-%m-%d"),
        f"{signal['entry_price']:.0f}",
    )
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


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

        # Safety check: verify broker mode matches config mode
        broker_mode = getattr(self.ctx.broker, "_mode", None)
        if broker_mode and broker_mode != self.ctx.config.mode:
            logger.error(
                "trade-execute: MODE MISMATCH — config.mode=%s but broker._mode=%s. "
                "Syncing broker to config. This may indicate a hot-reload missed the broker.",
                self.ctx.config.mode, broker_mode,
            )
            self.ctx.broker._mode = self.ctx.config.mode
            is_paper = self.ctx.config.mode == "paper"

        logger.info(
            "trade-execute: mode=%s for %s %s",
            "PAPER" if is_paper else "LIVE",
            signal.get("signal_type"), signal.get("symbol"),
        )

        if is_paper:
            return await self._execute_paper(signal)
        else:
            return await self._execute_live(signal)

    async def _execute_paper(self, signal: dict[str, Any]) -> SkillResult:
        """Simulate order execution for paper trading.

        Applies configurable simulated slippage from execution.paper_slippage_pct.
        Uses fresh LTP when available for realistic fill simulation.
        Supports scaled entry (splitting into multiple legs) when enabled.
        """
        # Use fresh LTP for realistic paper fills
        try:
            entry = await self.ctx.market_data.get_ltp(signal["symbol"])
        except Exception:
            logger.debug("LTP unavailable for paper trade %s, using signal price", signal["symbol"])
            entry = signal["entry_price"]
        slippage_pct = self.ctx.config.execution.paper_slippage_pct

        scaled_cfg = self.ctx.config.execution.scaled_entry
        total_qty = signal["position_size"]
        is_scaled = scaled_cfg.enabled and scaled_cfg.legs > 1 and total_qty >= 2

        if is_scaled:
            # Scaled entry: split into two legs
            leg1_qty = math.ceil(total_qty * 0.5)
            leg2_qty = total_qty - leg1_qty

            # Leg 1: market fill with slippage
            if signal["signal_type"] == "BUY":
                leg1_fill = entry * (1 + slippage_pct)
            else:
                leg1_fill = entry * (1 - slippage_pct)

            # Wait for second leg
            await asyncio.sleep(scaled_cfg.second_leg_delay_sec)

            # Leg 2: simulate limit fill at offset price
            offset = scaled_cfg.second_leg_offset_pct
            if signal["signal_type"] == "BUY":
                leg2_fill = entry * (1 - offset)  # limit below market for BUY
            else:
                leg2_fill = entry * (1 + offset)  # limit above market for SELL

            # Average fill across both legs
            fill_price = (leg1_fill * leg1_qty + leg2_fill * leg2_qty) / total_qty
            actual_qty = total_qty

            logger.info(
                "trade-execute: PAPER scaled entry %s %s leg1=%d@%.2f leg2=%d@%.2f avg=%.2f",
                signal["signal_type"], signal["symbol"],
                leg1_qty, leg1_fill, leg2_qty, leg2_fill, fill_price,
            )
        else:
            # Standard single-order fill
            if signal["signal_type"] == "BUY":
                fill_price = entry * (1 + slippage_pct)
            else:
                fill_price = entry * (1 - slippage_pct)
            actual_qty = total_qty

        slippage = abs(fill_price - entry)

        # Estimate transaction costs for realistic paper PnL
        product = signal.get("product", "MIS")
        est_costs = compute_transaction_costs(
            fill_price, signal["target_price"], actual_qty,
            product=product, cost_config=self.ctx.config.transaction_costs,
        )

        trade = {
            "symbol": signal["symbol"],
            "signal_type": signal["signal_type"],
            "entry_price": entry,
            "fill_price": round(fill_price, 2),
            "quantity": actual_qty,
            "stop_loss_price": signal["stop_loss_price"],
            "target_price": signal["target_price"],
            "product": signal.get("product", "MIS"),
            "status": "open",
            "mode": "paper",
            "slippage": round(slippage, 2),
            "estimated_costs": est_costs,
            "expected_holding_days": signal.get("expected_holding_days"),
        }

        if is_scaled:
            trade["scaled_entry"] = True

        trade_id = await self.ctx.db.insert_trade(trade)
        trade["trade_id"] = trade_id
        await self.ctx.notify.send_trade_alert(trade)
        await self.broadcast("trade_executed", {
            "symbol": trade["symbol"],
            "signal_type": trade["signal_type"],
            "fill_price": trade["fill_price"],
            "quantity": trade["quantity"],
            "mode": "paper",
            "trade_id": trade_id,
        })

        logger.info(
            "trade-execute: PAPER %s %s qty=%d fill=%.2f slippage=%.2f (id=%s)%s",
            trade["signal_type"], trade["symbol"], trade["quantity"],
            trade["fill_price"], trade["slippage"], trade_id,
            " [scaled]" if is_scaled else "",
        )

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

        logger.info(
            "trade-execute: LIVE START %s %s qty=%s product=%s entry=%.2f",
            signal.get("signal_type"), signal.get("symbol"),
            signal.get("position_size"), product, signal.get("entry_price", 0),
        )

        # Idempotency check: prevent duplicate orders on crash/restart.
        # Uses agent_memory with a TTL to track in-flight executions.
        dedup_key = _signal_dedup_key(signal)
        if self.ctx.memory:
            existing = await self.ctx.memory.get("trade_dedup", dedup_key)
            if existing:
                logger.warning(
                    "trade-execute: DUPLICATE detected for %s %s (dedup=%s) — skipping",
                    signal["signal_type"], signal["symbol"], dedup_key,
                )
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={"skipped": True, "reason": "duplicate_signal", "dedup_key": dedup_key},
                )
            # Mark as in-flight BEFORE placing the order
            await self.ctx.memory.set("trade_dedup", dedup_key, "in_flight", ttl_hours=24)

        # Use fresh LTP for order price
        try:
            order_price = await self.ctx.market_data.get_ltp(signal["symbol"])
            drift = abs(order_price - signal["entry_price"]) / signal["entry_price"]
            if drift > cfg.price_drift_max_pct:
                logger.warning(
                    "trade-execute: price drift %.1f%% for %s (signal=%.2f, ltp=%.2f), rejecting",
                    drift * 100, signal["symbol"], signal["entry_price"], order_price,
                )
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={
                        "rejected": True,
                        "reason": f"price_drift_{drift:.1%}",
                        "signal_price": signal["entry_price"],
                        "current_price": order_price,
                    },
                )
        except Exception:
            logger.debug("LTP unavailable for live trade %s, using signal price", signal["symbol"])
            order_price = signal["entry_price"]

        scaled_cfg = cfg.scaled_entry
        total_qty = signal["position_size"]
        is_scaled = scaled_cfg.enabled and scaled_cfg.legs > 1 and total_qty >= 2

        for attempt in range(cfg.max_order_retries + 1):
            try:
                if is_scaled:
                    # --- Scaled entry: two-leg order placement ---
                    leg1_qty = math.ceil(total_qty * 0.5)
                    leg2_qty = total_qty - leg1_qty
                    side = "BUY" if signal["signal_type"] == "BUY" else "SELL"
                    sl_side = "SELL" if signal["signal_type"] == "BUY" else "BUY"

                    # Leg 1: place at market price
                    leg1_order_id = await self.ctx.broker.place_order(
                        symbol=signal["symbol"],
                        side=side,
                        quantity=leg1_qty,
                        order_type="LIMIT",
                        price=order_price,
                        product=product,
                    )

                    # Wait for first leg to fill
                    await asyncio.sleep(0.5)
                    leg1_status = await self.ctx.broker.get_order_status(leg1_order_id)
                    leg1_filled = leg1_status.get("filled_quantity") or 0
                    leg1_fill_price = leg1_status.get("average_price") or order_price

                    if leg1_filled == 0:
                        # First leg didn't fill — fall back to market order
                        await self.ctx.broker.cancel_order(leg1_order_id)
                        leg1_order_id = await self.ctx.broker.place_order(
                            symbol=signal["symbol"],
                            side=side,
                            quantity=leg1_qty,
                            order_type="MARKET",
                            product=product,
                        )
                        await asyncio.sleep(1)
                        leg1_status = await self.ctx.broker.get_order_status(leg1_order_id)
                        leg1_filled = leg1_status.get("filled_quantity") or leg1_qty
                        leg1_fill_price = leg1_status.get("average_price") or order_price

                    # Wait before placing second leg
                    await asyncio.sleep(scaled_cfg.second_leg_delay_sec)

                    # Leg 2: place limit order at offset price
                    offset = scaled_cfg.second_leg_offset_pct
                    if signal["signal_type"] == "BUY":
                        leg2_price = round(order_price * (1 - offset), 2)
                    else:
                        leg2_price = round(order_price * (1 + offset), 2)

                    leg2_order_id = await self.ctx.broker.place_order(
                        symbol=signal["symbol"],
                        side=side,
                        quantity=leg2_qty,
                        order_type="LIMIT",
                        price=leg2_price,
                        product=product,
                    )

                    # Wait for second leg fill within order_timeout
                    leg2_filled = 0
                    leg2_fill_price = leg2_price
                    for _ in range(cfg.order_timeout_sec):
                        await asyncio.sleep(1)
                        leg2_status = await self.ctx.broker.get_order_status(leg2_order_id)
                        leg2_filled = leg2_status.get("filled_quantity") or 0
                        if leg2_filled >= leg2_qty:
                            leg2_fill_price = leg2_status.get("average_price") or leg2_price
                            break

                    if leg2_filled < leg2_qty:
                        # Second leg didn't fill — cancel and proceed with leg 1 only
                        await self.ctx.broker.cancel_order(leg2_order_id)
                        logger.info(
                            "trade-execute: LIVE scaled leg2 unfilled for %s, proceeding with leg1 only",
                            signal["symbol"],
                        )
                        actual_qty = leg1_filled if leg1_filled > 0 else leg1_qty
                        fill_price = leg1_fill_price
                        order_id = leg1_order_id
                    else:
                        # Both legs filled — compute weighted average price
                        actual_qty = leg1_filled + leg2_filled
                        fill_price = (
                            leg1_fill_price * leg1_filled + leg2_fill_price * leg2_filled
                        ) / actual_qty
                        order_id = leg1_order_id  # primary order for tracking

                    # Place SL-M (stop-loss market) order for actual filled quantity
                    sl_order_id = await self.ctx.broker.place_order(
                        symbol=signal["symbol"],
                        side=sl_side,
                        quantity=actual_qty,
                        order_type="SL-M",
                        trigger_price=signal["stop_loss_price"],
                        product=product,
                    )

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
                        "status": "open",
                        "mode": "live",
                        "slippage": slippage,
                        "scaled_entry": True,
                    }

                    # Verify the primary order
                    verified_status = await self._verify_fill(order_id, timeout_sec=5)
                    if verified_status in ("REJECTED", "CANCELLED"):
                        logger.error(
                            "trade-execute: scaled leg1 order %s was %s for %s — cancelling SL",
                            order_id, verified_status, signal["symbol"],
                        )
                        await self.ctx.broker.cancel_order(sl_order_id)
                        await self.ctx.notify.send(
                            f"Scaled order REJECTED/CANCELLED for {signal['symbol']} "
                            f"(order={order_id}, status={verified_status})",
                            alert_type="errors",
                        )
                        raise RuntimeError(
                            f"Order {order_id} {verified_status} by exchange"
                        )

                    # COMPLETE/filled from Kite means the order filled — position is "open"
                    trade["status"] = "open"

                    logger.info(
                        "trade-execute: LIVE scaled %s %s leg1=%d@%.2f leg2=%d@%.2f avg=%.2f (id=%s)",
                        signal["signal_type"], signal["symbol"],
                        leg1_filled, leg1_fill_price,
                        leg2_filled if leg2_filled >= leg2_qty else 0, leg2_fill_price,
                        fill_price, order_id,
                    )

                else:
                    # --- Standard single-order placement ---
                    # Place primary order
                    order_id = await self.ctx.broker.place_order(
                        symbol=signal["symbol"],
                        side="BUY" if signal["signal_type"] == "BUY" else "SELL",
                        quantity=signal["position_size"],
                        order_type="LIMIT",
                        price=order_price,
                        product=product,
                    )

                    # Place stop-loss order
                    sl_order_id = await self.ctx.broker.place_order(
                        symbol=signal["symbol"],
                        side="SELL" if signal["signal_type"] == "BUY" else "BUY",
                        quantity=signal["position_size"],
                        order_type="SL-M",
                        trigger_price=signal["stop_loss_price"],
                        product=product,
                    )

                    # Track order status, handle partial fills
                    await asyncio.sleep(0.5)  # brief wait for fill
                    order_status = await self.ctx.broker.get_order_status(order_id)

                    # Check for partial fill within timeout
                    filled_qty = order_status.get("filled_quantity") or 0
                    if filled_qty < signal["position_size"]:
                        # Wait up to order_timeout_sec for full fill
                        for _ in range(cfg.order_timeout_sec):
                            await asyncio.sleep(1)
                            order_status = await self.ctx.broker.get_order_status(order_id)
                            filled_qty = order_status.get("filled_quantity") or 0
                            if filled_qty >= signal["position_size"]:
                                break

                        if filled_qty < signal["position_size"]:
                            await self.ctx.broker.cancel_order(order_id)

                            if filled_qty == 0:
                                # Zero fills — retry with MARKET order for guaranteed execution
                                logger.warning(
                                    "trade-execute: LIMIT order unfilled for %s, retrying with MARKET",
                                    signal["symbol"],
                                )
                                order_id = await self.ctx.broker.place_order(
                                    symbol=signal["symbol"],
                                    side="BUY" if signal["signal_type"] == "BUY" else "SELL",
                                    quantity=signal["position_size"],
                                    order_type="MARKET",
                                    product=product,
                                )
                                await asyncio.sleep(1)
                                order_status = await self.ctx.broker.get_order_status(order_id)
                                filled_qty = order_status.get("filled_quantity") or signal["position_size"]
                            else:
                                # Partial fill — adjust SL order to match filled quantity
                                await self.ctx.broker.cancel_order(sl_order_id)
                                sl_order_id = await self.ctx.broker.place_order(
                                    symbol=signal["symbol"],
                                    side="SELL" if signal["signal_type"] == "BUY" else "BUY",
                                    quantity=filled_qty,
                                    order_type="SL-M",
                                    trigger_price=signal["stop_loss_price"],
                                    product=product,
                                )

                    actual_qty = filled_qty if filled_qty > 0 else signal["position_size"]

                    # Compute slippage — use entry price if avg_price is 0/None (unfilled)
                    fill_price = order_status.get("average_price") or signal["entry_price"]
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
                        "status": "open",  # position is open until target/SL/square-off closes it
                        "mode": "live",
                        "slippage": slippage,
                    }

                    # Final fill verification: confirm order is in a terminal state
                    verified_status = await self._verify_fill(order_id, timeout_sec=5)
                    if verified_status in ("REJECTED", "CANCELLED"):
                        logger.error(
                            "trade-execute: order %s was %s after placement for %s — "
                            "cancelling SL order",
                            order_id, verified_status, signal["symbol"],
                        )
                        await self.ctx.broker.cancel_order(sl_order_id)
                        await self.ctx.notify.send(
                            f"Order REJECTED/CANCELLED for {signal['symbol']} "
                            f"(order={order_id}, status={verified_status})",
                            alert_type="errors",
                        )
                        raise RuntimeError(
                            f"Order {order_id} {verified_status} by exchange"
                        )

                    # COMPLETE/filled from Kite means the order filled — position is "open"
                    trade["status"] = "open"

                trade_id = await self.ctx.db.insert_trade(trade)
                trade["trade_id"] = trade_id
                await self.ctx.notify.send_trade_alert(trade)
                await self.broadcast("trade_executed", {
                    "symbol": trade["symbol"],
                    "signal_type": trade["signal_type"],
                    "fill_price": trade["fill_price"],
                    "quantity": trade["quantity"],
                    "slippage": trade["slippage"],
                    "mode": "live",
                    "trade_id": trade_id,
                })

                logger.info(
                    "trade-execute: LIVE %s %s qty=%d fill=%.2f slippage=%.2f "
                    "attempt=%d status=%s (id=%s, order=%s)%s",
                    trade["signal_type"], trade["symbol"], trade["quantity"],
                    trade["fill_price"], trade["slippage"], attempt + 1, trade["status"],
                    trade_id, trade.get("order_id", "N/A"),
                    " [scaled]" if trade.get("scaled_entry") else "",
                )

                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={"trade": trade, "mode": "live", "attempts": attempt + 1},
                )

            except Exception as e:
                last_error = e
                logger.warning(
                    "trade-execute: %s %s attempt %d failed: %s",
                    signal["signal_type"], signal["symbol"], attempt + 1, e,
                )
                # CRITICAL: Before retrying, check if the "failed" order actually
                # went through on the broker. Zerodha sometimes returns errors
                # AFTER placing the order, causing duplicate orders on retry.
                if attempt < cfg.max_order_retries and self.ctx.config.mode == "live":
                    try:
                        recent_orders = await asyncio.to_thread(self.ctx.broker._kite.orders)
                        symbol_orders = [
                            o for o in (recent_orders or [])
                            if o.get("tradingsymbol") == signal["symbol"]
                            and o.get("status") in ("COMPLETE", "OPEN", "TRIGGER PENDING")
                            and o.get("transaction_type") == ("BUY" if signal["signal_type"] == "BUY" else "SELL")
                        ]
                        # Check for orders placed in the last 2 minutes
                        from datetime import datetime, timedelta
                        cutoff = datetime.now() - timedelta(minutes=2)
                        recent = [
                            o for o in symbol_orders
                            if o.get("order_timestamp") and o["order_timestamp"] > cutoff
                        ]
                        if recent:
                            logger.error(
                                "trade-execute: ABORT RETRY — found %d recent %s orders for %s on broker "
                                "despite error. The 'failed' order likely executed. Not retrying.",
                                len(recent), signal["signal_type"], signal["symbol"],
                            )
                            break
                    except Exception:
                        logger.debug("Could not check broker orders before retry", exc_info=True)

                    delay = cfg.retry_base_delay_sec * (2**attempt)
                    await asyncio.sleep(delay)

        logger.error(
            "trade-execute: FAILED %s %s after %d attempts: %s",
            signal["signal_type"], signal["symbol"],
            cfg.max_order_retries + 1, last_error,
        )

        return SkillResult(
            success=False,
            skill_name=self.name,
            error=f"Order failed after {cfg.max_order_retries + 1} attempts: {last_error}",
        )

    async def _verify_fill(self, order_id: str, timeout_sec: int = 5) -> str:
        """Poll order status until it reaches a terminal state.

        Terminal states: COMPLETE, CANCELLED, REJECTED.
        Non-terminal: OPEN, PENDING, PUT ORDER REQ RECEIVED, etc.

        Returns the terminal status string, or "COMPLETE" if timeout reached
        (assume filled — broker reconciliation will catch mismatches).
        """
        terminal = {"COMPLETE", "CANCELLED", "REJECTED", "filled"}
        for _ in range(timeout_sec):
            try:
                status = await self.ctx.broker.get_order_status(order_id)
                order_state = status.get("status", "").upper()
                if order_state in terminal:
                    return order_state
            except Exception as e:
                logger.warning("Fill verification poll failed for %s: %s", order_id, e)
            await asyncio.sleep(1)
        # Timeout — assume filled; ghost recovery will catch mismatches
        return "COMPLETE"
