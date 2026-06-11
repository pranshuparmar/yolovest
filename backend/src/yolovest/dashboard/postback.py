"""Order-postback business logic shared by the HTTP postback endpoint
and the KiteTicker order_update bridge."""

import logging
from typing import Any

from yolovest.context import AppContext

logger = logging.getLogger(__name__)

async def _apply_order_postback(
    ctx: AppContext, order_id: str, status: str, body: dict[str, Any],
) -> None:
    """Route a Zerodha postback to the matching trade row and act on it.

    Terminal statuses (COMPLETE / CANCELLED / REJECTED) handled here so
    trade-row state updates within seconds of the broker event rather
    than waiting for the next position-monitor heartbeat. Polling is
    still authoritative — this is a latency optimisation, not a
    replacement for `kite.orders()` reconciliation.
    """
    trade, leg = await ctx.db.find_trade_by_order_id(order_id)
    if not trade:
        # Could be a GTT-triggered order (we don't track that order_id
        # locally — ghost recovery cleans up the position when broker
        # qty hits zero) or an order placed outside the system. Log and
        # move on; ghost recovery is the safety net.
        logger.info(
            "Postback for order=%s status=%s — no matching local trade",
            order_id, status,
        )
        return

    symbol = trade.get("symbol")
    trade_id = trade.get("trade_id")
    if trade_id is None:
        logger.warning("Postback %s: trade row has no trade_id — skipping", symbol)
        return
    log_prefix = f"Postback {symbol} {trade_id} {leg}={order_id}"

    if leg == "entry":
        # Entry-leg lifecycle
        if status == "REJECTED":
            logger.warning("%s: entry REJECTED — marking trade failed", log_prefix)
            # Late-rejection cleanup. _verify_fill cancels the SL/target
            # legs inline when the entry rejects during synchronous
            # placement, but if Zerodha returned COMPLETE (or we hit the
            # verify timeout) and the exchange flips to REJECTED seconds
            # later, the cancel sweep here is the only protection
            # against orphaned resting orders. An armed SL-M on a
            # nonexistent position would fire on a downward move and
            # create an unintended short.
            for leg_name, oid in (
                ("sl", trade.get("sl_order_id")),
                ("target", trade.get("target_order_id")),
            ):
                if not oid:
                    continue
                try:
                    await ctx.broker.cancel_order(oid)
                    logger.info(
                        "%s: cancelled orphan %s order %s after entry REJECTED",
                        log_prefix, leg_name, oid,
                    )
                except Exception:
                    logger.warning(
                        "%s: failed to cancel orphan %s order %s",
                        log_prefix, leg_name, oid, exc_info=True,
                    )
            try:
                await ctx.db.conn.execute(
                    "UPDATE trades SET status = 'failed', "
                    "sl_order_id = NULL, target_order_id = NULL "
                    "WHERE trade_id = ?",
                    (trade_id,),
                )
                await ctx.db.conn.commit()
            except Exception:
                logger.exception("%s: failed to mark trade failed", log_prefix)
            await ctx.notify.send(
                f"Trade entry REJECTED: {symbol} ({order_id})\n"
                f"Reason: {body.get('status_message') or 'see Zerodha'}",
                alert_type="errors",
            )
        elif status == "COMPLETE":
            # Most entries already get marked filled by verify_fill at
            # placement time; the postback may arrive after we've moved
            # on. Update fill_price + slippage if not already set.
            try:
                fill_price = float(body.get("average_price") or 0)
            except (TypeError, ValueError):
                fill_price = 0.0
            if fill_price > 0 and not trade.get("fill_price"):
                slippage = abs(fill_price - float(trade.get("entry_price") or 0))
                await ctx.db.conn.execute(
                    "UPDATE trades SET fill_price = ?, slippage = ?, status = 'open' "
                    "WHERE trade_id = ? AND fill_price IS NULL",
                    (fill_price, slippage, trade_id),
                )
                await ctx.db.conn.commit()
                logger.info("%s: filled @ %.2f (slippage %.2f)", log_prefix, fill_price, slippage)
        elif status == "CANCELLED":
            # Usually expected — we cancelled it ourselves on retry/timeout.
            logger.info("%s: entry CANCELLED", log_prefix)

    elif leg == "sl":
        if status == "COMPLETE":
            # Broker-side SL fired — position is closed at broker. Cancel
            # the resting target leg and close the DB row inline using
            # the fill price from the postback. Ghost recovery is the
            # safety net if anything below raises.
            target_oid = trade.get("target_order_id")
            if target_oid:
                try:
                    await ctx.broker.cancel_order(target_oid)
                    await ctx.db.set_trade_target_order_id(trade_id, None)
                except Exception:
                    logger.debug("%s: target cancel after SL fill failed", log_prefix, exc_info=True)
            await _close_on_exit_fill(ctx, trade, body, leg="sl", log_prefix=log_prefix)
        elif status == "REJECTED":
            logger.warning("%s: SL order REJECTED — position is unprotected!", log_prefix)
            await ctx.notify.send(
                f"WARNING: SL order REJECTED for {symbol} ({order_id})\n"
                f"Position is UNPROTECTED. Reason: {body.get('status_message') or 'see Zerodha'}",
                alert_type="errors",
            )

    elif leg == "target":
        if status == "COMPLETE":
            # Target LIMIT filled — same shape as SL fill: cancel the
            # other leg and close the DB row inline.
            sl_oid = trade.get("sl_order_id")
            if sl_oid:
                try:
                    await ctx.broker.cancel_order(sl_oid)
                    await ctx.db.set_trade_sl_order_id(trade_id, None)
                except Exception:
                    logger.debug("%s: SL cancel after target fill failed", log_prefix, exc_info=True)
            await _close_on_exit_fill(ctx, trade, body, leg="target", log_prefix=log_prefix)


async def _close_on_exit_fill(
    ctx: AppContext,
    trade: dict[str, Any],
    body: dict[str, Any],
    leg: str,
    log_prefix: str,
) -> None:
    """Close the DB row immediately when a broker-side exit leg (SL or
    target) reports COMPLETE, using the fill price from the postback
    body. Heartbeat ghost-recovery remains the backstop for postbacks
    that get dropped (Kite doesn't retry).

    Idempotent: if the trade is already marked closed (e.g. duplicate
    postback via both HTTP + WebSocket channels), this returns early.
    """
    trade_id = trade.get("trade_id")
    if trade_id is None:
        return
    if (trade.get("status") or "").lower() == "closed":
        return

    try:
        exit_price = float(body.get("average_price") or 0)
    except (TypeError, ValueError):
        exit_price = 0.0
    if exit_price <= 0:
        # No fill price in the postback — let ghost recovery handle it
        # using kite.trades() lookup. Don't synthesize a number.
        logger.info(
            "%s: %s COMPLETE without average_price; deferring to ghost recovery",
            log_prefix, leg.upper(),
        )
        return

    entry = float(trade.get("fill_price") or trade.get("entry_price") or 0)
    qty = int(trade.get("quantity") or 0)
    if entry <= 0 or qty <= 0:
        logger.warning(
            "%s: cannot close on %s fill — entry=%.2f qty=%d invalid",
            log_prefix, leg.upper(), entry, qty,
        )
        return

    signal_type = trade.get("signal_type", "BUY")
    if signal_type == "BUY":
        gross_pnl = (exit_price - entry) * qty
    else:
        gross_pnl = (entry - exit_price) * qty

    product = trade.get("product", "MIS")
    try:
        from yolovest.costs import resolve_round_trip_costs

        costs, _src, breakdown = await resolve_round_trip_costs(
            ctx.broker, symbol=trade["symbol"], signal_type=signal_type,
            entry_price=entry, exit_price=exit_price, quantity=qty,
            product=product, cost_config=ctx.config.transaction_costs,
        )
    except Exception:
        logger.exception("%s: cost resolution failed, using gross PnL", log_prefix)
        costs, breakdown = 0.0, None

    pnl = round(gross_pnl - costs, 2)
    try:
        await ctx.db.close_position(
            trade_id, exit_price, pnl, realized_costs=breakdown,
        )
    except Exception:
        logger.exception("%s: close_position failed after %s fill", log_prefix, leg.upper())
        return

    logger.info(
        "%s: %s filled @ %.2f, closed inline — pnl ₹%.0f (gross ₹%.0f, costs ₹%.0f)",
        log_prefix, leg.upper(), exit_price, pnl, gross_pnl, costs,
    )

