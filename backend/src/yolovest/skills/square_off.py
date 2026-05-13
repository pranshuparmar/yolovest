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
4. Retry failed positions until hard deadline (market close - 1 min)
5. Send Telegram summary of all squared-off positions
6. If any positions remain after deadline, send CRITICAL alert
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from yolovest.costs import resolve_round_trip_costs
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)

# Max retries per position before giving up for this cycle
_MAX_RETRIES_PER_POSITION = 3
# Seconds between retry rounds
_RETRY_DELAY_SEC = 10


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
            logger.warning("Invalid square_off time %r, using default 15:15", sq_time)
            self.schedule = "15 15 * * 1-5"  # fallback default

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_square_off_window())

    def _get_hard_deadline(self) -> datetime:
        """Compute the hard deadline: market close - 1 minute.

        After this, the broker will auto-square at market price with
        potentially terrible slippage. We must finish before this.
        If we're already past market close (e.g. force=True from kill switch,
        or running outside market hours), set deadline 5 minutes from now.
        """
        now = now_ist()
        close_str = self.ctx.config.market_hours.close  # e.g. "15:30"
        parts = close_str.split(":")
        close_time = now.replace(
            hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0,
        )
        deadline = close_time - timedelta(minutes=1)
        if deadline <= now:
            # Already past market close — give a reasonable window
            deadline = now + timedelta(minutes=5)
        return deadline

    async def execute(self, **kwargs: Any) -> SkillResult:
        force = kwargs.get("force", False)  # True when called from kill-switch
        positions = await self.ctx.db.get_open_positions(mode=self.ctx.config.mode)

        # Filter to MIS (intraday) only, unless force=True (kill switch closes everything)
        if not force:
            positions = [p for p in positions if p["product"] == "MIS"]

        # Never auto-sell locked holdings (even on force/kill switch)
        locked_symbols = await self.ctx.db.get_locked_symbols()
        if locked_symbols:
            positions = [p for p in positions if p["symbol"] not in locked_symbols]

        if not positions:
            return SkillResult(
                success=True, skill_name=self.name,
                data={"squared_off": [], "total_pnl": 0, "failures": [], "force": force},
            )

        # In manual mode (non-force), notify user instead of auto-closing.
        # MIS positions MUST close by EOD — warn urgently via Telegram.
        if self.ctx.config.execution.transaction_mode == "manual" and not force:
            symbols = [p["symbol"] for p in positions]
            logger.warning(
                "square-off: %d MIS positions need closing but manual mode is active: %s",
                len(positions), symbols,
            )
            await self.ctx.notify.send(
                f"URGENT: {len(positions)} MIS positions must close before 3:30 PM!\n"
                f"Symbols: {', '.join(symbols)}\n"
                f"Approve exits on dashboard or Zerodha will auto-square with penalty.",
                alert_type="errors",
            )
            return SkillResult(
                success=True, skill_name=self.name,
                data={"squared_off": [], "total_pnl": 0, "failures": [],
                      "manual_mode_warning": symbols, "force": force},
            )

        deadline = self._get_hard_deadline()
        squared_off: list[dict[str, Any]] = []
        remaining = list(positions)
        all_failures: list[dict[str, Any]] = []
        attempt = 0

        while remaining and attempt < _MAX_RETRIES_PER_POSITION:
            if attempt > 0:
                # Check deadline before retrying
                if now_ist() >= deadline:
                    logger.error(
                        "square-off: HARD DEADLINE reached with %d positions still open",
                        len(remaining),
                    )
                    break
                logger.warning(
                    "square-off: retrying %d failed positions (attempt %d/%d)",
                    len(remaining), attempt + 1, _MAX_RETRIES_PER_POSITION,
                )
                await asyncio.sleep(_RETRY_DELAY_SEC)

            failed_this_round: list[dict[str, Any]] = []
            errors_this_round: list[dict[str, Any]] = []

            for pos in remaining:
                # Check deadline mid-loop
                if now_ist() >= deadline:
                    failed_this_round.append(pos)
                    errors_this_round.append({
                        "symbol": pos["symbol"], "error": "hard deadline reached",
                    })
                    continue

                try:
                    result = await self._close_single_position(pos)
                    squared_off.append(result)
                except Exception as e:
                    logger.warning(
                        "square-off: failed to close %s (attempt %d): %s",
                        pos["symbol"], attempt + 1, e,
                    )
                    failed_this_round.append(pos)
                    errors_this_round.append({
                        "symbol": pos["symbol"], "error": str(e),
                    })

            remaining = failed_this_round
            all_failures = errors_this_round
            attempt += 1

        # Telegram summary
        total_pnl = sum(s["pnl"] for s in squared_off)
        if squared_off or all_failures:
            msg = (
                f"Square-off complete: {len(squared_off)} positions closed, "
                f"PnL: ₹{total_pnl:,.2f}"
            )
            if all_failures:
                failed_syms = [f["symbol"] for f in all_failures]
                msg += (
                    f"\nCRITICAL: {len(all_failures)} positions FAILED to close "
                    f"after {attempt} attempts: {', '.join(failed_syms)}\n"
                    f"Broker will auto-square these at market close — expect slippage!"
                )
            await self.ctx.notify.send(msg, alert_type="trade_exit")

        return SkillResult(
            success=len(all_failures) == 0,
            skill_name=self.name,
            data={
                "squared_off": squared_off,
                "total_pnl": total_pnl,
                "failures": all_failures,
                "force": force,
                "attempts": attempt,
            },
            error=(
                f"{len(all_failures)} positions failed to close after {attempt} attempts"
                if all_failures else None
            ),
        )

    async def _close_single_position(self, pos: dict[str, Any]) -> dict[str, Any]:
        """Close a single position. Raises on failure."""
        # Cancel pending SL/target orders
        if pos.get("sl_order_id"):
            try:
                await self.ctx.broker.cancel_order(pos["sl_order_id"])
            except Exception as e:
                logger.warning(
                    "square-off: failed to cancel SL order for %s: %s",
                    pos["symbol"], e,
                )

        # Place market exit order
        exit_type = "SELL" if pos["signal_type"] == "BUY" else "BUY"
        exit_order_id = await self.ctx.broker.place_order(
            symbol=pos["symbol"],
            side=exit_type,
            quantity=pos["quantity"],
            order_type="MARKET",
            product=pos.get("product", "MIS"),
        )

        # Get fill price — wait for fill if not immediate
        exit_price = None
        for _ in range(10):
            order_status = await self.ctx.broker.get_order_status(exit_order_id)
            exit_price = order_status.get("average_price")
            if exit_price and exit_price > 0:
                break
            await asyncio.sleep(0.5)

        qty = pos["quantity"]
        # PnL uses the actual broker fill price for entry (not the signal's
        # entry_price) so recorded slippage is reflected.
        entry = float(pos.get("fill_price") or pos["entry_price"])

        if not exit_price or exit_price <= 0:
            # Fallback: no fill price came back from the broker — use the entry
            # fill so PnL is zero rather than fabricated from the signal price.
            logger.warning(
                "square-off: no fill price for %s exit order %s, using entry fill",
                pos["symbol"], exit_order_id,
            )
            exit_price = entry

        if pos["signal_type"] == "BUY":
            gross_pnl = (exit_price - entry) * qty
        else:
            gross_pnl = (entry - exit_price) * qty

        product = pos.get("product", "MIS")
        costs, _src, breakdown = await resolve_round_trip_costs(
            self.ctx.broker, symbol=pos["symbol"], signal_type=pos["signal_type"],
            entry_price=entry, exit_price=exit_price, quantity=qty,
            product=product, cost_config=self.ctx.config.transaction_costs,
        )
        pnl = round(gross_pnl - costs, 2)

        await self.ctx.db.close_position(
            pos["trade_id"], exit_price, pnl, realized_costs=breakdown,
        )
        return {"symbol": pos["symbol"], "pnl": pnl}
