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

            # Update unrealized PnL
            await self.ctx.db.update_unrealized_pnl(pos["trade_id"], current_price)

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
        logger.info(
            "position-monitor: %d positions — targets_hit=%s, stops_hit=%s, "
            "trails_modified=%d, discrepancies=%d, ltp_failures=%d, recovered=%d",
            len(local_positions), target_syms or "none", stop_syms or "none",
            trails_modified, len(discrepancies) if discrepancies else 0,
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

    def _is_better_sl(self, signal_type: str, new_sl: float, current_sl: float) -> bool:
        """Check if new SL is tighter (more protective) than current."""
        if signal_type == "BUY":
            return new_sl > current_sl
        return new_sl < current_sl
