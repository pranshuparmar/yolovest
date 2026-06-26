"""Portfolio, funds and capital endpoints.

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
    _compute_capital_breakdown,
    _compute_total_capital,
)

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

    # ------------------------------------------------------------------
    # Portfolio Overview
    # ------------------------------------------------------------------

    @app.get("/api/funds")
    async def get_funds(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Live funds / margins snapshot from the broker.

        Returns the raw broker.get_margins() payload alongside a parsed
        summary so the UI can render the high-signal numbers without
        knowing the Kite-specific schema. Used by the Funds page to
        give a complete "what's in my account right now" view —
        equivalent to Kite's Funds tab — so the user doesn't need to
        log into Zerodha to check available cash, used margin, payout
        balance, etc.
        """
        if not await ctx.broker.is_authenticated():
            return {
                "authenticated": False,
                "raw": None,
                "summary": {
                    "available_cash": 0.0,
                    "live_balance": 0.0,
                    "opening_balance": 0.0,
                    "utilised_margin": 0.0,
                    "m2m_unrealised": 0.0,
                    "m2m_realised": 0.0,
                    "payout": 0.0,
                    "collateral": 0.0,
                    "exposure": 0.0,
                    "span": 0.0,
                    "delivery": 0.0,
                    "net": 0.0,
                },
            }

        try:
            raw = await ctx.broker.get_margins()
        except Exception as e:
            logger.exception("get_funds: broker.get_margins failed")
            raise HTTPException(
                status_code=502,
                detail=f"Broker margins fetch failed: {e}",
            ) from e

        # Parse the Kite equity segment into a flat summary. Commodity
        # is intentionally ignored — the platform is equity-only.
        equity: dict[str, Any] = (raw or {}).get("equity", {}) or {}
        avail: dict[str, Any] = equity.get("available", {}) or {}
        util: dict[str, Any] = equity.get("utilised", {}) or {}

        def _f(d: dict[str, Any], key: str) -> float:
            try:
                return float(d.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        summary = {
            "available_cash": _f(avail, "cash"),
            "live_balance": _f(avail, "live_balance"),
            "opening_balance": _f(avail, "opening_balance"),
            "adhoc_margin": _f(avail, "adhoc_margin"),
            "intraday_payin": _f(avail, "intraday_payin"),
            "collateral": _f(avail, "collateral"),
            "utilised_margin": _f(util, "debits"),
            "m2m_unrealised": _f(util, "m2m_unrealised"),
            "m2m_realised": _f(util, "m2m_realised"),
            "payout": _f(util, "payout"),
            "exposure": _f(util, "exposure"),
            "span": _f(util, "span"),
            "delivery": _f(util, "delivery"),
            "option_premium": _f(util, "option_premium"),
            "turnover": _f(util, "turnover"),
            "net": _f(equity, "net"),
        }
        return {
            "authenticated": True,
            "enabled": bool(equity.get("enabled", True)),
            "raw": raw,
            "summary": summary,
        }

    @app.get("/api/funds/history")
    async def get_funds_history(
        days: int = Query(90, ge=1, le=365),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Daily funds/margins history from the funds-snapshot CRON.

        Mode-scoped to the current trading mode (paper / live). Used
        by the Funds page to render the cash + holdings + used-margin
        trail so the user can see daily movements without Kite.
        """
        snapshots = await ctx.db.get_funds_snapshots(
            mode=ctx.config.mode, days=days,
        )
        return {"snapshots": snapshots, "count": len(snapshots)}

    @app.get("/api/portfolio")
    async def get_portfolio(user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Portfolio overview: capital, exposure, open positions, PnL.

        If broker is authenticated, syncs available funds from Zerodha.
        """
        # Refresh live capital breakdown (cash + utilised + holdings) on every read.
        # initial_capital is the deposited baseline — set once at bootstrap and via
        # explicit /api/capital or /api/capital/sync; never overwritten here.
        try:
            if await ctx.broker.is_authenticated():
                bd = await _compute_capital_breakdown(ctx.broker)
                if bd["total"] > 0:
                    import json as _json
                    await ctx.db.set_system_state("capital_breakdown", _json.dumps(bd))
                    logger.info("Portfolio: synced broker capital ₹%.2f (cash=%.2f, used=%.2f, hold=%.2f)",
                                bd["total"], bd["available_cash"], bd["utilised_margin"], bd["holdings_current"])
                else:
                    logger.warning("Portfolio: total broker capital is 0, keeping previous value")
        except Exception:
            logger.debug("Broker capital sync failed, using DB value", exc_info=True)

        portfolio = await ctx.db.get_portfolio_state(mode=ctx.config.mode)
        return portfolio

    @app.post("/api/capital")
    async def update_capital(
        body: dict[str, Any],
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Manually update the initial capital amount."""
        amount = body.get("amount")
        if amount is None or float(amount) <= 0:
            raise HTTPException(status_code=400, detail="amount must be a positive number")
        await ctx.db.set_system_state("initial_capital", str(float(amount)))
        return {"success": True, "initial_capital": float(amount)}

    @app.post("/api/capital/sync")
    async def sync_capital_from_broker(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Sync capital (cash + holdings value) from Zerodha broker account."""
        try:
            if not await ctx.broker.is_authenticated():
                return {"success": False, "error": "Broker not authenticated"}
            broker_capital = await _compute_total_capital(ctx.broker)
            if broker_capital <= 0:
                return {"success": False, "error": "Broker reported zero total capital"}
            await ctx.db.set_system_state("initial_capital", str(broker_capital))
            return {"success": True, "initial_capital": broker_capital}
        except Exception as e:
            logger.warning("initial-capital sync failed: %s", e)
            return {"success": False, "error": "Failed to sync initial capital"}

