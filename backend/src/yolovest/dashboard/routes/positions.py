"""Open-position management: close, SL/target modify, broker orders, GTT, MIS->CNC convert.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)

from yolovest.dashboard.helpers import (
    _build_cdsl_response,
    _is_cdsl_tpin_error,
)

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

    @app.get("/api/positions")
    async def get_positions(
        user: str = Depends(verify_credentials),
        mode: str | None = Query(None, description="Filter by mode: paper, live, or omit for current"),
    ) -> list[dict[str, Any]]:
        """Current open positions."""
        return await ctx.db.get_open_positions(mode=mode or ctx.config.mode)

    @app.post("/api/positions/{trade_id}/close")
    async def close_position(
        trade_id: str,
        qty: int | None = Query(
            None, ge=1,
            description="Optional partial-close quantity. Omit to close the whole position.",
        ),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Immediately exit a single open position at market.

        Full close (default — no `qty` param):
          1. Cancel any attached SL order at broker.
          2. Delete any attached GTT at broker (so it doesn't fire later).
          3. Place a MARKET exit order in the opposite direction.
          4. Close the trade row with realised PnL.

        Partial close (`?qty=N` where N < current quantity):
          1. Place a MARKET exit order for N shares.
          2. Resize the broker-side SL / target / GTT to the remaining
             quantity so the protection still matches the position.
          3. Update trades.quantity to (current - N); trade stays open.
          4. Log the partial realised PnL to audit_log (separate from
             the trade's final pnl which still accrues against the
             remaining shares).

        Live mode places a real order via the broker; paper mode simulates
        the exit using current LTP. Bypasses the normal manual-approval
        queue — the action is user-initiated and explicit.
        """
        trade = await ctx.db.get_trade(trade_id)
        if not trade:
            raise HTTPException(status_code=404, detail=f"No trade with id={trade_id}")
        if trade.get("status") != "open":
            raise HTTPException(
                status_code=400,
                detail=f"Trade {trade_id} status is {trade.get('status')!r}; nothing to close",
            )

        symbol = trade["symbol"]
        full_qty = int(trade["quantity"])
        is_partial = qty is not None and qty < full_qty
        if qty is not None and qty > full_qty:
            raise HTTPException(
                status_code=400,
                detail=f"qty={qty} exceeds current position size {full_qty}",
            )
        exit_qty = int(qty or 0) if is_partial else full_qty
        remaining_qty = full_qty - exit_qty
        exit_side = "SELL" if trade["signal_type"] == "BUY" else "BUY"
        product = trade.get("product", "MIS")

        if is_partial:
            # Partial-close path: place exit for `exit_qty`, then resize
            # broker-side SL / target / GTT to `remaining_qty`. We do NOT
            # cancel/delete the protection legs the way full-close does —
            # the remaining shares still need them.
            try:
                exit_order_id = await ctx.broker.place_order(
                    symbol=symbol, side=exit_side, quantity=exit_qty,
                    order_type="MARKET", product=product,
                    tag="yv-partial-close",
                )
            except Exception as e:
                logger.exception(
                    "close_position(partial): place exit failed for %s", trade_id,
                )
                msg = str(e)
                if _is_cdsl_tpin_error(msg):
                    # Defer to the frontend to render the CDSL action UI;
                    # 412 Precondition Required nicely signals "do the
                    # auth step first, then retry."
                    cdsl = await _build_cdsl_response(ctx, 
                        msg,
                        triggered_by={
                            "symbol": symbol,
                            "quantity": exit_qty,
                            "side": exit_side,
                            "source": "partial-close",
                        },
                    )
                    raise HTTPException(status_code=412, detail=cdsl) from e
                raise HTTPException(
                    status_code=502,
                    detail=f"Broker rejected partial-exit order: {e}",
                ) from e

            # Wait briefly for fill
            exit_price = None
            for _ in range(10):
                try:
                    status = await ctx.broker.get_order_status(exit_order_id)
                    exit_price = status.get("average_price")
                    if exit_price and exit_price > 0:
                        break
                except Exception:
                    logger.debug(
                        "close_position(partial): exit-status poll failed for %s",
                        trade_id, exc_info=True,
                    )
                await asyncio.sleep(0.5)
            exit_price_unreliable = False
            if not exit_price or exit_price <= 0:
                try:
                    exit_price = await ctx.market_data.get_ltp(symbol)
                except Exception:
                    logger.warning(
                        "close_position(partial): LTP fallback failed for %s — "
                        "exit price falls back to entry; PnL will be unreliable",
                        symbol, exc_info=True,
                    )
                    exit_price = float(
                        trade.get("fill_price") or trade["entry_price"] or 0
                    )
                    exit_price_unreliable = True

            entry = float(trade.get("fill_price") or trade["entry_price"])
            gross_pnl = (
                (exit_price - entry) * exit_qty
                if trade["signal_type"] == "BUY"
                else (entry - exit_price) * exit_qty
            )
            from yolovest.costs import resolve_round_trip_costs
            costs, _src, _breakdown = await resolve_round_trip_costs(
                ctx.broker, symbol=symbol, signal_type=trade["signal_type"],
                entry_price=entry, exit_price=float(exit_price),
                quantity=exit_qty, product=product,
                cost_config=ctx.config.transaction_costs,
            )
            partial_pnl = round(gross_pnl - costs, 2)
            if exit_price_unreliable:
                try:
                    await ctx.notify.send(
                        f"Partial close {symbol} x{exit_qty}: could NOT determine the "
                        f"actual exit price (broker status + live quote both "
                        f"unavailable). Recorded PnL ₹{partial_pnl:+,.2f} is "
                        f"UNRELIABLE — reconcile against your broker contract note.",
                        alert_type="errors",
                    )
                except Exception:
                    logger.debug(
                        "close_position(partial): unreliable-exit alert failed",
                        exc_info=True,
                    )

            # Resize broker-side protection to remaining_qty. GTT
            # (CNC OCO) → modify with new quantity, target/SL prices
            # unchanged. MIS broker-side SL → cancel + re-place at
            # remaining_qty. Same template as
            # position_monitor._check_partial_profit_booking's
            # resize block, but without the move-to-breakeven step
            # (that's a separate decision the user can take via the
            # Tighten SL action).
            gtt_id = trade.get("gtt_id")
            if gtt_id and remaining_qty > 0 and hasattr(ctx.broker, "modify_gtt"):
                tgt = float(trade.get("target_price") or 0)
                cur_sl = float(trade.get("stop_loss_price") or 0)
                if tgt > 0 and cur_sl > 0:
                    buf = 0.005
                    if exit_side == "SELL":
                        sl_limit = cur_sl * (1 - buf)
                        tgt_limit = tgt * (1 - buf * 0.5)
                    else:
                        sl_limit = cur_sl * (1 + buf)
                        tgt_limit = tgt * (1 + buf * 0.5)
                    try:
                        await ctx.broker.modify_gtt(
                            gtt_id=int(gtt_id), symbol=symbol, side=exit_side,
                            quantity=remaining_qty,
                            stoploss_trigger=cur_sl, stoploss_limit=sl_limit,
                            target_trigger=tgt, target_limit=tgt_limit,
                            last_price=float(exit_price),
                        )
                        await ctx.db.log_gtt_event(
                            trade_id=trade_id, gtt_id=int(gtt_id), symbol=symbol,
                            event_type="modified", status="active",
                            details={
                                "reason": "user_partial_close_resize",
                                "quantity": remaining_qty,
                                "sl_trigger": cur_sl, "sl_limit": sl_limit,
                                "target_trigger": tgt, "target_limit": tgt_limit,
                            },
                        )
                    except Exception:
                        logger.exception(
                            "close_position(partial): GTT resize failed for %s",
                            trade_id,
                        )

            # For MIS broker-side SL / target LIMITs we cancel and let
            # position-monitor re-attach them with the new qty on its
            # next cycle. Resizing in place via cancel/replace here
            # would duplicate trade_execute._attach_mis_target_limit
            # logic for marginal benefit — position-monitor runs every
            # 15 min and will reconcile.
            if not gtt_id:
                for oid_key in ("sl_order_id", "target_order_id"):
                    oid = trade.get(oid_key)
                    if not oid:
                        continue
                    try:
                        await ctx.broker.cancel_order(oid)
                    except Exception:
                        logger.debug(
                            "close_position(partial): cancel %s failed (terminal?)",
                            oid_key, exc_info=True,
                        )

            # Resize the local trade row + record the partial PnL.
            # trades.pnl stays NULL until the final closure of the
            # remaining shares; trades.realized_partial_pnl accumulates
            # the booked-along-the-way PnL so the UI can show
            # total = realized_partial_pnl + (pnl or unrealised).
            await ctx.db.update_position_quantity(trade_id, remaining_qty)
            await ctx.db.increment_realized_partial_pnl(trade_id, partial_pnl)
            try:
                await ctx.db.log_audit(
                    action_type="partial_close",
                    skill_name="user_partial_close",
                    output_summary={
                        "trade_id": trade_id, "symbol": symbol,
                        "exit_qty": exit_qty, "remaining_qty": remaining_qty,
                        "exit_price": float(exit_price),
                        "entry_price": entry,
                        "partial_pnl": partial_pnl,
                        "exit_order_id": exit_order_id,
                    },
                    duration_ms=0,
                )
            except Exception:
                logger.debug("close_position(partial): audit log failed", exc_info=True)

            try:
                await ctx.notify.send(
                    f"Partial close: {symbol} {exit_qty}/{full_qty} @ "
                    f"₹{exit_price:.2f} (entry ₹{entry:.2f}) — "
                    f"booked ₹{partial_pnl:+,.2f}. Remaining {remaining_qty} open.",
                    alert_type="trade_exit",
                )
            except Exception:
                logger.debug("close_position(partial): notify failed", exc_info=True)

            logger.info(
                "close_position(partial): %s %s qty=%d/%d exit=%.2f "
                "partial_pnl=%.2f remaining=%d (order=%s)",
                exit_side, symbol, exit_qty, full_qty, exit_price,
                partial_pnl, remaining_qty, exit_order_id,
            )

            return {
                "status": "partial",
                "trade_id": trade_id,
                "exit_qty": exit_qty,
                "remaining_qty": remaining_qty,
                "exit_price": float(exit_price),
                "partial_pnl": partial_pnl,
                "exit_order_id": exit_order_id,
            }

        # ---------- Full-close path (existing behaviour) ----------
        qty = full_qty

        # Cancel any open SL / target (MIS LIMIT) orders so the exit isn't
        # double-placed and dangling orders don't fire after we've closed.
        for oid_key, label in (("sl_order_id", "SL"), ("target_order_id", "target")):
            oid = trade.get(oid_key)
            if not oid:
                continue
            try:
                await ctx.broker.cancel_order(oid)
            except Exception:
                logger.debug(
                    "close_position: %s cancel failed (already executed?)",
                    label, exc_info=True,
                )

        # Delete attached GTT (CNC only — MIS has no GTT)
        gtt_id = trade.get("gtt_id")
        if gtt_id and hasattr(ctx.broker, "delete_gtt"):
            try:
                await ctx.broker.delete_gtt(int(gtt_id))
                await ctx.db.set_trade_gtt(trade_id, None)
                await ctx.db.log_gtt_event(
                    trade_id=trade_id, gtt_id=int(gtt_id), symbol=symbol,
                    event_type="deleted", status="deleted",
                    details={"reason": "manual_close"},
                )
            except Exception:
                logger.warning("close_position: delete_gtt %s failed", gtt_id, exc_info=True)

        # Place market exit
        try:
            exit_order_id = await ctx.broker.place_order(
                symbol=symbol,
                side=exit_side,
                quantity=qty,
                order_type="MARKET",
                product=product,
                tag="yv-close",
            )
        except Exception as e:
            logger.exception("close_position: place exit order failed for %s", trade_id)
            msg = str(e)
            if _is_cdsl_tpin_error(msg):
                cdsl = await _build_cdsl_response(ctx, 
                    msg,
                    triggered_by={
                        "symbol": symbol,
                        "quantity": qty,
                        "side": exit_side,
                        "source": "close-position",
                    },
                )
                raise HTTPException(status_code=412, detail=cdsl) from e
            raise HTTPException(status_code=502, detail=f"Broker rejected exit order: {e}") from e

        # Wait briefly for fill, fall back to LTP-based estimate
        exit_price = None
        for _ in range(10):
            try:
                status = await ctx.broker.get_order_status(exit_order_id)
                exit_price = status.get("average_price")
                if exit_price and exit_price > 0:
                    break
            except Exception:
                logger.debug(
                    "close_position: exit-status poll failed for %s",
                    trade_id, exc_info=True,
                )
            await asyncio.sleep(0.5)

        exit_price_unreliable = False
        if not exit_price or exit_price <= 0:
            try:
                exit_price = await ctx.market_data.get_ltp(symbol)
            except Exception:
                # Both the broker fill price AND the live quote are
                # unavailable. The exit order DID go to the broker, so the
                # position is closed — but computing PnL against the entry
                # price fabricates a ~0 result. Record it (the row must close)
                # but flag it loudly so the operator reconciles.
                logger.warning(
                    "close_position: LTP fallback failed for %s — exit price "
                    "falls back to entry; recorded PnL will be unreliable",
                    symbol, exc_info=True,
                )
                exit_price = float(trade.get("fill_price") or trade.get("entry_price") or 0)
                exit_price_unreliable = True

        entry = float(trade.get("fill_price") or trade["entry_price"])
        gross_pnl = (
            (exit_price - entry) * qty if trade["signal_type"] == "BUY"
            else (entry - exit_price) * qty
        )
        from yolovest.costs import resolve_round_trip_costs
        costs, _src, breakdown = await resolve_round_trip_costs(
            ctx.broker, symbol=symbol, signal_type=trade["signal_type"],
            entry_price=entry, exit_price=float(exit_price), quantity=qty,
            product=product, cost_config=ctx.config.transaction_costs,
        )
        pnl = round(gross_pnl - costs, 2)

        await ctx.db.close_position(
            trade_id, float(exit_price), pnl, realized_costs=breakdown,
        )

        if exit_price_unreliable:
            try:
                await ctx.notify.send(
                    f"Manual close {symbol} x{qty}: could NOT determine the actual "
                    f"exit price (broker status + live quote both unavailable). "
                    f"Recorded PnL ₹{pnl:+,.2f} is UNRELIABLE — reconcile against "
                    f"your broker contract note.",
                    alert_type="errors",
                )
            except Exception:
                logger.debug("close_position: unreliable-exit alert failed", exc_info=True)

        try:
            await ctx.notify.send(
                f"Manual close: {symbol} x{qty} @ ₹{exit_price:.2f} "
                f"(entry ₹{entry:.2f}) — PnL ₹{pnl:+,.2f}",
                alert_type="trade_exit",
            )
        except Exception:
            logger.debug("close_position: notify failed", exc_info=True)

        logger.info(
            "close_position: %s %s qty=%d exit=%.2f pnl=%.2f (order=%s)",
            exit_side, symbol, qty, exit_price, pnl, exit_order_id,
        )

        return {
            "status": "closed",
            "trade_id": trade_id,
            "exit_price": float(exit_price),
            "pnl": pnl,
            "exit_order_id": exit_order_id,
        }

    @app.post("/api/positions/{trade_id}/tighten-sl")
    async def tighten_sl(
        trade_id: str,
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Move the stop-loss to a tighter level on an open position.

        Body: {"new_sl": float}

        Routes through the right execution path based on what the trade
        carries:
          - `gtt_id` (CNC OCO GTT): calls broker.modify_gtt to lift the
            SL leg in place. Target leg unchanged.
          - `sl_order_id` (MIS broker-side SL): calls broker.modify_sl_order
            to raise the trigger.
          - Neither (legacy client-side managed): just updates the DB so
            position-monitor's client-side exit uses the new level.

        Validates that the new level is genuinely tighter (closer to LTP)
        than the existing one — refuses to widen the SL via this endpoint
        to prevent accidental risk increases. Use the order form to flip
        a position outright.
        """
        body = await request.json()
        try:
            new_sl = float(body["new_sl"])
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(
                status_code=400, detail=f"Body must include numeric new_sl: {e}",
            ) from e
        if new_sl <= 0:
            raise HTTPException(status_code=400, detail="new_sl must be > 0")

        trade = await ctx.db.get_trade(trade_id)
        if not trade:
            raise HTTPException(status_code=404, detail=f"No trade with id={trade_id}")
        if trade.get("status") != "open":
            raise HTTPException(
                status_code=400,
                detail=f"Trade {trade_id} is {trade.get('status')!r}, not open",
            )

        signal_type = trade["signal_type"]
        current_sl = float(trade.get("stop_loss_price") or 0)
        # Tighten = move SL toward LTP (higher for BUY, lower for SELL).
        if current_sl > 0:
            if signal_type == "BUY" and new_sl <= current_sl:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"new_sl {new_sl} must be above current SL {current_sl} "
                        f"to tighten a BUY position"
                    ),
                )
            if signal_type == "SELL" and new_sl >= current_sl:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"new_sl {new_sl} must be below current SL {current_sl} "
                        f"to tighten a SELL position"
                    ),
                )

        symbol = trade["symbol"]
        path: str
        gtt_id = trade.get("gtt_id")
        sl_order_id = trade.get("sl_order_id")

        if gtt_id and hasattr(ctx.broker, "modify_gtt"):
            # CNC OCO: modify_gtt requires both legs supplied; target
            # unchanged, SL trigger / SL limit moved. Mirrors
            # position_monitor._maybe_trail_gtt_sl.
            exit_side = "SELL" if signal_type == "BUY" else "BUY"
            tgt = float(trade.get("target_price") or 0)
            if tgt <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Trade has no target_price; cannot modify GTT",
                )
            buf = 0.005
            if exit_side == "SELL":
                sl_limit = new_sl * (1 - buf)
                tgt_limit = tgt * (1 - buf * 0.5)
            else:
                sl_limit = new_sl * (1 + buf)
                tgt_limit = tgt * (1 + buf * 0.5)
            try:
                ltp = await ctx.market_data.get_ltp(symbol)
            except Exception:
                ltp = float(trade.get("fill_price") or trade.get("entry_price") or 0)
            await ctx.broker.modify_gtt(
                gtt_id=int(gtt_id), symbol=symbol, side=exit_side,
                quantity=int(trade["quantity"]),
                stoploss_trigger=new_sl, stoploss_limit=sl_limit,
                target_trigger=tgt, target_limit=tgt_limit,
                last_price=float(ltp or 0),
            )
            await ctx.db.log_gtt_event(
                trade_id=trade_id, gtt_id=int(gtt_id), symbol=symbol,
                event_type="modified", status="active",
                details={
                    "reason": "user_tighten_sl",
                    "sl_trigger": new_sl, "sl_limit": sl_limit,
                    "target_trigger": tgt, "target_limit": tgt_limit,
                    "previous_sl": current_sl,
                },
            )
            path = "gtt"
        elif sl_order_id and hasattr(ctx.broker, "modify_sl_order"):
            # MIS broker-side SL: trigger lifted in place.
            await ctx.broker.modify_sl_order(sl_order_id, new_sl)
            path = "sl_order"
        else:
            # Legacy client-side managed — just update the DB; the
            # next position-monitor cycle will exit at the new level.
            path = "client_side"

        await ctx.db.update_position_sl(trade_id, new_sl)
        logger.info(
            "tighten-sl: %s (trade_id=%s) SL %.2f → %.2f via %s",
            symbol, trade_id, current_sl, new_sl, path,
        )
        return {
            "ok": True, "trade_id": trade_id, "symbol": symbol,
            "previous_sl": current_sl, "new_sl": new_sl, "path": path,
        }

    @app.post("/api/positions/{trade_id}/modify-target")
    async def modify_target(
        trade_id: str,
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Move the target price on an open position.

        Symmetric to tighten-sl but with no direction restriction —
        target can move in either direction since "extend the target"
        and "take profits sooner" are both legitimate user intents.

        Routes through:
          - `gtt_id` (CNC OCO): modify_gtt with target trigger lifted,
            SL leg unchanged.
          - `target_order_id` (MIS resting LIMIT): modify_order with
            new price.
          - Neither: just updates the DB so position-monitor's
            client-side exit uses the new level.
        """
        body = await request.json()
        try:
            new_target = float(body["new_target"])
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"Body must include numeric new_target: {e}",
            ) from e
        if new_target <= 0:
            raise HTTPException(status_code=400, detail="new_target must be > 0")

        trade = await ctx.db.get_trade(trade_id)
        if not trade:
            raise HTTPException(status_code=404, detail=f"No trade with id={trade_id}")
        if trade.get("status") != "open":
            raise HTTPException(
                status_code=400,
                detail=f"Trade {trade_id} is {trade.get('status')!r}, not open",
            )

        signal_type = trade["signal_type"]
        current_target = float(trade.get("target_price") or 0)
        current_sl = float(trade.get("stop_loss_price") or 0)
        symbol = trade["symbol"]
        path: str
        gtt_id = trade.get("gtt_id")
        target_order_id = trade.get("target_order_id")

        # Sanity: target must stay on the right side of LTP / entry,
        # otherwise the OCO logic flips. For BUY target > entry/SL;
        # for SELL target < entry/SL. We don't enforce this strictly
        # (user might want to lower a BUY target to take profits at a
        # tighter level, which is valid) but we DO refuse "target
        # crosses SL" which makes the OCO unworkable.
        if current_sl > 0:
            if signal_type == "BUY" and new_target <= current_sl:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"BUY target {new_target} would be at/below SL {current_sl} "
                        f"— move SL first via Tighten SL"
                    ),
                )
            if signal_type == "SELL" and new_target >= current_sl:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"SELL target {new_target} would be at/above SL {current_sl} "
                        f"— move SL first"
                    ),
                )

        if gtt_id and hasattr(ctx.broker, "modify_gtt"):
            exit_side = "SELL" if signal_type == "BUY" else "BUY"
            buf = 0.005
            if exit_side == "SELL":
                sl_limit = current_sl * (1 - buf)
                tgt_limit = new_target * (1 - buf * 0.5)
            else:
                sl_limit = current_sl * (1 + buf)
                tgt_limit = new_target * (1 + buf * 0.5)
            try:
                ltp = await ctx.market_data.get_ltp(symbol)
            except Exception:
                ltp = float(trade.get("fill_price") or trade.get("entry_price") or 0)
            await ctx.broker.modify_gtt(
                gtt_id=int(gtt_id), symbol=symbol, side=exit_side,
                quantity=int(trade["quantity"]),
                stoploss_trigger=current_sl, stoploss_limit=sl_limit,
                target_trigger=new_target, target_limit=tgt_limit,
                last_price=float(ltp or 0),
            )
            await ctx.db.log_gtt_event(
                trade_id=trade_id, gtt_id=int(gtt_id), symbol=symbol,
                event_type="modified", status="active",
                details={
                    "reason": "user_modify_target",
                    "sl_trigger": current_sl, "sl_limit": sl_limit,
                    "target_trigger": new_target, "target_limit": tgt_limit,
                    "previous_target": current_target,
                },
            )
            path = "gtt"
        elif target_order_id and hasattr(ctx.broker, "modify_order"):
            await ctx.broker.modify_order(target_order_id, price=new_target)
            path = "target_order"
        else:
            path = "client_side"

        await ctx.db.conn.execute(
            "UPDATE trades SET target_price = ? WHERE trade_id = ?",
            (float(new_target), trade_id),
        )
        await ctx.db.conn.commit()

        logger.info(
            "modify-target: %s (trade_id=%s) target %.2f → %.2f via %s",
            symbol, trade_id, current_target, new_target, path,
        )
        return {
            "ok": True, "trade_id": trade_id, "symbol": symbol,
            "previous_target": current_target,
            "new_target": new_target, "path": path,
        }

    @app.get("/api/broker/orders")
    async def get_broker_orders(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Today's full order book from the broker (open, executed,
        cancelled, rejected, trigger-pending). Plus active GTTs.

        Mirrors what Kite shows on the Orders tab so the user can
        cancel / modify open orders without bouncing through Kite.
        """
        if not await ctx.broker.is_authenticated():
            return {"authenticated": False, "orders": [], "gtts": []}
        orders: list[dict[str, Any]] = []
        gtts: list[dict[str, Any]] = []
        try:
            orders = list(await ctx.broker.get_orders() or [])
        except Exception:
            logger.exception("get_broker_orders: get_orders failed")
            return {
                "authenticated": True, "orders": [], "gtts": [],
                "error": "orders fetch failed",
            }
        try:
            if hasattr(ctx.broker, "get_gtts"):
                gtts = list(await ctx.broker.get_gtts() or [])
        except Exception:
            logger.debug("get_broker_orders: get_gtts failed", exc_info=True)
        return {"authenticated": True, "orders": orders, "gtts": gtts}

    @app.post("/api/broker/orders/{order_id}/cancel")
    async def cancel_broker_order(
        order_id: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Cancel a still-open broker order by id."""
        try:
            ok = await ctx.broker.cancel_order(order_id)
        except Exception as e:
            logger.exception("cancel_broker_order: %s failed", order_id)
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {"ok": bool(ok), "order_id": order_id}

    @app.post("/api/broker/orders/{order_id}/modify")
    async def modify_broker_order(
        order_id: str,
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Modify an open broker order. Body accepts any subset of
        {price, quantity, trigger_price, order_type}. None fields are
        left unchanged.
        """
        body = await request.json()
        price = body.get("price")
        quantity = body.get("quantity")
        trigger_price = body.get("trigger_price")
        order_type = body.get("order_type")
        if all(v is None for v in (price, quantity, trigger_price, order_type)):
            raise HTTPException(
                status_code=400,
                detail="Body must include at least one of: price, quantity, trigger_price, order_type",
            )
        try:
            await ctx.broker.modify_order(
                order_id,
                price=float(price) if price is not None else None,
                quantity=int(quantity) if quantity is not None else None,
                trigger_price=float(trigger_price) if trigger_price is not None else None,
                order_type=str(order_type) if order_type is not None else None,
            )
        except Exception as e:
            logger.exception("modify_broker_order: %s failed", order_id)
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {
            "ok": True, "order_id": order_id,
            "price": price, "quantity": quantity,
            "trigger_price": trigger_price, "order_type": order_type,
        }

    @app.post("/api/broker/gtts/{gtt_id}/cancel")
    async def cancel_broker_gtt(
        gtt_id: int,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a GTT order by id. Also clears the trades.gtt_id
        link when the GTT belonged to a tracked trade so client-side
        exit detection takes over.
        """
        if not hasattr(ctx.broker, "delete_gtt"):
            raise HTTPException(status_code=400, detail="Broker does not support GTT")
        try:
            await ctx.broker.delete_gtt(int(gtt_id))
        except Exception as e:
            logger.exception("cancel_broker_gtt: %s failed", gtt_id)
            raise HTTPException(status_code=502, detail=str(e)) from e
        # Best-effort: clear gtt_id on any trade carrying this GTT so
        # position-monitor's ghost-recovery doesn't see a phantom link.
        try:
            cur = await ctx.db.conn.execute(
                "SELECT trade_id, symbol FROM trades WHERE gtt_id = ?",
                (int(gtt_id),),
            )
            rows = await cur.fetchall()
            for r in rows:
                await ctx.db.set_trade_gtt(r[0], None)
                await ctx.db.log_gtt_event(
                    trade_id=r[0], gtt_id=int(gtt_id), symbol=r[1],
                    event_type="deleted", status="deleted",
                    details={"reason": "user_cancel_via_order_book"},
                )
        except Exception:
            logger.debug("cancel_broker_gtt: trade unlink failed", exc_info=True)
        return {"ok": True, "gtt_id": gtt_id}

    @app.post("/api/positions/{trade_id}/convert")
    async def convert_position(
        trade_id: str,
        body: dict[str, Any],
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Convert an open MIS position to CNC (or back). Promotes a
        winning intraday trade to delivery so it survives the 3:15 PM
        auto-square-off. Caller must ensure sufficient delivery margin
        is available — broker rejection bubbles up as 502.
        """
        trade = await ctx.db.get_trade(trade_id)
        if not trade:
            raise HTTPException(status_code=404, detail="Trade not found")
        if trade.get("status") != "open":
            raise HTTPException(
                status_code=400,
                detail=f"Trade is not open (status={trade.get('status')})",
            )

        current = trade.get("product", "MIS")
        target = (body.get("to_product") or "").upper()
        if target not in ("MIS", "CNC"):
            raise HTTPException(status_code=400, detail="to_product must be MIS or CNC")
        if current == target:
            return {"status": "noop", "product": current}

        ok = await ctx.broker.convert_position(
            symbol=trade["symbol"],
            quantity=int(trade["quantity"]),
            from_product=current,
            to_product=target,
            side=trade["signal_type"],
        )
        if not ok:
            raise HTTPException(
                status_code=502,
                detail=f"Broker rejected {current}->{target} conversion",
            )

        await ctx.db.set_trade_product(trade_id, target)
        try:
            await ctx.notify.send(
                f"Position converted: {trade['symbol']} {current} -> {target}",
                alert_type="trade_exit",
            )
        except Exception:
            logger.debug("convert_position: notify failed", exc_info=True)

        # If we just promoted MIS -> CNC and the trade had MIS broker-side
        # OCO orders (resting LIMIT target + SL), those are now stale —
        # they're product-specific. Cancel both; the user can re-attach a
        # GTT manually or let the next heartbeat see it as CNC and place
        # one automatically via the existing _attach_oco_gtt path on a
        # future code path. For now we leave attach to manual / next-day.
        if current == "MIS" and target == "CNC":
            for oid_key in ("sl_order_id", "target_order_id"):
                oid = trade.get(oid_key)
                if not oid:
                    continue
                try:
                    await ctx.broker.cancel_order(oid)
                except Exception:
                    logger.debug("convert_position: %s cancel failed", oid_key, exc_info=True)

        logger.info(
            "convert_position: %s %s -> %s qty=%d",
            trade["symbol"], current, target, trade["quantity"],
        )
        return {"status": "converted", "trade_id": trade_id, "product": target}


    # Track whether we've already sent a broker-expired Telegram alert this session
    # to avoid spamming on every page load / auto-refresh.
    _broker_expired_alerted = {"sent": False}

