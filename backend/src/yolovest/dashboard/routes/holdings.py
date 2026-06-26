"""Broker holdings, ML review, locked holdings, CDSL auth, manual order form.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
)

from yolovest.dashboard.helpers import (
    _build_cdsl_response,
    _compute_cdsl_alert_gate,
    _compute_cdsl_status,
    _is_cdsl_tpin_error,
)
from yolovest.dashboard.ws import broadcast_ws

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials
    # Once-per-session broker-token-expired Telegram alert latch.
    _broker_expired_alerted = {"sent": False}

    @app.get("/api/holdings")
    async def get_holdings(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Zerodha portfolio holdings (CNC/delivery stocks held overnight).

        Returns {holdings: [...], broker_authenticated: true} on success.
        Returns {holdings: [], broker_authenticated: false, login_url: ...}
        when broker token is expired/missing, plus logs and Telegram alert.
        """
        try:
            authenticated = await ctx.broker.is_authenticated()
        except Exception:
            logger.debug("Broker auth check failed for holdings request", exc_info=True)
            authenticated = False

        if not authenticated:
            login_url = ctx.broker.get_login_url()
            logger.warning(
                "Holdings request: broker not authenticated "
                "(token expired or missing)"
            )
            # Send Telegram alert once per session (not on every page load)
            if (
                not _broker_expired_alerted["sent"]
                and ctx.config.notifications.telegram.enabled
                and ctx.config.notifications.telegram.alerts.errors
            ):
                _broker_expired_alerted["sent"] = True
                try:
                    await ctx.notify.send(
                        "Kite session expired — holdings unavailable.\n"
                        f"Re-authenticate: {login_url}\n"
                        "Or use /auth (request_token) in Telegram.",
                        alert_type="errors",
                    )
                except Exception as e:
                    logger.warning("Failed to send broker-expired Telegram alert: %s", e)
            return {
                "holdings": [],
                "broker_authenticated": False,
                "login_url": login_url,
            }

        # Reset alert flag on successful auth
        _broker_expired_alerted["sent"] = False

        try:
            holdings = await ctx.broker.get_holdings()
            # Merge lock status into holdings
            locked_symbols = await ctx.db.get_locked_symbols()
            for h in holdings:
                sym = h.get("tradingsymbol") or h.get("symbol", "")
                h["locked"] = sym in locked_symbols
            return {
                "holdings": holdings,
                "broker_authenticated": True,
            }
        except Exception as e:
            logger.error("Failed to fetch holdings: %s", e)
            raise HTTPException(
                status_code=502,
                detail=f"Broker error: {e}. Token may be expired — re-authenticate via Settings.",
            ) from e

    @app.post("/api/review")
    async def review_endpoint(
        body: dict[str, Any] | None = None,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Run ML review on any symbols and return recommendations.

        Body: {"symbols": ["SYM1", "SYM2"]} — review specific symbols.
        Omit body or symbols to review all current holdings.
        Works for any NSE symbol, whether held or not.
        """
        from yolovest.review import review_symbols
        return await review_symbols(ctx, (body or {}).get("symbols"))

    @app.get("/api/locked-holdings")
    async def get_locked_holdings(
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get all locked holdings."""
        return await ctx.db.get_locked_holdings()

    @app.post("/api/locked-holdings/{symbol}")
    async def lock_holding(
        symbol: str,
        notes: str | None = Query(default=None),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Lock a holding — YoloVest will not sell this symbol."""
        await ctx.db.lock_symbol(symbol, notes)
        logger.info("Locked holding: %s (notes: %s)", symbol, notes)
        return {"success": True, "symbol": symbol.upper(), "locked": True}

    @app.post("/api/locked-holdings/bulk")
    async def bulk_lock_holdings(
        body: dict[str, Any],
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Bulk lock or unlock multiple holdings.

        Body: {"symbols": ["SYM1", "SYM2"], "action": "lock" | "unlock", "notes": "optional"}
        """
        symbols = body.get("symbols", [])
        action = body.get("action", "lock")
        notes = body.get("notes")
        if not symbols:
            raise HTTPException(status_code=400, detail="symbols list is required")
        results = {}
        for sym in symbols:
            sym = sym.upper()
            if action == "lock":
                await ctx.db.lock_symbol(sym, notes)
                results[sym] = "locked"
            else:
                removed = await ctx.db.unlock_symbol(sym)
                results[sym] = "unlocked" if removed else "not_found"
        logger.info("Bulk %s: %s", action, results)
        return {"success": True, "action": action, "results": results}

    @app.delete("/api/locked-holdings/{symbol}")
    async def unlock_holding(
        symbol: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Unlock a holding — YoloVest can sell this symbol again."""
        removed = await ctx.db.unlock_symbol(symbol)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{symbol} was not locked")
        logger.info("Unlocked holding: %s", symbol)
        return {"success": True, "symbol": symbol.upper(), "locked": False}

    @app.get("/api/broker/cdsl-status")
    async def get_cdsl_status(
        refresh: bool = Query(False, description="Force a live broker fetch"),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Current CDSL TPIN authorisation status across the user's
        holdings. Used by the dashboard banner (and the cdsl-auth-check
        CRON skill) to alert when CNC sells will require TPIN today.

        By default returns the cached snapshot from system_state
        (refreshed by the CRON skill at market open, ~9:20 IST).
        Pass `refresh=true` to force a live fetch — useful from the
        banner's "Refresh" button after the user has completed the
        auth flow in another tab.
        """
        import json as _json

        if not refresh:
            cached = await ctx.db.get_system_state("cdsl_auth_status")
            if cached:
                try:
                    return _json.loads(cached)
                except (ValueError, TypeError):
                    pass

        if not await ctx.broker.is_authenticated():
            return {
                "authenticated": False, "needs_auth": False,
                "checked_at": None,
                "pending_symbols": [], "pending_count": 0,
                "ddpi_likely_enabled": False,
            }

        try:
            holdings = await ctx.broker.get_holdings()
        except Exception as e:
            logger.warning("cdsl-status: get_holdings failed: %s", e)
            return {
                "authenticated": True, "needs_auth": False,
                "checked_at": None,
                "error": "could not fetch holdings from broker",
                "pending_symbols": [], "pending_count": 0,
                "ddpi_likely_enabled": False,
            }

        status = _compute_cdsl_status(holdings or [])
        gate = await _compute_cdsl_alert_gate(ctx)
        from yolovest.timezone import now_utc as _now_utc
        result = {
            "authenticated": True,
            **status,
            **gate,
            # alert_needed: the field the UI banner and CRON skill key
            # off. Unauthorised holdings alone don't trigger an alert —
            # there has to be something that might try to sell today.
            "alert_needed": status["needs_auth"] and gate["has_active_cnc_exits"],
            "checked_at": _now_utc().isoformat(),
        }
        # Persist the latest live read so the next dashboard tick
        # gets a fresh value without re-hitting the broker.
        try:
            await ctx.db.set_system_state(
                "cdsl_auth_status", _json.dumps(result),
            )
        except Exception:
            logger.debug("cdsl-status: cache write failed", exc_info=True)
        return result

    @app.post("/api/broker/holdings-auth")
    async def initiate_holdings_authorisation(
        body: dict[str, Any] | None = None,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Proactively start the CDSL TPIN flow without waiting for a
        failed sell order. Useful as a one-click "authorize my
        holdings for today" action before the user starts selling.

        Body (optional): {"holdings": [{"isin": "INE...", "quantity": 5}, ...]}
        When omitted, defaults to authorising every current holding.

        Returns the same shape as the CDSL error response so the UI
        can reuse the same "Open Auth" button component.
        """
        body = body or {}
        explicit_holdings = body.get("holdings") if isinstance(body, dict) else None
        return await _build_cdsl_response(ctx, 
            "Holdings authorisation initiated by user",
        ) if explicit_holdings is None else (
            await _build_cdsl_response(ctx, "Authorising specified holdings")
        )

    @app.post("/api/orders")
    async def place_manual_order(
        body: dict[str, Any],
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Place a manual order (buy/sell) via the broker.

        Required fields: symbol, side (BUY/SELL), quantity, order_type, product
        Optional: price (for LIMIT orders), trigger_price (for SL orders)
        """
        symbol = body.get("symbol", "").strip().upper()
        side = body.get("side", "").strip().upper()
        quantity = int(body.get("quantity", 0))
        order_type = body.get("order_type", "MARKET").strip().upper()
        product = body.get("product", "CNC").strip().upper()
        price = body.get("price")
        trigger_price = body.get("trigger_price")

        if not symbol:
            raise HTTPException(status_code=400, detail="symbol is required")
        if side not in ("BUY", "SELL"):
            raise HTTPException(status_code=400, detail="side must be BUY or SELL")
        if quantity <= 0:
            raise HTTPException(status_code=400, detail="quantity must be > 0")
        if product not in ("CNC", "MIS"):
            raise HTTPException(status_code=400, detail="product must be CNC or MIS")
        if order_type not in ("MARKET", "LIMIT", "SL", "SL-M"):
            raise HTTPException(status_code=400, detail="order_type must be MARKET, LIMIT, SL, or SL-M")
        if order_type == "LIMIT" and not price:
            raise HTTPException(status_code=400, detail="price is required for LIMIT orders")

        try:
            order_id = await ctx.broker.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                product=product,
                price=float(price) if price else None,
                trigger_price=float(trigger_price) if trigger_price else None,
                tag="yv-manual",
            )

            # Record trade in DB
            fill_price = float(price) if price else 0.0
            trade = {
                "symbol": symbol,
                "signal_type": side,
                "entry_price": fill_price,
                "fill_price": fill_price,
                "quantity": quantity,
                "stop_loss_price": 0,
                "target_price": 0,
                "order_id": order_id,
                "product": product,
                "status": "filled" if order_type == "MARKET" else "placed",
                "mode": ctx.config.mode,
                "slippage": 0,
            }
            trade_id = await ctx.db.insert_trade(trade)

            logger.info(
                "Manual order placed: %s %s %s x%d @ %s (order_id: %s, trade_id: %s)",
                side, symbol, order_type, quantity, price or "MARKET", order_id, trade_id,
            )
            await broadcast_ws("trade_executed", {
                "symbol": symbol,
                "signal_type": side,
                "quantity": quantity,
                "mode": ctx.config.mode,
                "manual": True,
                "trade_id": trade_id,
            })
            return {"success": True, "order_id": order_id, "trade_id": trade_id}
        except Exception as e:
            logger.warning("Manual order failed: %s", e)
            msg = str(e)
            # CDSL TPIN: surface a structured response so the UI can
            # render an "Authorize at CDSL" action button instead of
            # just dumping the broker's raw error string.
            if _is_cdsl_tpin_error(msg):
                return await _build_cdsl_response(ctx, 
                    msg,
                    triggered_by={
                        "symbol": symbol,
                        "quantity": quantity,
                        "side": side,
                        "source": "manual-order",
                    },
                )
            return {
                "success": False,
                "error": "Order rejected by broker — see server logs for the reason.",
            }

