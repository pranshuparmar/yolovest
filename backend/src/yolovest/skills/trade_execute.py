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
from yolovest.data.db import DuplicateSignalError
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)


def _reanchor_levels(
    signal: dict[str, Any], fill_price: float,
) -> tuple[float, float, float]:
    """Shift `target_price` and `stop_loss_price` by the entry-slippage
    delta so they sit at the same ATR-multiplier distance from the
    actual fill that they sat from the predicted entry.

    Without this, a small adverse slippage (e.g. SELL filled 0.2%
    below the predicted entry) silently flips the trade's intended
    R:R — the SL distance widens and the target distance shrinks even
    though the model's geometry was 2:1.

    Returns (new_target, new_stop_loss, delta). When fill_price isn't
    available (zero / non-finite / no slippage), returns the original
    levels and delta=0 so the caller can skip any modify_order step.
    """
    try:
        entry = float(signal.get("entry_price") or 0)
        fill = float(fill_price or 0)
        target = float(signal.get("target_price") or 0)
        sl = float(signal.get("stop_loss_price") or 0)
    except (TypeError, ValueError):
        return (
            float(signal.get("target_price") or 0),
            float(signal.get("stop_loss_price") or 0),
            0.0,
        )
    if entry <= 0 or fill <= 0 or target <= 0 or sl <= 0:
        return target, sl, 0.0
    delta = fill - entry
    if delta == 0:
        return target, sl, 0.0
    return round(target + delta, 2), round(sl + delta, 2), delta


def _signal_dedup_key(signal: dict[str, Any], mode: str = "paper") -> str:
    """Generate a dedup key for a signal to prevent duplicate order placement.

    Key components: mode + symbol + signal_type + date + entry_price.
    Mode is part of the key so a paper test in the morning doesn't
    block a live execution of the same setup that afternoon (or vice
    versa). If the process crashes after placing a broker order but
    before recording the trade, the same signal re-entering this
    skill in the same mode will still be detected.
    """
    parts = (
        mode,
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

        # Reanchor target/SL to the actual fill so the simulated trade
        # is managed at the same ATR-distance from the fill that the
        # model intended from the predicted entry.
        reanchored_target, reanchored_sl, _ = _reanchor_levels(
            {**signal, "entry_price": entry}, fill_price,
        )

        # Estimate transaction costs for realistic paper PnL
        product = signal.get("product", "MIS")
        est_costs = compute_transaction_costs(
            fill_price, reanchored_target, actual_qty,
            product=product, cost_config=self.ctx.config.transaction_costs,
        )

        trade = {
            "symbol": signal["symbol"],
            "signal_type": signal["signal_type"],
            "signal_id": signal.get("signal_id"),
            "model_version": signal.get("model_version"),
            "entry_price": entry,
            "fill_price": round(fill_price, 2),
            "quantity": actual_qty,
            "stop_loss_price": reanchored_sl,
            "target_price": reanchored_target,
            "product": signal.get("product", "MIS"),
            "status": "open",
            "mode": "paper",
            "slippage": round(slippage, 2),
            "estimated_costs": est_costs,
            "expected_holding_days": signal.get("expected_holding_days"),
        }

        if is_scaled:
            trade["scaled_entry"] = True

        try:
            trade_id = await self.ctx.db.insert_trade(trade)
        except DuplicateSignalError as exc:
            logger.warning(
                "trade-execute: PAPER signal_id=%d already produced trade %s — "
                "skipping (DB UNIQUE caught the retry)",
                exc.signal_id, exc.existing_trade_id,
            )
            return SkillResult(
                success=True, skill_name=self.name,
                data={
                    "skipped": True, "reason": "duplicate_signal_db",
                    "signal_id": exc.signal_id,
                    "existing_trade_id": exc.existing_trade_id,
                },
            )
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
        """Place real orders via Kite Connect.

        Retry shell: preconditions (dedup + price drift) -> place entry
        (scaled or single) -> persist + arm broker-side exits. On any
        attempt failure, first check whether the "failed" order actually
        landed at the broker and reconcile it instead of retrying.
        """
        cfg = self.ctx.config.execution
        last_error = None
        product = signal.get("product", "MIS")

        logger.info(
            "trade-execute: LIVE START %s %s qty=%s product=%s entry=%.2f",
            signal.get("signal_type"), signal.get("symbol"),
            signal.get("position_size"), product, signal.get("entry_price", 0),
        )

        early, order_price = await self._check_live_preconditions(signal, cfg)
        if early is not None:
            return early

        scaled_cfg = cfg.scaled_entry
        total_qty = signal["position_size"]
        is_scaled = scaled_cfg.enabled and scaled_cfg.legs > 1 and total_qty >= 2

        for attempt in range(cfg.max_order_retries + 1):
            try:
                if is_scaled:
                    trade = await self._place_scaled_entry(
                        signal, order_price, product, scaled_cfg, cfg,
                    )
                else:
                    trade = await self._place_single_entry(
                        signal, order_price, product, cfg,
                    )
                return await self._persist_and_arm_exits(
                    signal, trade, product, attempt,
                )

            except Exception as e:
                last_error = e
                logger.warning(
                    "trade-execute: %s %s attempt %d failed: %s",
                    signal["signal_type"], signal["symbol"], attempt + 1, e,
                )
                # CRITICAL: Before retrying, check if the "failed" order actually
                # went through on the broker. Zerodha sometimes returns errors
                # AFTER placing the order — if we retry naively we'd create
                # a duplicate. Worse, if the broker order succeeded and we
                # simply skip retry, the calling code sees success=False and
                # leaves the pending trade un-reconciled while the actual
                # position exists on Kite. Reconcile instead: adopt the
                # surviving order as our trade record.
                if self.ctx.config.mode == "live":
                    recovered = await self._find_recently_placed_order(signal)
                    if recovered is not None:
                        return await self._reconcile_recovered_order(
                            signal, recovered, product, last_error,
                        )

                if attempt < cfg.max_order_retries and self.ctx.config.mode == "live":
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

    async def _check_live_preconditions(
        self, signal: dict[str, Any], cfg: Any,
    ) -> tuple[SkillResult | None, float]:
        """Crash-restart dedup + entry-price drift check. Returns
        (early-exit SkillResult or None, order_price to place at)."""
        # Idempotency check: prevent duplicate orders on crash/restart.
        # Uses agent_memory with a TTL to track in-flight executions.
        dedup_key = _signal_dedup_key(signal, mode=self.ctx.config.mode)
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
                ), 0.0
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
                ), order_price
        except Exception:
            logger.debug("LTP unavailable for live trade %s, using signal price", signal["symbol"])
            order_price = signal["entry_price"]

        return None, order_price

    async def _place_scaled_entry(
        self,
        signal: dict[str, Any],
        order_price: float,
        product: str,
        scaled_cfg: Any,
        cfg: Any,
    ) -> dict[str, Any]:
        """Two-leg scaled entry: leg1 at market price (LIMIT with MARKET
        fallback), leg2 as a resting LIMIT at an offset; SL placed for the
        actual filled quantity, levels reanchored to the realised fill.
        Returns the trade dict; raises on exchange rejection."""
        total_qty = signal["position_size"]
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
            tag="yv-entry-l1",
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
                tag="yv-entry-l1",
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
            tag="yv-entry-l2",
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

        # Reanchor target/SL to the actual fill so the
        # resting SL and the downstream GTT/MIS-OCO target
        # sit at the same ATR-distance from the fill that
        # the model intended from the predicted entry.
        reanchored_target, reanchored_sl, delta = _reanchor_levels(
            signal, fill_price,
        )
        if delta:
            logger.info(
                "trade-execute: reanchored levels for %s by ₹%.2f "
                "(entry %.2f → fill %.2f): SL %.2f → %.2f, target %.2f → %.2f",
                signal["symbol"], delta,
                signal["entry_price"], fill_price,
                signal["stop_loss_price"], reanchored_sl,
                signal["target_price"], reanchored_target,
            )

        # Place SL-M (stop-loss market) order for actual filled quantity
        sl_order_id = await self.ctx.broker.place_order(
            symbol=signal["symbol"],
            side=sl_side,
            quantity=actual_qty,
            order_type="SL-M",
            trigger_price=reanchored_sl,
            product=product,
            tag="yv-sl",
        )

        slippage = abs(fill_price - signal["entry_price"])

        trade = {
            "symbol": signal["symbol"],
            "signal_type": signal["signal_type"],
            "signal_id": signal.get("signal_id"),
            "model_version": signal.get("model_version"),
            "entry_price": signal["entry_price"],
            "fill_price": fill_price,
            "quantity": actual_qty,
            "stop_loss_price": reanchored_sl,
            "target_price": reanchored_target,
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

        return trade

    async def _place_single_entry(
        self,
        signal: dict[str, Any],
        order_price: float,
        product: str,
        cfg: Any,
    ) -> dict[str, Any]:
        """Standard LIMIT entry + SL pair with partial-fill handling
        (timeout -> MARKET fallback on zero fill, SL resize on partial)
        and fill-reanchored levels. Returns the trade dict; raises on
        exchange rejection."""
        # --- Standard single-order placement ---
        # Place primary order
        order_id = await self.ctx.broker.place_order(
            symbol=signal["symbol"],
            side="BUY" if signal["signal_type"] == "BUY" else "SELL",
            quantity=signal["position_size"],
            order_type="LIMIT",
            price=order_price,
            product=product,
            tag="yv-entry",
        )

        # Place stop-loss order
        sl_order_id = await self.ctx.broker.place_order(
            symbol=signal["symbol"],
            side="SELL" if signal["signal_type"] == "BUY" else "BUY",
            quantity=signal["position_size"],
            order_type="SL-M",
            trigger_price=signal["stop_loss_price"],
            product=product,
            tag="yv-sl",
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
                        tag="yv-entry",
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
                        tag="yv-sl",
                    )

        actual_qty = filled_qty if filled_qty > 0 else signal["position_size"]

        # Compute slippage — use entry price if avg_price is 0/None (unfilled)
        fill_price = order_status.get("average_price") or signal["entry_price"]
        slippage = abs(fill_price - signal["entry_price"])

        # Reanchor target/SL to the actual fill. The SL was
        # placed before the fill was known, so modify it in
        # place via kite.modify_order. Target is enforced
        # downstream by GTT/MIS-OCO using the trade dict's
        # target_price below.
        reanchored_target, reanchored_sl, delta = _reanchor_levels(
            signal, fill_price,
        )
        if delta and sl_order_id:
            try:
                await self.ctx.broker.modify_sl_order(
                    sl_order_id, reanchored_sl,
                )
                logger.info(
                    "trade-execute: reanchored levels for %s by ₹%.2f "
                    "(entry %.2f → fill %.2f): SL %.2f → %.2f, "
                    "target %.2f → %.2f",
                    signal["symbol"], delta,
                    signal["entry_price"], fill_price,
                    signal["stop_loss_price"], reanchored_sl,
                    signal["target_price"], reanchored_target,
                )
            except Exception:
                # Modify failed — fall back to original SL.
                # Position-monitor's client-side detection
                # remains a safety net so we're not
                # exposed; just log loudly.
                logger.warning(
                    "trade-execute: failed to reanchor SL for %s "
                    "(order_id=%s); keeping original %.2f",
                    signal["symbol"], sl_order_id,
                    signal["stop_loss_price"], exc_info=True,
                )
                reanchored_sl = signal["stop_loss_price"]
                reanchored_target = signal["target_price"]

        trade = {
            "symbol": signal["symbol"],
            "signal_type": signal["signal_type"],
            "signal_id": signal.get("signal_id"),
            "model_version": signal.get("model_version"),
            "entry_price": signal["entry_price"],
            "fill_price": fill_price,
            "quantity": actual_qty,
            "stop_loss_price": reanchored_sl,
            "target_price": reanchored_target,
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

        return trade

    async def _persist_and_arm_exits(
        self,
        signal: dict[str, Any],
        trade: dict[str, Any],
        product: str,
        attempt: int,
    ) -> SkillResult:
        """Insert the trade row (cancelling duplicate broker orders if the
        DB UNIQUE catches a signal replay), attach the broker-side exit
        (CNC GTT / MIS OCO), alert, broadcast, and return success."""
        try:
            trade_id = await self.ctx.db.insert_trade(trade)
        except DuplicateSignalError as exc:
            # The signal_id already attaches to an existing
            # trade — this LIVE execution is a duplicate. Best
            # effort: cancel the broker order(s) we just placed
            # so the position doesn't get doubled. The existing
            # trade row still tracks the original execution.
            logger.error(
                "trade-execute: LIVE signal_id=%d already produced trade %s — "
                "cancelling duplicate broker orders %s / %s",
                exc.signal_id, exc.existing_trade_id,
                trade.get("order_id"), trade.get("sl_order_id"),
            )
            for oid in (trade.get("order_id"), trade.get("sl_order_id")):
                if oid:
                    try:
                        await self.ctx.broker.cancel_order(oid)
                    except Exception:
                        logger.warning(
                            "trade-execute: failed to cancel duplicate "
                            "order %s", oid, exc_info=True,
                        )
            try:
                await self.ctx.notify.send(
                    f"Duplicate signal execution caught at DB UNIQUE — "
                    f"signal_id={exc.signal_id} maps to trade "
                    f"{exc.existing_trade_id}; cancelled broker orders "
                    f"{trade.get('order_id')} / {trade.get('sl_order_id')}",
                    alert_type="errors",
                )
            except Exception:
                pass
            return SkillResult(
                success=True, skill_name=self.name,
                data={
                    "skipped": True, "reason": "duplicate_signal_db",
                    "signal_id": exc.signal_id,
                    "existing_trade_id": exc.existing_trade_id,
                },
            )
        trade["trade_id"] = trade_id

        # For CNC trades, attach a broker-side OCO GTT for target +
        # stoploss. Kite only allows GTT on CNC — MIS positions
        # get a resting LIMIT order at target instead, with
        # position-monitor enforcing OCO across SL and target.
        if product == "CNC":
            await self._attach_oco_gtt(trade)
        elif product == "MIS":
            await self._attach_mis_target_limit(trade)

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


    async def _find_recently_placed_order(
        self, signal: dict[str, Any], window_minutes: int = 2,
    ) -> dict[str, Any] | None:
        """Return the most recent matching broker order for this signal,
        or None if no candidate exists.

        Matches by tradingsymbol + transaction_type + recent timestamp.
        Prefers COMPLETE > OPEN > TRIGGER PENDING.
        """
        try:
            recent_orders = await asyncio.to_thread(self.ctx.broker._kite.orders)
        except Exception:
            logger.debug("Could not list broker orders for reconciliation", exc_info=True)
            return None

        want_side = "BUY" if signal["signal_type"] == "BUY" else "SELL"
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(minutes=window_minutes)

        candidates = []
        for o in recent_orders or []:
            if o.get("tradingsymbol") != signal["symbol"]:
                continue
            if o.get("transaction_type") != want_side:
                continue
            if o.get("status") not in ("COMPLETE", "OPEN", "TRIGGER PENDING"):
                continue
            ts = o.get("order_timestamp")
            if ts and ts > cutoff:
                candidates.append(o)

        if not candidates:
            return None

        # Prefer COMPLETE, then most recent
        status_rank = {"COMPLETE": 0, "OPEN": 1, "TRIGGER PENDING": 2}
        candidates.sort(key=lambda o: (
            status_rank.get(o.get("status"), 99),
            -(o["order_timestamp"].timestamp() if o.get("order_timestamp") else 0),
        ))
        return candidates[0]

    async def _reconcile_recovered_order(
        self,
        signal: dict[str, Any],
        order: dict[str, Any],
        product: str,
        last_error: Exception | None,
    ) -> SkillResult:
        """Adopt a broker order that was placed despite our place_order call
        raising — record it as a successful trade so the pending queue
        doesn't get stuck and the position is tracked.

        SL order is NOT auto-placed here even if the original SL-leg failed;
        position-monitor will detect the unmanaged position and either set
        SL via its trailing logic or surface it for manual intervention.
        """
        symbol = signal["symbol"]
        order_id = str(order.get("order_id") or "")
        fill_price = float(order.get("average_price") or signal["entry_price"] or 0)
        actual_qty = int(order.get("filled_quantity") or signal["position_size"])
        slippage = abs(fill_price - signal["entry_price"])

        logger.warning(
            "trade-execute: RECONCILED %s %s — broker order %s status=%s qty=%d fill=%.2f "
            "(place_order raised %s, but order actually went through)",
            signal["signal_type"], symbol, order_id, order.get("status"),
            actual_qty, fill_price, type(last_error).__name__ if last_error else "n/a",
        )

        trade = {
            "symbol": symbol,
            "signal_type": signal["signal_type"],
            "signal_id": signal.get("signal_id"),
            "model_version": signal.get("model_version"),
            "entry_price": signal["entry_price"],
            "fill_price": fill_price,
            "quantity": actual_qty,
            "stop_loss_price": signal["stop_loss_price"],
            "target_price": signal["target_price"],
            "order_id": order_id,
            "sl_order_id": None,  # SL leg not separately tracked on reconcile
            "product": product,
            "status": "open",
            "mode": "live",
            "slippage": slippage,
            "origin": "system",
        }
        try:
            trade_id = await self.ctx.db.insert_trade(trade)
        except DuplicateSignalError as exc:
            # Reconcile path also competes with the normal path. If
            # the signal_id is already attached to a trade, the
            # existing row already covers the broker order we
            # rediscovered — nothing to do here.
            logger.warning(
                "trade-execute: reconcile saw broker order for signal_id=%d, "
                "but trade %s already attached — leaving as-is",
                exc.signal_id, exc.existing_trade_id,
            )
            return SkillResult(
                success=True, skill_name=self.name,
                data={
                    "skipped": True, "reason": "duplicate_signal_db",
                    "signal_id": exc.signal_id,
                    "existing_trade_id": exc.existing_trade_id,
                },
            )
        trade["trade_id"] = trade_id
        try:
            await self.ctx.notify.send_trade_alert(trade)
        except Exception:
            logger.debug("Failed to notify on reconciled trade", exc_info=True)
        try:
            await self.ctx.notify.send(
                f"⚠️ Reconciled {signal['signal_type']} {symbol} — entry filled at "
                f"₹{fill_price} but SL leg failed. position-monitor will manage SL.",
                alert_type="errors",
            )
        except Exception:
            logger.debug("Failed to send reconcile alert", exc_info=True)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"trade": trade, "mode": "live", "reconciled": True},
        )

    # Zerodha caps active GTTs at 50 per account. When we're within
    # this margin, skip new GTT placement and fall back to client-side
    # detection so the position still gets exit coverage.
    _GTT_SLOT_WARN_THRESHOLD = 45

    @staticmethod
    def _validate_gtt_params(
        exit_side: str,
        sl_trig: float,
        tgt_trig: float,
        last_price: float,
        quantity: int,
    ) -> str | None:
        """Pre-flight validation for an OCO GTT. Returns an error message
        if the parameters are nonsense, None if they're OK. Catches the
        common bugs (SL on wrong side, target on wrong side, crossed
        legs, zero qty) before we waste an API call on something Kite
        will reject anyway."""
        if quantity <= 0:
            return "quantity must be positive"
        if sl_trig <= 0 or tgt_trig <= 0 or last_price <= 0:
            return "prices must be positive"
        if exit_side == "SELL":  # closing a long
            if sl_trig >= last_price:
                return f"long-exit SL trigger {sl_trig:.2f} must be < LTP {last_price:.2f}"
            if tgt_trig <= last_price:
                return f"long-exit target trigger {tgt_trig:.2f} must be > LTP {last_price:.2f}"
        else:  # closing a short
            if sl_trig <= last_price:
                return f"short-exit SL trigger {sl_trig:.2f} must be > LTP {last_price:.2f}"
            if tgt_trig >= last_price:
                return f"short-exit target trigger {tgt_trig:.2f} must be < LTP {last_price:.2f}"
        if sl_trig == tgt_trig:
            return "SL and target triggers cannot be equal"
        return None

    async def _attach_oco_gtt(self, trade: dict[str, Any]) -> None:
        """Place a two-leg OCO GTT (stoploss + target) for a freshly-filled
        CNC trade. Records the broker's trigger_id on the trade row.

        GTT failure is non-fatal — the trade itself succeeded; position-
        monitor's client-side detection still provides exit coverage.
        """
        broker = self.ctx.broker
        if not hasattr(broker, "place_oco_gtt"):
            return

        symbol = trade["symbol"]
        side = trade["signal_type"]
        # Exit side is the opposite of the entry side
        exit_side = "SELL" if side == "BUY" else "BUY"

        sl_trig = float(trade["stop_loss_price"])
        tgt_trig = float(trade["target_price"])
        last_price = float(trade.get("fill_price") or trade["entry_price"])
        qty = int(trade["quantity"])

        # Pre-flight validation — fail fast on obvious nonsense rather
        # than firing an API call we know Kite will reject.
        err = self._validate_gtt_params(exit_side, sl_trig, tgt_trig, last_price, qty)
        if err:
            logger.warning(
                "trade-execute: GTT params invalid for %s: %s — "
                "skipping GTT, client-side detection active",
                trade.get("trade_id"), err,
            )
            await self.ctx.db.log_gtt_event(
                trade_id=trade.get("trade_id"), gtt_id=None, symbol=symbol,
                event_type="rejected_placement",
                details={"reason": err, "stage": "validation"},
            )
            return

        # Slot-cap check — Zerodha allows ≤50 active GTTs per account.
        # When near the cap, skip placement (client-side exit takes over)
        # and notify so the user can clean up stale GTTs.
        if hasattr(broker, "get_gtts"):
            try:
                gtts = await broker.get_gtts()
                active = sum(
                    1 for g in (gtts or [])
                    if (g.get("status") or "").lower() == "active"
                )
                if active >= self._GTT_SLOT_WARN_THRESHOLD:
                    logger.warning(
                        "trade-execute: %d active GTTs at broker (cap 50) — "
                        "skipping new GTT for %s; client-side exit detection active",
                        active, trade.get("trade_id"),
                    )
                    await self.ctx.db.log_gtt_event(
                        trade_id=trade.get("trade_id"), gtt_id=None, symbol=symbol,
                        event_type="rejected_placement",
                        details={"reason": "slot_cap", "active_gtts": active},
                    )
                    return
            except Exception:
                logger.debug("Slot-cap probe failed; proceeding with GTT", exc_info=True)

        # Limit price for each leg sits past the trigger so the resulting
        # LIMIT order fills reliably once the trigger fires.
        buffer = 0.005  # 0.5%
        if exit_side == "SELL":  # closing a long
            sl_limit = sl_trig * (1 - buffer)
            tgt_limit = tgt_trig * (1 - buffer * 0.5)
        else:  # closing a short
            sl_limit = sl_trig * (1 + buffer)
            tgt_limit = tgt_trig * (1 + buffer * 0.5)

        try:
            gtt_id = await broker.place_oco_gtt(
                symbol=symbol,
                side=exit_side,
                quantity=qty,
                stoploss_trigger=sl_trig,
                stoploss_limit=sl_limit,
                target_trigger=tgt_trig,
                target_limit=tgt_limit,
                last_price=last_price,
            )
        except Exception as e:
            logger.warning(
                "trade-execute: GTT attach failed for %s (entry succeeded; "
                "client-side exit detection still active): %s",
                trade.get("trade_id"), e,
            )
            await self.ctx.db.log_gtt_event(
                trade_id=trade.get("trade_id"), gtt_id=None, symbol=symbol,
                event_type="rejected_placement",
                details={"reason": "broker_error", "error": str(e)},
            )
            return

        if gtt_id:
            trade["gtt_id"] = gtt_id
            try:
                await self.ctx.db.set_trade_gtt(trade["trade_id"], gtt_id)
            except Exception:
                logger.debug("Failed to persist gtt_id", exc_info=True)
            await self.ctx.db.log_gtt_event(
                trade_id=trade.get("trade_id"), gtt_id=gtt_id, symbol=symbol,
                event_type="placed", status="active",
                details={
                    "side": exit_side, "quantity": qty,
                    "sl_trigger": sl_trig, "target_trigger": tgt_trig,
                    "sl_limit": sl_limit, "target_limit": tgt_limit,
                },
            )

    async def _attach_mis_target_limit(self, trade: dict[str, Any]) -> None:
        """Place a resting LIMIT order at target for a freshly-filled MIS
        trade. Kite doesn't allow GTT on MIS, so we DIY an OCO: this LIMIT
        sits on the book; position-monitor cancels the SL when it fills,
        and cancels this when the SL fills.

        Failure is non-fatal — the trade itself succeeded; position-monitor
        falls back to client-side target detection (with the 0.15% buffer).
        """
        exit_side = "SELL" if trade["signal_type"] == "BUY" else "BUY"
        target_price = float(trade["target_price"])
        qty = int(trade["quantity"])
        try:
            target_order_id = await self.ctx.broker.place_order(
                symbol=trade["symbol"],
                side=exit_side,
                quantity=qty,
                order_type="LIMIT",
                price=target_price,
                product="MIS",
                tag="yv-tgt",
            )
        except Exception as e:
            logger.warning(
                "trade-execute: target LIMIT attach failed for %s (entry "
                "succeeded; heartbeat target detection still active): %s",
                trade.get("trade_id"), e,
            )
            return

        if target_order_id:
            trade["target_order_id"] = target_order_id
            try:
                await self.ctx.db.set_trade_target_order_id(
                    trade["trade_id"], target_order_id,
                )
            except Exception:
                logger.debug("Failed to persist target_order_id", exc_info=True)
            logger.info(
                "trade-execute: MIS target LIMIT placed for %s @ %.2f (order=%s)",
                trade["symbol"], target_price, target_order_id,
            )

    async def _verify_fill(self, order_id: str, timeout_sec: int = 5) -> str:
        """Poll order status until it reaches a terminal state.

        Terminal states: COMPLETE, CANCELLED, REJECTED.
        Non-terminal: OPEN, PENDING, PUT ORDER REQ RECEIVED, etc.

        Returns the terminal status string, or "COMPLETE" if timeout reached
        (assume filled — broker reconciliation will catch mismatches).

        A genuine timeout (polls succeeded but the order stayed non-terminal)
        is the documented "assume filled" case. But if EVERY poll raised —
        the broker was unreachable for the whole window — "could not verify"
        is indistinguishable from "filled" at the return value, so we alert
        loudly before returning COMPLETE. Ghost-recovery + postback are still
        the reconciliation backstops; this just makes the blind spot visible
        so the operator can verify the position manually.
        """
        terminal = {"COMPLETE", "CANCELLED", "REJECTED", "filled"}
        poll_succeeded = False
        for _ in range(timeout_sec):
            try:
                status = await self.ctx.broker.get_order_status(order_id)
                poll_succeeded = True
                order_state = status.get("status", "").upper()
                if order_state in terminal:
                    return order_state
            except Exception as e:
                logger.warning("Fill verification poll failed for %s: %s", order_id, e)
            await asyncio.sleep(1)
        if not poll_succeeded:
            # Broker unreachable for the entire window — assuming filled is a
            # guess, not a verification. Make it loud.
            logger.error(
                "trade-execute: ALL fill-verification polls failed for %s — "
                "broker unreachable; assuming filled. Verify the position "
                "manually; ghost-recovery will reconcile if it did not fill.",
                order_id,
            )
            try:
                await self.ctx.notify.send(
                    f"Could not verify fill for order {order_id} — broker "
                    f"unreachable during verification. Assuming filled; please "
                    f"verify the position manually.",
                    alert_type="errors",
                )
            except Exception:
                logger.debug("verify-fill alert send failed", exc_info=True)
        # Timeout — assume filled; ghost recovery will catch mismatches
        return "COMPLETE"
