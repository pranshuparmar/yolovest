"""Skill: position-monitor — Monitor open positions, trail SLs, reconcile.

Trigger: HEARTBEAT during market hours
Pipeline position: Runs continuously alongside generate-signals.

Flow:
1. Fetch current positions from broker
2. Reconcile broker state with local DB state — flag discrepancies
3. For each open position:
   a. Check if target hit → emit exit signal
   b. Check if SL hit → record loss
   c. Check trailing SL logic:
      - If profit >= trailing_sl_trigger_multiple x risk -> move SL to breakeven
      - Continue trailing SL upward in trailing_sl_step_pct increments
   d. Modify SL order on broker if trail triggered
4. Track unrealized PnL for portfolio state
5. Update slippage records
6. Alert on any discrepancies between local and broker state
"""

import asyncio
import logging
from typing import Any

from yolovest.costs import compute_transaction_costs
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class PositionMonitorSkill(SkillBase):
    name = "position-monitor"
    description = "Reconcile positions, trail stop-losses, track PnL"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_market_hours())

    async def execute(self, **kwargs: Any) -> SkillResult:
        from yolovest.timezone import now_ist

        cfg = self.ctx.config.risk
        local_positions = await self.ctx.db.get_open_positions()
        broker_positions = await self.ctx.broker.get_positions()

        discrepancies = self._reconcile(local_positions, broker_positions)

        # Recover ghost positions: local DB says open, broker says closed.
        # This happens when broker-side SL triggers or manual broker actions.
        recovered = await self._recover_ghost_positions(
            local_positions, broker_positions,
        )

        if discrepancies:
            await self.ctx.notify.send(
                f"Position discrepancy detected:\n"
                + "\n".join(discrepancies)
                + (f"\nAuto-recovered: {', '.join(recovered)}" if recovered else ""),
                alert_type="errors",
            )

        trails_modified = 0
        targets_hit: list[dict[str, Any]] = []
        stops_hit: list[dict[str, Any]] = []
        expiry_actions: list[dict[str, Any]] = []
        ltp_failures: list[str] = []

        # Skip positions that were just recovered (already closed in DB)
        recovered_set = set(recovered)

        # Load locked symbols — these should not be auto-sold (target/SL/trail)
        locked_symbols = await self.ctx.db.get_locked_symbols()

        for pos in local_positions:
            symbol = pos["symbol"]
            if symbol in recovered_set:
                continue

            # Fetch LTP with retry (positions must not go unmonitored)
            current_price = await self._get_ltp_with_retry(symbol)
            if current_price is None:
                ltp_failures.append(symbol)
                logger.error(
                    "position-monitor: LTP fetch failed for %s after retries — "
                    "position UNMONITORED this cycle",
                    symbol,
                )
                continue

            entry = pos["entry_price"]
            sl = pos["stop_loss_price"]
            target = pos["target_price"]
            risk_per_share = abs(entry - sl)

            # Locked holdings: track PnL but never auto-close
            if symbol in locked_symbols:
                await self.ctx.db.update_unrealized_pnl(
                    pos["trade_id"], current_price,
                )
                continue

            # Partial profit booking (before target/SL checks)
            partial_booked = await self._check_partial_profit_booking(
                pos, current_price,
            )
            if partial_booked:
                # Update unrealized PnL for remaining position and move on;
                # skip target/SL checks this cycle to let the partial order settle
                await self.ctx.db.update_unrealized_pnl(
                    pos["trade_id"], current_price,
                )
                continue

            # Target hit?
            if (pos["signal_type"] == "BUY" and current_price >= target) or (
                pos["signal_type"] == "SELL" and current_price <= target
            ):
                qty = pos.get("quantity", 0)
                if pos["signal_type"] == "BUY":
                    gross_pnl = (current_price - entry) * qty
                else:
                    gross_pnl = (entry - current_price) * qty
                product = pos.get("product", "MIS")
                costs = compute_transaction_costs(
                    entry, current_price, qty, product=product,
                    cost_config=self.ctx.config.transaction_costs,
                )
                pnl = round(gross_pnl - costs, 2)

                if self.ctx.config.execution.transaction_mode == "manual":
                    await self._queue_exit_for_approval(
                        pos, current_price, pnl, "target_hit",
                    )
                else:
                    await self.ctx.db.close_position(pos["trade_id"], current_price, pnl)
                targets_hit.append({"symbol": symbol, "pnl": pnl})
                logger.info(
                    "position-monitor: TARGET HIT %s — exit=%.2f pnl=₹%.2f (costs=₹%.2f)",
                    symbol, current_price, pnl, costs,
                )
                continue

            # SL hit?
            if (pos["signal_type"] == "BUY" and current_price <= sl) or (
                pos["signal_type"] == "SELL" and current_price >= sl
            ):
                qty = pos.get("quantity", 0)
                if pos["signal_type"] == "BUY":
                    gross_pnl = (current_price - entry) * qty
                else:
                    gross_pnl = (entry - current_price) * qty
                product = pos.get("product", "MIS")
                costs = compute_transaction_costs(
                    entry, current_price, qty, product=product,
                    cost_config=self.ctx.config.transaction_costs,
                )
                pnl = round(gross_pnl - costs, 2)

                if self.ctx.config.execution.transaction_mode == "manual":
                    await self._queue_exit_for_approval(
                        pos, current_price, pnl, "stop_loss_hit",
                    )
                else:
                    await self.ctx.db.close_position(pos["trade_id"], current_price, pnl)
                stops_hit.append({"symbol": symbol, "pnl": pnl})
                logger.info(
                    "position-monitor: STOP LOSS HIT %s — exit=%.2f pnl=₹%.2f (costs=₹%.2f)",
                    symbol, current_price, pnl, costs,
                )
                continue

            # Trailing SL
            if cfg.trailing_sl_enabled and risk_per_share > 0:
                if pos["signal_type"] == "BUY":
                    profit = current_price - entry
                else:
                    profit = entry - current_price
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
                        await self.ctx.db.update_position_sl(pos["trade_id"], new_sl)
                        trails_modified += 1

            # Holding period expiry check
            expiry_result = await self._check_holding_expiry(
                pos, current_price, entry, now_ist(),
            )
            if expiry_result:
                expiry_actions.append(expiry_result)
                if expiry_result["action"] == "closed":
                    continue  # position already closed, skip PnL update

            # Update unrealized PnL
            await self.ctx.db.update_unrealized_pnl(pos["trade_id"], current_price)

        # Notify holding period expiry actions
        for ea in expiry_actions:
            if ea["action"] == "closed":
                await self.broadcast("trade_exit", {
                    "symbol": ea["symbol"], "reason": "holding_expiry",
                })
                await self.ctx.notify.send_exit_alert(
                    ea["symbol"],
                    f"Holding period expired ({ea['days_held']}d/{ea['expected_days']}d) — {ea['reason']}",
                    ea.get("pnl", 0),
                )
            elif ea["action"] == "tightened":
                await self.ctx.notify.send(
                    f"Holding period expired for {ea['symbol']} "
                    f"({ea['days_held']}d/{ea['expected_days']}d): "
                    f"in profit — SL tightened to {ea['new_sl']:.2f}",
                    alert_type="info",
                )

        # Broadcast and notify target/stop hits
        for hit in targets_hit:
            await self.broadcast("trade_exit", {
                "symbol": hit["symbol"], "reason": "target_hit",
            })
            await self.ctx.notify.send_exit_alert(
                hit["symbol"], "Target hit", hit["pnl"],
            )
        for hit in stops_hit:
            await self.broadcast("trade_exit", {
                "symbol": hit["symbol"], "reason": "stop_loss_hit",
            })
            await self.ctx.notify.send_exit_alert(
                hit["symbol"], "Stop loss hit", hit["pnl"],
            )

        # Broadcast portfolio PnL summary
        if local_positions:
            await self.broadcast("portfolio_pnl", {
                "positions": len(local_positions),
                "targets_hit": len(targets_hit),
                "stops_hit": len(stops_hit),
                "trails_modified": trails_modified,
            })

        # Alert on LTP fetch failures (positions were unmonitored)
        if ltp_failures:
            await self.ctx.notify.send(
                f"WARNING: LTP fetch failed for {len(ltp_failures)} positions "
                f"({', '.join(ltp_failures)}). These positions were NOT monitored "
                f"this cycle — SL/target checks skipped.",
                alert_type="errors",
            )

        target_syms = [h["symbol"] for h in targets_hit]
        stop_syms = [h["symbol"] for h in stops_hit]
        expiry_syms = [ea["symbol"] for ea in expiry_actions]
        logger.info(
            "position-monitor: %d positions — targets_hit=%s, stops_hit=%s, "
            "expiry_actions=%s, trails_modified=%d, discrepancies=%d, "
            "ltp_failures=%d, recovered=%d",
            len(local_positions), target_syms or "none", stop_syms or "none",
            expiry_syms or "none", trails_modified,
            len(discrepancies) if discrepancies else 0,
            len(ltp_failures), len(recovered),
        )

        return SkillResult(
            success=len(ltp_failures) == 0,
            skill_name=self.name,
            error=f"LTP fetch failed for: {', '.join(ltp_failures)}" if ltp_failures else None,
            data={
                "positions_monitored": len(local_positions),
                "trails_modified": trails_modified,
                "targets_hit": target_syms,
                "stops_hit": stop_syms,
                "expiry_actions": expiry_syms,
                "discrepancies": len(discrepancies) if discrepancies else 0,
                "ltp_failures": ltp_failures,
                "ghost_recovered": recovered,
            },
        )

    async def _get_ltp_with_retry(
        self, symbol: str, max_retries: int = 3, base_delay: float = 1.0,
    ) -> float | None:
        """Fetch LTP with exponential backoff retries.

        Returns the price on success, or None if all retries exhausted.
        """
        for attempt in range(max_retries):
            try:
                price = await self.ctx.market_data.get_ltp(symbol)
                if price is not None and price > 0:
                    return price
                logger.warning(
                    "LTP for %s returned invalid value: %s (attempt %d/%d)",
                    symbol, price, attempt + 1, max_retries,
                )
            except Exception as e:
                logger.warning(
                    "LTP fetch failed for %s: %s (attempt %d/%d)",
                    symbol, e, attempt + 1, max_retries,
                )
            if attempt < max_retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        return None

    def _reconcile(self, local: list[dict[str, Any]], broker: list[dict[str, Any]]) -> list[str]:
        """Compare local DB positions with broker positions."""
        discrepancies = []

        # Build lookup by symbol for broker positions
        broker_by_symbol: dict[str, dict[str, Any]] = {}
        for bp in broker:
            sym = bp.get("tradingsymbol") or bp.get("symbol", "")
            broker_by_symbol[sym] = bp

        # Check each local position against broker
        local_symbols = set()
        for pos in local:
            symbol = pos.get("symbol", "")
            local_symbols.add(symbol)

            if symbol not in broker_by_symbol:
                # Paper mode positions won't be on broker
                if pos.get("mode") != "paper":
                    discrepancies.append(
                        f"{symbol}: in local DB but not on broker"
                    )
                continue

            bp = broker_by_symbol[symbol]
            broker_qty = bp.get("quantity", bp.get("net_quantity", 0))
            local_qty = pos.get("quantity", 0)
            if broker_qty != local_qty:
                discrepancies.append(
                    f"{symbol}: qty mismatch (local={local_qty}, broker={broker_qty})"
                )

        # Check for broker positions not in local DB
        for sym, bp in broker_by_symbol.items():
            qty = bp.get("quantity", bp.get("net_quantity", 0))
            if sym not in local_symbols and qty != 0:
                discrepancies.append(
                    f"{sym}: on broker (qty={qty}) but not in local DB"
                )

        return discrepancies

    async def _recover_ghost_positions(
        self,
        local_positions: list[dict[str, Any]],
        broker_positions: list[dict[str, Any]],
    ) -> list[str]:
        """Auto-close local positions that no longer exist on the broker.

        When the broker closes a position (e.g. SL-M triggered server-side,
        manual exit via Kite web), the local DB still shows it as open.
        This method detects those "ghost" positions and closes them using
        the best available exit price.

        Returns list of symbols that were recovered.
        """
        if self.ctx.config.mode == "paper":
            return []

        broker_by_symbol: dict[str, dict[str, Any]] = {}
        for bp in broker_positions:
            sym = bp.get("tradingsymbol") or bp.get("symbol", "")
            qty = bp.get("quantity", bp.get("net_quantity", 0))
            broker_by_symbol[sym] = {"qty": qty, "data": bp}

        recovered: list[str] = []

        for pos in local_positions:
            if pos.get("mode") == "paper":
                continue
            symbol = pos["symbol"]
            broker_info = broker_by_symbol.get(symbol)

            # Position gone from broker entirely, or broker shows qty=0
            is_ghost = (
                broker_info is None
                or broker_info["qty"] == 0
            )
            if not is_ghost:
                continue

            # Determine exit price: use LTP as best estimate
            exit_price = await self._get_ltp_with_retry(symbol)
            if exit_price is None:
                # Fallback: use stop-loss price (conservative estimate for
                # broker-side SL triggers, which is the most common cause)
                exit_price = pos["stop_loss_price"]
                logger.warning(
                    "Ghost position %s: LTP unavailable, using SL price %.2f as exit estimate",
                    symbol, exit_price,
                )

            entry = pos["entry_price"]
            qty = pos.get("quantity", 0)
            if pos["signal_type"] == "BUY":
                gross_pnl = (exit_price - entry) * qty
            else:
                gross_pnl = (entry - exit_price) * qty

            product = pos.get("product", "MIS")
            costs = compute_transaction_costs(
                entry, exit_price, qty, product=product,
                cost_config=self.ctx.config.transaction_costs,
            )
            pnl = round(gross_pnl - costs, 2)

            await self.ctx.db.close_position(pos["trade_id"], exit_price, pnl)
            recovered.append(symbol)

            logger.warning(
                "GHOST POSITION RECOVERED: %s — closed in DB with exit=%.2f pnl=₹%.2f "
                "(position was closed on broker but still open locally)",
                symbol, exit_price, pnl,
            )

            await self.ctx.notify.send_exit_alert(
                symbol, "Broker-side close (auto-recovered)", pnl,
            )

            # Audit trail
            try:
                await self.ctx.db.log_audit(
                    action_type="ghost_position_recovery",
                    skill_name=self.name,
                    input_summary={
                        "symbol": symbol, "trade_id": pos["trade_id"],
                        "entry_price": entry, "exit_price": exit_price,
                    },
                    output_summary={"pnl": pnl, "costs": costs},
                )
            except Exception:
                pass

        return recovered

    async def _check_holding_expiry(
        self,
        pos: dict[str, Any],
        current_price: float,
        entry: float,
        now: Any,
    ) -> dict[str, Any] | None:
        """Check if a position has exceeded its expected holding period and act.

        Returns None if no action needed, or a dict describing the action taken.
        """
        from datetime import datetime

        cfg = self.ctx.config.risk.holding_expiry
        if not cfg.enabled or cfg.action == "ignore":
            return None

        expected_days = pos.get("expected_holding_days")
        if not expected_days or expected_days <= 0:
            return None  # intraday or no holding period set (handled by square-off)

        # Calculate trading days held
        created_at_str = pos.get("created_at", "")
        if not created_at_str:
            return None
        try:
            created_at = datetime.fromisoformat(str(created_at_str))
        except (ValueError, TypeError):
            return None

        # Approximate trading days: calendar days * 5/7 (excludes weekends)
        calendar_days = (now.replace(tzinfo=None) - created_at.replace(tzinfo=None)).days
        trading_days_held = max(0, int(calendar_days * 5 / 7))

        # Cap at max_holding_days
        effective_expiry = min(expected_days, cfg.max_holding_days)

        if trading_days_held < effective_expiry:
            return None  # not yet expired

        symbol = pos["symbol"]
        qty = pos.get("quantity", 0)

        # Calculate unrealized PnL %
        if pos["signal_type"] == "BUY":
            pnl_pct = (current_price - entry) / entry * 100 if entry else 0
        else:
            pnl_pct = (entry - current_price) / entry * 100 if entry else 0

        result_base = {
            "symbol": symbol,
            "days_held": trading_days_held,
            "expected_days": expected_days,
            "pnl_pct": round(pnl_pct, 2),
        }

        if cfg.action == "force_close":
            # Close regardless of P&L
            pnl = await self._close_expired_position(pos, current_price, entry, qty)
            return {**result_base, "action": "closed", "reason": "force_close", "pnl": pnl}

        # tighten_or_close logic
        if pnl_pct > cfg.breakeven_buffer_pct:
            # In profit — tighten SL to breakeven + buffer
            buffer = entry * cfg.breakeven_buffer_pct / 100
            if pos["signal_type"] == "BUY":
                new_sl = entry + buffer
            else:
                new_sl = entry - buffer

            if self._is_better_sl(pos["signal_type"], new_sl, pos["stop_loss_price"]):
                await self.ctx.broker.modify_sl_order(pos.get("sl_order_id"), new_sl)
                await self.ctx.db.update_position_sl(pos["trade_id"], new_sl)
                logger.info(
                    "position-monitor: HOLDING EXPIRY %s — in profit (%.1f%%), "
                    "SL tightened to %.2f",
                    symbol, pnl_pct, new_sl,
                )
                return {**result_base, "action": "tightened", "new_sl": new_sl}
            return None  # SL already tighter than breakeven

        # At a loss or near breakeven — close the position
        reason = "at_loss" if pnl_pct < cfg.loss_threshold_pct else "near_breakeven"
        pnl = await self._close_expired_position(pos, current_price, entry, qty)
        logger.info(
            "position-monitor: HOLDING EXPIRY %s — %s (%.1f%%), closed at %.2f pnl=₹%.2f",
            symbol, reason, pnl_pct, current_price, pnl,
        )
        return {**result_base, "action": "closed", "reason": reason, "pnl": pnl}

    async def _queue_exit_for_approval(
        self,
        pos: dict[str, Any],
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        """Queue an exit as a pending trade instead of auto-closing (manual mode)."""
        symbol = pos.get("symbol", "?")
        qty = pos.get("quantity", 0)
        product = pos.get("product", "CNC")
        entry = pos.get("entry_price", 0)
        exit_side = "SELL" if pos["signal_type"] == "BUY" else "BUY"

        existing = await self.ctx.db.get_pending_trade_by_symbol(symbol)
        if existing:
            logger.info(
                "position-monitor: %s %s — pending exit already queued (id=%s)",
                reason.upper(), symbol, existing.get("id"),
            )
            return

        signal = {
            "symbol": symbol,
            "signal_type": exit_side,
            "entry_price": exit_price,
            "target_price": exit_price,
            "stop_loss_price": exit_price,
            "position_size": qty,
            "confidence_score": 1.0,
            "product": product,
            "model_version": f"exit_{reason}",
        }
        pending_id = await self.ctx.db.insert_pending_trade(signal)
        invested = round(qty * entry, 2)
        current_val = round(qty * exit_price, 2)
        pnl_pct = round((pnl / invested) * 100, 2) if invested > 0 else 0

        logger.info(
            "position-monitor: queued %s exit for %s (reason=%s, pending_id=%d, pnl=₹%.2f)",
            exit_side, symbol, reason, pending_id, pnl,
        )
        logger.info(
            "position-monitor: %s %s — queued for approval (manual mode)",
            reason.upper(), symbol,
        )
        await self.ctx.notify.send(
            f"Pending Exit — {reason.upper().replace('_', ' ')}\n"
            f"{exit_side} <b>{symbol}</b> x{qty} ({product})\n"
            f"  Entry: ₹{entry:.2f} → LTP: ₹{exit_price:.2f}\n"
            f"  Invested: ₹{invested:,.2f} | Current: ₹{current_val:,.2f}\n"
            f"  PnL: ₹{pnl:,.2f} ({pnl_pct:+.2f}%)\n"
            f"  SL: ₹{pos.get('stop_loss_price', 0):.2f} | Target: ₹{pos.get('target_price', 0):.2f}\n"
            f"Approve: /approve {symbol}\n"
            f"Reject: /reject {symbol}",
            alert_type="trade_entry",
        )

    async def _close_expired_position(
        self,
        pos: dict[str, Any],
        current_price: float,
        entry: float,
        qty: int,
    ) -> float:
        """Close a position due to holding period expiry."""
        if pos["signal_type"] == "BUY":
            gross_pnl = (current_price - entry) * qty
        else:
            gross_pnl = (entry - current_price) * qty
        product = pos.get("product", "MIS")
        costs = compute_transaction_costs(
            entry, current_price, qty, product=product,
            cost_config=self.ctx.config.transaction_costs,
        )
        pnl = round(gross_pnl - costs, 2)
        await self.ctx.db.close_position(pos["trade_id"], current_price, pnl)
        return pnl

    def _is_better_sl(self, signal_type: str, new_sl: float, current_sl: float) -> bool:
        """Check if new SL is tighter (more protective) than current."""
        if signal_type == "BUY":
            return new_sl > current_sl
        return new_sl < current_sl

    async def _check_partial_profit_booking(
        self,
        pos: dict[str, Any],
        current_price: float,
    ) -> bool:
        """Book partial profits at an intermediate target level.

        Returns True if a partial booking was executed this cycle (caller
        should skip target/SL checks to let the order settle), False otherwise.
        """
        cfg = self.ctx.config.risk.partial_profit
        if not cfg.enabled:
            return False

        position_id = pos["trade_id"]

        # Check if this position already had a partial booking
        already_booked = await self.ctx.db.get_system_state(
            f"partial_booked_{position_id}",
        )
        if already_booked:
            return False

        entry = pos["entry_price"]
        target = pos["target_price"]
        signal_type = pos["signal_type"]

        # Calculate intermediate target
        if signal_type == "BUY":
            intermediate_target = entry + (target - entry) * cfg.first_target_pct
            crossed = current_price >= intermediate_target
        else:
            intermediate_target = entry - (entry - target) * cfg.first_target_pct
            crossed = current_price <= intermediate_target

        if not crossed:
            return False

        qty = pos.get("quantity", 0)
        close_qty = round(qty * cfg.first_close_pct)
        if close_qty <= 0:
            return False

        # In manual mode, skip auto partial profit — user controls all exits
        if self.ctx.config.execution.transaction_mode == "manual":
            logger.info(
                "position-monitor: partial profit target crossed for %s but skipping (manual mode)",
                pos["symbol"],
            )
            return False

        # Place the partial exit order
        try:
            if signal_type == "BUY":
                await self.ctx.broker.place_order(
                    symbol=pos["symbol"],
                    side="SELL",
                    quantity=close_qty,
                    price=current_price,
                    order_type="MARKET",
                    product=pos.get("product", "MIS"),
                )
            else:
                await self.ctx.broker.place_order(
                    symbol=pos["symbol"],
                    side="BUY",
                    quantity=close_qty,
                    price=current_price,
                    order_type="MARKET",
                    product=pos.get("product", "MIS"),
                )
        except Exception:
            logger.exception(
                "position-monitor: PARTIAL PROFIT order failed for %s",
                pos["symbol"],
            )
            return False

        # Move SL to breakeven if configured
        if cfg.move_sl_to_breakeven:
            try:
                sl_order_id = pos.get("sl_order_id")
                if sl_order_id:
                    await self.ctx.broker.modify_sl_order(sl_order_id, entry)
                    await self.ctx.db.update_position_sl(position_id, entry)
            except Exception:
                logger.exception(
                    "position-monitor: Failed to move SL to breakeven for %s",
                    pos["symbol"],
                )

        # Mark as partially booked
        await self.ctx.db.set_system_state(
            f"partial_booked_{position_id}", "true",
        )

        logger.info(
            "position-monitor: PARTIAL PROFIT BOOKED %s — "
            "closed %d/%d shares at %.2f (intermediate target %.2f), "
            "SL moved to breakeven=%s",
            pos["symbol"],
            close_qty,
            qty,
            current_price,
            intermediate_target,
            cfg.move_sl_to_breakeven,
        )

        return True
