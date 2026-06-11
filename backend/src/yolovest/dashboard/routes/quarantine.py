"""Quarantined symbols and rotation cooldown.

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

    # ------------------------------------------------------------------
    # Symbol Quarantine
    # ------------------------------------------------------------------

    @app.get("/api/quarantined-symbols")
    async def get_quarantined_symbols(
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get all quarantined symbols (auto-blocked after repeated fetch failures)."""
        return await ctx.db.get_quarantined_symbols()

    @app.delete("/api/quarantined-symbols/{symbol}")
    async def unquarantine_symbol(
        symbol: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Remove a symbol from quarantine so it will be fetched again."""
        removed = await ctx.db.unquarantine_symbol(symbol.upper())
        if removed:
            logger.info("Unquarantined symbol %s", symbol.upper())
        return {"success": removed, "symbol": symbol.upper()}

    @app.post("/api/quarantined-symbols/bulk-unblock")
    async def bulk_unquarantine_symbols(
        body: dict[str, Any],
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Unquarantine many symbols in one round-trip.

        Body: {"symbols": ["AAA", "BBB", ...]} (max 500). Returns
        per-symbol success status. Used by the Data Management page's
        multi-select bulk action when an auth outage or transient
        upstream incident sent a wave of valid symbols into quarantine.
        """
        raw = body.get("symbols") or []
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="`symbols` must be a list")
        if len(raw) > 500:
            raise HTTPException(status_code=400, detail="Too many symbols (max 500)")
        symbols = [str(s).strip().upper() for s in raw if str(s).strip()]
        results: dict[str, bool] = {}
        for sym in symbols:
            try:
                results[sym] = bool(await ctx.db.unquarantine_symbol(sym))
            except Exception:
                logger.warning("Failed to unquarantine %s", sym, exc_info=True)
                results[sym] = False
        removed = sum(1 for ok in results.values() if ok)
        logger.info(
            "Bulk-unquarantined %d/%d symbols", removed, len(symbols),
        )
        return {"success": True, "removed": removed, "results": results}

    @app.put("/api/quarantined-symbols/{symbol}/replacement")
    async def set_replacement_symbol(
        symbol: str,
        body: dict[str, Any],
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Set a replacement symbol for a quarantined symbol.

        Send {"replacement": "NEWNAME"} to set, or {"replacement": null} to clear.
        """
        replacement = body.get("replacement")
        if replacement is not None:
            replacement = str(replacement).strip().upper()
            if not replacement:
                replacement = None
        updated = await ctx.db.set_replacement_symbol(symbol, replacement)
        if updated:
            logger.info(
                "Set replacement for quarantined %s -> %s",
                symbol.upper(), replacement,
            )
        return {
            "success": updated,
            "symbol": symbol.upper(),
            "replacement": replacement,
        }

    @app.get("/api/rotation-cooldown")
    async def get_rotation_cooldown(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """List symbols currently held in rotation cooldown + the
        active threshold / window for the user to gauge how aggressive
        the screening rotation is.
        """
        cfg = ctx.config.scanning
        symbols = await ctx.db.get_rotation_cooldown_symbols()
        return {
            "enabled": cfg.rotation_enabled,
            "no_signal_threshold": cfg.rotation_no_signal_threshold,
            "cooldown_hours": cfg.rotation_cooldown_hours,
            "symbols": sorted(symbols),
            "count": len(symbols),
        }

    @app.post("/api/rotation-cooldown/clear")
    async def clear_rotation_cooldown(
        symbol: str | None = Query(None, description="Clear one symbol; omit for all"),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """One-shot reset of rotation cooldown so market-scan reconsiders
        the affected symbols on the next run. Omit `symbol` to clear all.
        """
        cleared = await ctx.db.clear_rotation_cooldown(symbol)
        logger.info(
            "Rotation cooldown cleared: %s (%d rows)",
            symbol or "ALL", cleared,
        )
        return {"success": True, "cleared": cleared, "symbol": symbol}

