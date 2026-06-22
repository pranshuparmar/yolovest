"""Per-symbol context, OHLCV, LTP, strategy/execution analytics, correlations, alerts.

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
)

from yolovest.data.ohlcv_cache import get_ohlcv_cached

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

    # ------------------------------------------------------------------
    # Symbol Deep-Dive (Feature #3)
    # ------------------------------------------------------------------

    @app.get("/api/symbol/{symbol}/context")
    async def get_symbol_context(
        symbol: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Symbol detail-page extras: quarantine status, recent bulk
        deals, average delivery %, latest signal + top-5 attribution.
        Composed in one round-trip so the page doesn't fire N queries
        on load.
        """
        sym = symbol.upper()
        try:
            quarantined = await ctx.db.get_quarantined_symbols()
        except Exception:
            quarantined = []
        q_entry = next(
            (q for q in quarantined if q.get("symbol", "").upper() == sym), None,
        )
        try:
            bulk = await ctx.db.get_bulk_deals_list(days=30, symbol=sym, limit=20)
        except Exception:
            bulk = []
        try:
            delivery_avg = await ctx.db.get_recent_delivery_pct(sym, lookback_days=5)
        except Exception:
            delivery_avg = None

        # Latest signal + TreeSHAP top-5 attribution. Mode-scoped so
        # paper-mode signals don't leak into a live view.
        latest_signal: dict[str, Any] | None = None
        try:
            cur = await ctx.db.read_conn.execute(
                "SELECT signal_type, confidence_score, attribution_json, "
                "disposition, created_at "
                "FROM signals WHERE symbol = ? AND mode = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (sym, ctx.config.mode),
            )
            row = await cur.fetchone()
            if row:
                import json as _json

                attribution: list[dict[str, Any]] = []
                if row[2]:
                    try:
                        parsed = _json.loads(row[2])
                        if isinstance(parsed, list):
                            attribution = parsed[:5]
                    except Exception:
                        attribution = []
                latest_signal = {
                    "signal_type": row[0],
                    "confidence_score": row[1],
                    "disposition": row[3],
                    "created_at": row[4],
                    "attribution": attribution,
                }
        except Exception:
            logger.debug("symbol context: latest_signal lookup failed", exc_info=True)

        return {
            "quarantine": q_entry,
            "recent_bulk_deals": bulk,
            "delivery_pct_avg_5d": delivery_avg,
            "latest_signal": latest_signal,
        }

    @app.get("/api/symbol/{symbol}/quick-context")
    async def get_symbol_quick_context(
        symbol: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Compact context for the floating Quick ML Review widget.

        One round-trip: sector, last 8 daily bars (LTP/open/prev close/
        7d perf/avg vol), quarantine + lock status, current open
        position (if any), and today's signal disposition (if any).

        Designed to be cheap: no bulk-deals, no TreeSHAP attribution —
        the full /context endpoint covers that for the detail page.
        """
        sym = symbol.upper()

        sector: str | None = None
        try:
            cur = await ctx.db.read_conn.execute(
                "SELECT sector, industry FROM symbol_sectors WHERE symbol = ?",
                (sym,),
            )
            row = await cur.fetchone()
            if row:
                sector = row[0] or row[1]
        except Exception:
            logger.debug("quick-context sector lookup failed", exc_info=True)

        # OHLCV from the DB (ingested universe), falling back to an on-demand
        # provider fetch so the floater shows price history for ANY NSE symbol,
        # not just the ingested watchlist. Transient — not persisted.
        ohlcv_bars: list[Any] = []
        try:
            ohlcv_bars = await ctx.db.get_ohlcv(sym, "daily", days=30)
            if not ohlcv_bars or len(ohlcv_bars) < 10:
                fetched = await get_ohlcv_cached(ctx.market_data, sym, 30)
                if fetched and len(fetched) > len(ohlcv_bars or []):
                    ohlcv_bars = fetched
        except Exception:
            logger.debug("quick-context ohlcv lookup failed", exc_info=True)

        bars: list[dict[str, Any]] = [
            {
                "timestamp": b.timestamp.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in (ohlcv_bars or [])[-10:]
        ]

        avg_volume_20d: float | None = None
        recent_vols = [b.volume for b in (ohlcv_bars or [])[-20:] if b.volume]
        if recent_vols:
            avg_volume_20d = sum(recent_vols) / len(recent_vols)

        ltp: float | None = None
        try:
            quote = await ctx.market_data.get_quote(sym)
            ltp = float(quote.get("last_price") or 0) or None
        except Exception:
            logger.debug("quick-context LTP fetch failed", exc_info=True)

        is_quarantined = False
        quarantine_reason: str | None = None
        try:
            quarantined = await ctx.db.get_quarantined_symbols()
            q_entry = next(
                (q for q in quarantined if (q.get("symbol") or "").upper() == sym),
                None,
            )
            if q_entry:
                is_quarantined = True
                quarantine_reason = q_entry.get("reason")
        except Exception:
            logger.debug("quick-context quarantine lookup failed", exc_info=True)

        is_locked = False
        try:
            locked = await ctx.db.get_locked_holdings()
            is_locked = any(
                (h.get("symbol") or "").upper() == sym for h in locked
            )
        except Exception:
            logger.debug("quick-context lock lookup failed", exc_info=True)

        open_position: dict[str, Any] | None = None
        try:
            positions = await ctx.db.get_open_positions(mode=ctx.config.mode)
            for p in positions:
                if (p.get("symbol") or "").upper() == sym:
                    open_position = {
                        "signal_type": p.get("signal_type"),
                        "quantity": p.get("quantity"),
                        "fill_price": p.get("fill_price"),
                        "entry_price": p.get("entry_price"),
                        "target_price": p.get("target_price"),
                        "stop_loss_price": p.get("stop_loss_price"),
                        "product": p.get("product"),
                    }
                    break
        except Exception:
            logger.debug("quick-context open positions lookup failed", exc_info=True)

        todays_signal: dict[str, Any] | None = None
        try:
            from yolovest.timezone import now_ist

            today_str = now_ist().date().isoformat()
            cur = await ctx.db.read_conn.execute(
                "SELECT signal_type, confidence_score, disposition, "
                "disposition_reason, created_at "
                "FROM signals WHERE symbol = ? AND mode = ? "
                "AND DATE(created_at) = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (sym, ctx.config.mode, today_str),
            )
            row = await cur.fetchone()
            if row:
                todays_signal = {
                    "signal_type": row[0],
                    "confidence_score": row[1],
                    "disposition": row[2],
                    "disposition_reason": row[3],
                    "created_at": row[4],
                }
        except Exception:
            logger.debug("quick-context todays signal lookup failed", exc_info=True)

        return {
            "symbol": sym,
            "sector": sector,
            "ltp": ltp,
            "bars": bars,
            "avg_volume_20d": avg_volume_20d,
            "quarantine": {"is_quarantined": is_quarantined, "reason": quarantine_reason},
            "is_locked": is_locked,
            "open_position": open_position,
            "todays_signal": todays_signal,
        }

    @app.get("/api/symbol/{symbol}/ohlcv")
    async def get_symbol_ohlcv(
        symbol: str,
        days: int = Query(60, ge=1, le=365),
        interval: str = Query("daily"),
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """OHLCV bars for a symbol with delivery_pct overlay. The DB
        stores daily bars under the canonical interval "daily"
        (matching ingester / market-scan conventions); "1d" / "1day"
        are normalised so older frontend builds still work.
        """
        from datetime import timedelta

        from yolovest.timezone import now_ist
        iv = interval.lower()
        if iv in ("1d", "1day", "day"):
            iv = "daily"
        # Direct query so we can include delivery_pct alongside the
        # standard OHLCV columns without round-tripping through
        # OHLCVBar (which doesn't have a delivery_pct field).
        cutoff = (now_ist() - timedelta(days=days)).isoformat()
        cursor = await ctx.db.read_conn.execute(
            "SELECT timestamp, open, high, low, close, volume, delivery_pct "
            "FROM ohlcv WHERE symbol = ? AND interval = ? AND timestamp >= ? "
            "ORDER BY timestamp",
            (symbol.upper(), iv, cutoff),
        )
        rows = await cursor.fetchall()
        if not rows and iv == "daily":
            # Not in the ingested universe — fetch daily bars on demand so the
            # deep-dive chart works for ANY NSE symbol (no delivery_pct overlay
            # for these; that's only stored for ingested bars). Transient.
            try:
                fetched = await get_ohlcv_cached(ctx.market_data, symbol.upper(), days)
                return [
                    {
                        "timestamp": b.timestamp.isoformat(),
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                        "delivery_pct": None,
                    }
                    for b in (fetched or [])
                ]
            except Exception:
                logger.debug(
                    "symbol ohlcv: on-demand fetch failed for %s", symbol,
                    exc_info=True,
                )
        return [
            {
                "timestamp": r[0],
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "volume": r[5],
                "delivery_pct": r[6],
            }
            for r in rows
        ]

    @app.get("/api/ltp")
    async def get_ltp_batch(
        symbols: str = Query(..., description="Comma-separated symbol list"),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, float]:
        """Best-effort LTP map for arbitrary symbols.

        Order of preference per symbol — same chain that powers open
        positions, so the trade-history table renders identical LTPs
        for open and recently-closed rows:
          1. KiteTicker cache (sub-second real-time when ticker is on
             and the symbol is subscribed)
          2. market_data.get_ltp() — live Kite REST quote (or jugaad /
             yfinance fallback) for symbols the WS hasn't subscribed
          3. Last OHLCV close from local DB as a final stale fallback
             so an offline market still shows a price

        Symbols that resolve to no price are omitted from the response.
        REST fetches run concurrently so a 30-symbol page doesn't
        serialise into a 30 × round-trip wait.
        """
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        result: dict[str, float] = {}
        ticker = getattr(ctx, "ticker", None)

        async def _resolve(sym: str) -> tuple[str, float | None]:
            # 1) WS tick cache (subscribed symbols only).
            if ticker is not None:
                try:
                    ltp = ticker.get_ltp(sym, max_age_sec=600.0)
                    if ltp is not None and ltp > 0:
                        return sym, float(ltp)
                except Exception:
                    pass
            # 2) Live REST quote through the provider chain.
            try:
                ltp = await ctx.market_data.get_ltp(sym)
                if ltp is not None and ltp > 0:
                    return sym, float(ltp)
            except Exception:
                pass
            # 3) Stale last-known close from local OHLCV.
            try:
                row = list(await ctx.db.read_conn.execute_fetchall(
                    "SELECT close FROM ohlcv WHERE symbol = ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (sym,),
                ))
                if row and row[0][0] is not None:
                    return sym, float(row[0][0])
            except Exception:
                pass
            return sym, None

        pairs = await asyncio.gather(*[_resolve(s) for s in syms])
        for sym, ltp in pairs:
            if ltp is not None and ltp > 0:
                result[sym] = ltp
        return result

    @app.get("/api/symbol/{symbol}/trades")
    async def get_symbol_trades(
        symbol: str,
        limit: int = Query(50, ge=1, le=200),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Trades for a specific symbol."""
        return await ctx.db.get_symbol_trades(symbol.upper(), limit, mode=ctx.config.mode)

    @app.get("/api/symbol/{symbol}/predictions")
    async def get_symbol_predictions(
        symbol: str, user: str = Depends(verify_credentials)
    ) -> list[dict[str, Any]]:
        """Predictions for a specific symbol."""
        return await ctx.db.get_symbol_predictions(symbol.upper(), mode=ctx.config.mode)

    # ------------------------------------------------------------------
    # Strategy Performance (Feature #5)
    # ------------------------------------------------------------------

    @app.get("/api/strategy-performance")
    async def get_strategy_performance(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Aggregate trade performance by signal type, product, sector, time, holding period."""
        return await ctx.db.get_strategy_performance(mode=ctx.config.mode)

    # ------------------------------------------------------------------
    # Execution Quality (Feature #8)
    # ------------------------------------------------------------------

    @app.get("/api/execution-quality")
    async def get_execution_quality(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Detailed execution quality metrics: slippage by hour/size, fill rate."""
        return await ctx.db.get_execution_quality(days=days, mode=ctx.config.mode)

    # ------------------------------------------------------------------
    # Correlation Data (Feature #7)
    # ------------------------------------------------------------------

    @app.get("/api/correlations")
    async def get_correlations(
        days: int = Query(60, ge=7, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Correlation matrix for open positions' symbols."""
        positions = await ctx.db.get_open_positions(mode=ctx.config.mode)
        watchlist = await ctx.db.get_watchlist()
        # Use symbols from positions + top watchlist
        symbols = list({p.get("symbol", "") for p in positions if p.get("symbol")})
        wl_symbols = [w.get("symbol", "") for w in watchlist[:10] if w.get("symbol")]
        for s in wl_symbols:
            if s not in symbols:
                symbols.append(s)
        symbols = symbols[:15]  # Cap at 15

        if len(symbols) < 2:
            return {"symbols": symbols, "matrix": [], "data": {}}

        ohlcv = await ctx.db.get_ohlcv_multi(symbols, days)

        # Compute returns and correlation
        import math
        returns: dict[str, list[float]] = {}
        for sym, bars in ohlcv.items():
            if len(bars) < 2:
                continue
            r = []
            for i in range(1, len(bars)):
                prev = bars[i - 1]["close"]
                curr = bars[i]["close"]
                if prev and prev > 0:
                    r.append((curr - prev) / prev)
            if r:
                returns[sym] = r

        valid_symbols = [s for s in symbols if s in returns]

        # Pearson correlation
        def pearson(x: list[float], y: list[float]) -> float:
            n = min(len(x), len(y))
            if n < 3:
                return 0.0
            x, y = x[:n], y[:n]
            mx = sum(x) / n
            my = sum(y) / n
            num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
            dx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
            dy = math.sqrt(sum((yi - my) ** 2 for yi in y))
            if dx == 0 or dy == 0:
                return 0.0
            return round(num / (dx * dy), 3)

        matrix: list[list[float]] = []
        for s1 in valid_symbols:
            row = []
            for s2 in valid_symbols:
                if s1 == s2:
                    row.append(1.0)
                else:
                    row.append(pearson(returns[s1], returns[s2]))
            matrix.append(row)

        return {"symbols": valid_symbols, "matrix": matrix}

    # ------------------------------------------------------------------
    # Price Alerts (Feature #4)
    # ------------------------------------------------------------------

    @app.get("/api/alerts")
    async def get_alerts(
        active_only: bool = Query(True),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get price alerts."""
        return await ctx.db.get_price_alerts(active_only=active_only)

    @app.post("/api/alerts")
    async def create_alert(
        body: dict[str, Any],
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Create a price alert."""
        symbol = body.get("symbol", "").strip().upper()
        target_price = body.get("target_price")
        direction = body.get("direction", "above")
        note = body.get("note")
        if not symbol or target_price is None:
            raise HTTPException(status_code=400, detail="symbol and target_price required")
        if direction not in ("above", "below"):
            raise HTTPException(status_code=400, detail="direction must be 'above' or 'below'")
        alert_id = await ctx.db.create_price_alert(symbol, float(target_price), direction, note)
        return {"success": True, "id": alert_id}

    @app.delete("/api/alerts/{alert_id}")
    async def delete_alert(
        alert_id: int, user: str = Depends(verify_credentials)
    ) -> dict[str, Any]:
        """Delete a price alert."""
        ok = await ctx.db.delete_price_alert(alert_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Alert not found")
        return {"success": True}

