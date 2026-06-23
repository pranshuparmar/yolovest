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
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        # Build cron from market_hours.square_off (e.g. "15:15" → "15 15 * * 1-5")
        sq_time = self.ctx.config.market_hours.square_off
        try:
            parts = sq_time.split(":")
            h, m = int(parts[0]), int(parts[1])
            return f"{m} {h} * * 1-5"  # weekdays only
        except (ValueError, IndexError, AttributeError):
            logger.warning("Invalid square_off time %r, using default 15:15", sq_time)
            return "15 15 * * 1-5"  # fallback default

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

        # On kill-switch (force=True), also delete any orphan GTTs at the
        # broker — GTTs that don't reference any currently-open local
        # trade. These can leak when a position was closed outside the
        # system (e.g. manual exit on Kite web) and the GTT was never
        # cleaned up. Letting them survive a kill switch defeats the
        # point — the broker would still fire the order on a price hit.
        orphan_gtts_deleted: list[int] = []
        if force:
            orphan_gtts_deleted = await self._delete_orphan_gtts(positions)

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

        # Manual mode does NOT apply to square-off. EOD is a hard
        # broker-enforced deadline — Zerodha will auto-square any
        # remaining MIS positions at market close with penalty, so
        # asking for human approval defeats the point (and there's no
        # exit-approval UI on the dashboard either; pending_trades is
        # entry-only). User already committed to the trade when it
        # entered; the exit at EOD is a deterministic consequence.
        if self.ctx.config.execution.transaction_mode == "manual" and not force:
            logger.info(
                "square-off: manual mode is active, but square-off auto-closes "
                "regardless (EOD broker deadline) — proceeding to close %d MIS "
                "positions: %s",
                len(positions), [p["symbol"] for p in positions],
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
                "orphan_gtts_deleted": orphan_gtts_deleted,
            },
            error=(
                f"{len(all_failures)} positions failed to close after {attempt} attempts"
                if all_failures else None
            ),
        )

    async def _delete_orphan_gtts(
        self, open_positions: list[dict[str, Any]],
    ) -> list[int]:
        """Delete any broker-side GTTs that aren't bound to a currently-
        open local trade. Used by kill-switch (`force=True`) so a price
        hit doesn't fire a stale GTT after we've market-exited everything.

        Returns the list of deleted trigger_ids (purely for the result
        payload / notify summary).
        """
        broker = self.ctx.broker
        if not (hasattr(broker, "get_gtts") and hasattr(broker, "delete_gtt")):
            return []

        try:
            gtts = await broker.get_gtts()
        except Exception as e:
            logger.warning("orphan-GTT sweep: get_gtts failed: %s", e)
            return []

        live_gtt_ids = {
            int(p["gtt_id"]) for p in open_positions
            if p.get("gtt_id")
        }

        deleted: list[int] = []
        for g in gtts or []:
            try:
                gid = int(g.get("id") or g.get("trigger_id") or 0)
            except (TypeError, ValueError):
                continue
            if not gid or gid in live_gtt_ids:
                continue
            status = (g.get("status") or "").lower()
            # Only delete things that are still live at the broker; let
            # already-triggered / cancelled / expired entries age out
            # naturally.
            if status not in {"active", "scheduled"}:
                continue
            try:
                await broker.delete_gtt(gid)
                deleted.append(gid)
                await self.ctx.db.log_gtt_event(
                    trade_id=None, gtt_id=gid, symbol=g.get("condition", {}).get("tradingsymbol"),
                    event_type="deleted", status="deleted",
                    details={"reason": "orphan_sweep", "prior_status": status},
                )
                logger.info(
                    "orphan-GTT sweep: deleted GTT %d (not bound to an open trade)",
                    gid,
                )
            except Exception as e:
                logger.warning("orphan-GTT sweep: delete %d failed: %s", gid, e)
        if deleted:
            logger.info("orphan-GTT sweep: removed %d stale GTTs", len(deleted))
        return deleted

    async def _close_single_position(self, pos: dict[str, Any]) -> dict[str, Any]:
        """Close a single position. Raises on failure."""
        # Cancel pending SL and target (LIMIT) orders so they don't fire
        # against the market exit that follows.
        for oid_key, label in (("sl_order_id", "SL"), ("target_order_id", "target")):
            oid = pos.get(oid_key)
            if not oid:
                continue
            try:
                await self.ctx.broker.cancel_order(oid)
            except Exception as e:
                logger.warning(
                    "square-off: failed to cancel %s order for %s: %s",
                    label, pos["symbol"], e,
                )

        # Delete any attached GTT (CNC only — MIS positions never have one).
        # If left alive, a GTT can fire after we've market-exited and try
        # to sell shares we no longer own.
        gtt_id = pos.get("gtt_id")
        if gtt_id and hasattr(self.ctx.broker, "delete_gtt"):
            try:
                await self.ctx.broker.delete_gtt(int(gtt_id))
                await self.ctx.db.log_gtt_event(
                    trade_id=pos.get("trade_id"), gtt_id=int(gtt_id),
                    symbol=pos.get("symbol"),
                    event_type="deleted", status="deleted",
                    details={"reason": "square_off"},
                )
            except Exception as e:
                logger.warning(
                    "square-off: failed to delete GTT %s for %s: %s",
                    gtt_id, pos["symbol"], e,
                )

        # Place market exit order
        exit_type = "SELL" if pos["signal_type"] == "BUY" else "BUY"
        exit_order_id = await self.ctx.broker.place_order(
            symbol=pos["symbol"],
            side=exit_type,
            quantity=pos["quantity"],
            order_type="MARKET",
            product=pos.get("product", "MIS"),
            tag="yv-sqoff",
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
