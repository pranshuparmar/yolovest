"""Trade history, detail, equity curve, PnL calendar.

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

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

    @app.get("/api/trades/recent-symbols")
    async def get_recent_traded_symbols(
        limit: int = Query(10, ge=1, le=50),
        _user: str = Depends(verify_credentials),
    ) -> list[str]:
        """Distinct symbols ordered by most-recent trade time. Used by
        the Quick ML Review floater as a sensible default before the
        user types anything. Mode-scoped (matches everything else)."""
        try:
            cur = await ctx.db.read_conn.execute(
                "SELECT symbol, MAX(created_at) AS last_seen FROM trades "
                "WHERE mode = ? GROUP BY symbol "
                "ORDER BY last_seen DESC LIMIT ?",
                (ctx.config.mode, limit),
            )
            rows = await cur.fetchall()
            return [r[0] for r in rows if r[0]]
        except Exception:
            logger.debug("recent-traded-symbols lookup failed", exc_info=True)
            return []

    @app.get("/api/trades/today")
    async def get_todays_trades(
        user: str = Depends(verify_credentials),
        mode: str | None = Query(None, description="Filter by mode: paper, live, or omit for current"),
    ) -> list[dict[str, Any]]:
        """Today's trades."""
        return await ctx.db.get_todays_trades(mode=mode or ctx.config.mode)

    @app.get("/api/trades")
    async def get_trades(
        start: str | None = Query(None, description="Start date YYYY-MM-DD"),
        end: str | None = Query(None, description="End date YYYY-MM-DD"),
        symbol: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        mode: str | None = Query(None, description="Filter by mode: paper, live, or omit for current"),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Trade history with optional date range and symbol filter."""
        return await ctx.db.get_trades_history(
            start_date=start, end_date=end, symbol=symbol, limit=limit,
            mode=mode or ctx.config.mode,
        )

    @app.get("/api/equity-curve")
    async def get_equity_curve(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Daily equity curve data for charting."""
        return await ctx.db.get_equity_curve(days=days, mode=ctx.config.mode)

    @app.get("/api/pnl-calendar")
    async def get_pnl_calendar(
        days: int = Query(90, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Daily PnL for calendar heatmap: {date, pnl, trade_count, wins, losses}."""
        return await ctx.db.get_daily_pnl_calendar(days=days, mode=ctx.config.mode)

    # ------------------------------------------------------------------
    # Trade Detail View
    # ------------------------------------------------------------------

    @app.delete("/api/trades/{trade_id}")
    async def delete_trade(
        trade_id: str, user: str = Depends(verify_credentials)
    ) -> dict[str, Any]:
        """Delete a specific trade record (e.g., ghost/paper trades with wrong mode)."""
        cursor = await ctx.db.conn.execute(
            "DELETE FROM trades WHERE trade_id = ?", (trade_id,),
        )
        await ctx.db.conn.commit()
        if cursor.rowcount > 0:
            logger.info("Deleted trade %s", trade_id)
            return {"success": True, "trade_id": trade_id}
        raise HTTPException(status_code=404, detail="Trade not found")

    @app.get("/api/trades/{trade_id}")
    async def get_trade_detail(
        trade_id: str, user: str = Depends(verify_credentials)
    ) -> dict[str, Any]:
        """Full reasoning chain for a trade: signal → risk → LLM → execution → outcome."""
        import json as _json

        from yolovest.costs import compute_transaction_cost_breakdown

        detail = await ctx.db.get_trade_detail(trade_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Trade not found")

        # Prefer the breakdown captured at close time (broker contract-note when
        # available, config-based estimate otherwise); else compute a live
        # estimate from fill/exit so open trades still see something useful.
        stored = detail.get("realized_costs_json")
        if stored:
            try:
                detail["cost_breakdown"] = _json.loads(stored)
            except (ValueError, TypeError):
                detail["cost_breakdown"] = None
        if not detail.get("cost_breakdown"):
            fill = detail.get("fill_price") or detail.get("entry_price", 0)
            exit_p = detail.get("exit_price") or detail.get("target_price") or fill
            qty = detail.get("quantity") or 0
            product = detail.get("product") or "MIS"
            if fill and qty:
                bd = compute_transaction_cost_breakdown(
                    fill, exit_p, qty, product=product,
                    cost_config=ctx.config.transaction_costs,
                )
                bd["source"] = "estimate"
                detail["cost_breakdown"] = bd

        # GTT lifecycle audit trail — placed, modified, deleted, status
        # changes. Empty for trades that never had a GTT (e.g. MIS).
        try:
            detail["gtt_events"] = await ctx.db.get_gtt_events_for_trade(trade_id)
        except Exception:
            detail["gtt_events"] = []

        return detail

    @app.get("/api/trades/{trade_id}/order-detail")
    async def get_trade_order_detail(
        trade_id: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Broker-side order history + per-fill records for each order id
        attached to a trade (entry / SL / target). Read-only — fetches
        live from Kite each call, no local cache.

        Useful for forensic review of slippage, partial fills, and the
        exact state-transition timeline a broker order went through.
        """
        trade = await ctx.db.get_trade(trade_id)
        if not trade:
            raise HTTPException(status_code=404, detail="Trade not found")

        result: dict[str, Any] = {"trade_id": trade_id, "legs": {}}
        for leg, oid in (
            ("entry", trade.get("order_id")),
            ("sl", trade.get("sl_order_id")),
            ("target", trade.get("target_order_id")),
        ):
            if not oid:
                continue
            history = await ctx.broker.get_order_history(str(oid))
            fills = await ctx.broker.get_order_trades(str(oid))
            result["legs"][leg] = {
                "order_id": oid,
                "history": history,
                "fills": fills,
            }
        return result
