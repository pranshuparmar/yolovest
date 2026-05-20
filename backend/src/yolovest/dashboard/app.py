"""FastAPI dashboard for YoloVest.

REST API + WebSocket for portfolio overview, trade detail, reports, and auth.
All endpoints read from the shared database via AppContext.

Security:
- Session token auth: POST /api/auth/login returns a signed HMAC token
- Bearer token in Authorization header for all subsequent requests
- Basic auth still supported for backwards compatibility (CLI, curl)
- CSRF protection: state-changing endpoints require X-CSRF-Token header
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    Depends, FastAPI, Header, HTTPException, Query, Request,
    WebSocket, WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from yolovest.context import AppContext, MarketHoursChecker

logger = logging.getLogger(__name__)

security = HTTPBasic(auto_error=False)

# Token signing key — generated once per process lifetime.
# Tokens become invalid on restart (forces re-login, which is fine).
_TOKEN_SECRET = secrets.token_bytes(32)
_TOKEN_TTL_SEC = 24 * 60 * 60  # 24 hours


def _sign_token(username: str) -> str:
    """Create a signed session token: base64(payload).signature."""
    import base64

    payload = json.dumps({
        "user": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + _TOKEN_TTL_SEC,
        "jti": secrets.token_hex(8),
    }).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).decode()
    sig = hmac.new(_TOKEN_SECRET, payload, hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_token(token: str) -> str:
    """Verify a signed session token. Returns username or raises."""
    import base64

    parts = token.split(".", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=401, detail="Invalid token format")

    payload_b64, sig = parts
    try:
        payload = base64.urlsafe_b64decode(payload_b64)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token encoding")

    expected_sig = hmac.new(_TOKEN_SECRET, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    if data.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="Token expired")

    return data.get("user", "anonymous")


def _extract_broker_capital(margins: dict[str, Any]) -> float:
    """Extract free cash + utilised margin from Kite margins response.

    This represents the trading account's cash side (excluding holdings value).
    For total net worth use _compute_total_capital() which adds holdings value.

    Kite margins() returns different structures depending on the SDK version:
    - {"equity": {"net": X, "available": {"cash": Y, ...}, "utilised": {...}}}
    - Or a flat segment dict if called with segment="equity"
    Handles all known variants.
    """
    # Try Kite's nested equity structure
    equity = margins.get("equity", {})
    if isinstance(equity, dict) and equity:
        # Prefer "net" (total funds = available + used)
        net = equity.get("net")
        if net is not None:
            return float(net)
        # Fallback: available.cash + utilised.debits
        avail = equity.get("available", {})
        if isinstance(avail, dict):
            cash = avail.get("cash") or avail.get("live_balance") or 0
            used = equity.get("utilised", {}).get("debits", 0)
            return float(cash) + float(used)

    # Flat structure (segment-level response)
    net = margins.get("net")
    if net is not None:
        return float(net)

    avail = margins.get("available", {})
    if isinstance(avail, dict):
        cash = avail.get("cash") or avail.get("live_balance") or 0
        return float(cash)

    # Direct keys
    for key in ("available_cash", "total_balance"):
        val = margins.get(key)
        if val is not None:
            return float(val)

    logger.warning("Could not extract capital from margins: %s", list(margins.keys()))
    return 0.0


def _holdings_value(holdings: list[dict[str, Any]]) -> float:
    """Sum the current market value of all delivery holdings.

    Each holding from kite.holdings() has fields like:
    - quantity / opening_quantity
    - last_price (current LTP) or close_price (yesterday's close)
    - average_price (cost basis)
    """
    total = 0.0
    for h in holdings or []:
        qty = h.get("quantity") or h.get("opening_quantity") or 0
        if qty <= 0:
            continue
        # Prefer LTP, fall back to close, then to average price
        price = (
            h.get("last_price")
            or h.get("close_price")
            or h.get("average_price")
            or 0
        )
        try:
            total += float(qty) * float(price)
        except (TypeError, ValueError):
            continue
    return total


def _extract_available_cash(margins: dict[str, Any]) -> float:
    """Extract free trading cash (not deployed) from Kite margins.

    Kite's equity.available.cash is the OPENING balance — it doesn't
    reflect intraday utilisation. equity.available.live_balance (and
    equity.net) is the truly-available figure after deducting margin
    used by open MIS/CO positions. Prefer those; fall back to
    `cash − utilised.debits` so the result is honest even on older
    Kite payload shapes.
    """
    equity = margins.get("equity", {})
    if isinstance(equity, dict):
        # Top-level `net` is Kite's authoritative "available right now".
        net = equity.get("net")
        if net is not None:
            try:
                return float(net)
            except (TypeError, ValueError):
                pass

        avail = equity.get("available", {})
        used = equity.get("utilised", {})
        if isinstance(avail, dict):
            # Prefer live_balance / adhoc_margin (post-deduction values).
            for k in ("live_balance", "adhoc_margin"):
                v = avail.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            # Fall back: opening cash minus utilised debits.
            cash = avail.get("cash")
            if cash is not None:
                try:
                    used_debits = 0.0
                    if isinstance(used, dict):
                        used_debits = float(used.get("debits") or used.get("net") or 0.0)
                    return float(cash) - used_debits
                except (TypeError, ValueError):
                    pass
            # Last resort: opening balance.
            v = avail.get("opening_balance")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass

    # Legacy/non-Kite shape
    avail = margins.get("available", {})
    if isinstance(avail, dict):
        v = avail.get("cash") or avail.get("live_balance")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _extract_utilised_margin(margins: dict[str, Any]) -> float:
    """Extract margin currently locked in open intraday positions."""
    equity = margins.get("equity", {})
    if isinstance(equity, dict):
        used = equity.get("utilised", {})
        if isinstance(used, dict):
            v = used.get("debits") or used.get("net")
            if v is not None:
                return float(v)
    return 0.0


def _compute_holdings_breakdown(holdings: list[dict[str, Any]]) -> dict[str, float]:
    """Sum invested cost basis and current market value across delivery holdings."""
    invested = 0.0
    current = 0.0
    for h in holdings or []:
        qty = h.get("quantity") or h.get("opening_quantity") or 0
        if qty <= 0:
            continue
        avg = h.get("average_price") or 0
        ltp = h.get("last_price") or h.get("close_price") or avg or 0
        try:
            invested += float(qty) * float(avg)
            current += float(qty) * float(ltp)
        except (TypeError, ValueError):
            continue
    return {"invested": invested, "current": current}


async def _compute_capital_breakdown(broker: Any) -> dict[str, float]:
    """Return a structured breakdown of broker capital.

    Keys:
        available_cash: free funds ready to deploy
        utilised_margin: margin locked in open intraday positions
        holdings_invested: total buy price of CNC delivery holdings
        holdings_current: current market value of CNC delivery holdings
        total: available_cash + utilised_margin + holdings_current
    """
    breakdown = {
        "available_cash": 0.0,
        "utilised_margin": 0.0,
        "holdings_invested": 0.0,
        "holdings_current": 0.0,
        "total": 0.0,
    }
    try:
        margins = await broker.get_margins()
        if margins:
            breakdown["available_cash"] = _extract_available_cash(margins)
            breakdown["utilised_margin"] = _extract_utilised_margin(margins)
    except Exception:
        logger.debug("Margins fetch failed", exc_info=True)
    try:
        holdings = await broker.get_holdings()
        h = _compute_holdings_breakdown(holdings)
        breakdown["holdings_invested"] = h["invested"]
        breakdown["holdings_current"] = h["current"]
    except Exception:
        logger.debug("Holdings fetch failed", exc_info=True)
    breakdown["total"] = (
        breakdown["available_cash"]
        + breakdown["utilised_margin"]
        + breakdown["holdings_current"]
    )
    return breakdown


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
            # any resting target leg so it doesn't try to sell on a now-
            # empty position. Ghost recovery (next heartbeat) closes the
            # DB row with the actual fill price.
            target_oid = trade.get("target_order_id")
            if target_oid:
                try:
                    await ctx.broker.cancel_order(target_oid)
                    await ctx.db.set_trade_target_order_id(trade_id, None)
                except Exception:
                    logger.debug("%s: target cancel after SL fill failed", log_prefix, exc_info=True)
            logger.info("%s: SL fired — broker exit registered, ghost recovery will close DB row", log_prefix)
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
            # other leg, let ghost recovery close the row.
            sl_oid = trade.get("sl_order_id")
            if sl_oid:
                try:
                    await ctx.broker.cancel_order(sl_oid)
                    await ctx.db.set_trade_sl_order_id(trade_id, None)
                except Exception:
                    logger.debug("%s: SL cancel after target fill failed", log_prefix, exc_info=True)
            logger.info("%s: target LIMIT filled — broker exit registered", log_prefix)


async def _compute_total_capital(broker: Any) -> float:
    """Backward-compat wrapper. Returns the total of the breakdown."""
    bd = await _compute_capital_breakdown(broker)
    return bd["total"]


# WebSocket connection manager
_ws_clients: set[WebSocket] = set()


def _compute_volatility_score(atr_pct: float, vol_cfg: Any) -> float:
    """Compute a [0, 1] volatility score using a bell-curve preference."""
    if atr_pct <= 0 or atr_pct < vol_cfg.min_atr_pct:
        return 0.0
    if atr_pct > vol_cfg.max_atr_pct:
        return 0.3
    if vol_cfg.ideal_min_atr_pct <= atr_pct <= vol_cfg.ideal_max_atr_pct:
        return 1.0
    if atr_pct < vol_cfg.ideal_min_atr_pct:
        rng = vol_cfg.ideal_min_atr_pct - vol_cfg.min_atr_pct
        return 0.5 + 0.5 * ((atr_pct - vol_cfg.min_atr_pct) / rng) if rng > 0 else 0.5
    rng = vol_cfg.max_atr_pct - vol_cfg.ideal_max_atr_pct
    return 0.3 + 0.7 * ((vol_cfg.max_atr_pct - atr_pct) / rng) if rng > 0 else 0.5


def _compute_scan_scores(stock: dict[str, Any], min_vol: int, vol_cfg: Any = None) -> dict[str, Any]:
    """Compute sub-scores for dry-run market scanning (mirrors MarketScanSkill logic)."""
    # Technical score from indicators
    signals: list[float] = []
    rsi = stock.get("rsi")
    if rsi is not None:
        if rsi < 30:
            signals.append(0.8)
        elif rsi < 45:
            signals.append(0.65)
        elif rsi <= 55:
            signals.append(0.5)
        elif rsi <= 70:
            signals.append(0.35)
        else:
            signals.append(0.2)
    macd_hist = stock.get("macd_histogram")
    if macd_hist is not None:
        signals.append(0.7 if macd_hist > 0 else 0.3)
    supertrend_dir = stock.get("supertrend_direction")
    if supertrend_dir is not None:
        signals.append(0.7 if supertrend_dir > 0 else 0.3)
    momentum = stock.get("momentum_score")
    if momentum is not None:
        signals.append(min(momentum / 100.0, 1.0))
    tech = round(sum(signals) / len(signals), 4) if signals else 0.5

    # Volume score
    avg_vol = stock.get("avg_daily_volume") or 0
    vol_score = min(avg_vol / (min_vol * 5), 1.0) if min_vol > 0 else 0.5

    # Sentiment score
    sentiment = stock.get("sentiment")
    sent_conf = stock.get("sentiment_confidence") or 0.5
    if sentiment == "bullish":
        sent_score = 0.5 + sent_conf * 0.5
    elif sentiment == "bearish":
        sent_score = 0.5 - sent_conf * 0.5
    else:
        sent_score = 0.5

    # Fundamental score
    pe = stock.get("pe_ratio")
    promoter = stock.get("promoter_holding_pct") or 50.0
    if pe and pe > 0:
        fund_score = min(10.0 / pe, 1.0) * 0.6 + (promoter / 100.0) * 0.4
    else:
        fund_score = (promoter / 100.0) * 0.4 + 0.3

    # Volatility score
    atr_pct = stock.get("atr_pct") or 0.0
    volatility_score = _compute_volatility_score(atr_pct, vol_cfg) if vol_cfg else 0.5

    return {
        "technical_score": tech,
        "volume_momentum_score": round(vol_score, 4),
        "news_sentiment_score": round(sent_score, 4),
        "fundamental_score": round(min(fund_score, 1.0), 4),
        "volatility_score": round(volatility_score, 4),
    }


def create_app(ctx: AppContext) -> FastAPI:
    """Create and configure the FastAPI dashboard application."""
    app = FastAPI(
        title="YoloVest Dashboard",
        description="Autonomous AI-driven Indian stock trading platform",
        version="0.1.0",
    )

    # CORS for development (Vite dev server)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # CSRF middleware — require X-CSRF-Token on state-changing methods.
    # Exempt paths: login (no token yet), Zerodha postback (external caller),
    # health check (no auth needed).
    _CSRF_EXEMPT_PATHS = {
        "/api/auth/login",
        "/api/auth/zerodha/postback",
        "/api/health",
        "/ws",
    }

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE"):
            if request.url.path not in _CSRF_EXEMPT_PATHS:
                csrf_header = request.headers.get("X-CSRF-Token", "")
                # Only enforce CSRF when using Bearer auth (session-based).
                # Basic auth requests (curl, CLI) are exempt since they
                # already prove identity per-request.
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer ") and not csrf_header:
                    from starlette.responses import JSONResponse
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Missing X-CSRF-Token header"},
                    )
                if csrf_header and not secrets.compare_digest(csrf_header, _csrf_token):
                    from starlette.responses import JSONResponse
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Invalid CSRF token"},
                    )
        return await call_next(request)

    # Store context for dependency injection
    app.state.ctx = ctx

    # Auth config
    dash_password = (
        ctx.config.dashboard.password.get_secret_value()
        if hasattr(ctx.config.dashboard, "password")
        else "yolovest"
    )

    # Mutable password container (allows runtime change)
    # Check DB for a persisted password override (set via /api/change-password)
    _password = {"current": dash_password}

    @app.on_event("startup")
    async def _load_persisted_password() -> None:
        try:
            saved_pw = await ctx.db.get_system_state("dashboard_password")
            if saved_pw:
                _password["current"] = saved_pw
        except Exception:
            logger.warning("Failed to load persisted dashboard password", exc_info=True)

    def verify_credentials(
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> str:
        """Authenticate via Bearer token (preferred) or Basic auth (fallback).

        Bearer token: Authorization: Bearer <token from /api/auth/login>
        Basic auth: Authorization: Basic <base64(user:password)>
        """
        auth_header = request.headers.get("Authorization", "")

        # Try Bearer token first
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            return _verify_token(token)

        # Fall back to Basic auth
        if credentials is not None:
            correct = secrets.compare_digest(credentials.password, _password["current"])
            if correct:
                return credentials.username

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Bearer, Basic realm="YoloVest"'},
        )

    # CSRF token — one per process, sent to client on login
    _csrf_token = secrets.token_hex(32)

    # Login endpoint — issues session token + CSRF token
    @app.post("/api/auth/login")
    async def login(body: dict[str, Any]) -> dict[str, Any]:
        """Authenticate with password and receive a session token."""
        pw = body.get("password", "")
        if not secrets.compare_digest(pw, _password["current"]):
            raise HTTPException(status_code=401, detail="Invalid password")
        username = body.get("username", "admin")
        token = _sign_token(username)
        return {
            "token": token,
            "csrf_token": _csrf_token,
            "expires_in": _TOKEN_TTL_SEC,
        }

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
            return {"success": False, "error": str(e)}

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
        exit_qty = int(qty) if is_partial else full_qty
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
                raise HTTPException(
                    status_code=502,
                    detail=f"Broker rejected partial-exit order: {e}",
                )

            # Wait briefly for fill
            exit_price = None
            for _ in range(10):
                try:
                    status = await ctx.broker.get_order_status(exit_order_id)
                    exit_price = status.get("average_price")
                    if exit_price and exit_price > 0:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if not exit_price or exit_price <= 0:
                try:
                    exit_price = await ctx.market_data.get_ltp(symbol)
                except Exception:
                    exit_price = float(
                        trade.get("fill_price") or trade["entry_price"] or 0
                    )

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
            raise HTTPException(status_code=502, detail=f"Broker rejected exit order: {e}")

        # Wait briefly for fill, fall back to LTP-based estimate
        exit_price = None
        for _ in range(10):
            try:
                status = await ctx.broker.get_order_status(exit_order_id)
                exit_price = status.get("average_price")
                if exit_price and exit_price > 0:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        if not exit_price or exit_price <= 0:
            try:
                exit_price = await ctx.market_data.get_ltp(symbol)
            except Exception:
                exit_price = float(trade.get("fill_price") or trade.get("entry_price") or 0)

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
        except Exception as e:
            logger.exception("get_broker_orders: get_orders failed")
            return {
                "authenticated": True, "orders": [], "gtts": [],
                "error": f"orders fetch failed: {e}",
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
            )

    @app.post("/api/review")
    async def review_symbols(
        body: dict[str, Any] | None = None,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Run ML review on any symbols and return recommendations.

        Body: {"symbols": ["SYM1", "SYM2"]} — review specific symbols.
        Omit body or symbols to review all current holdings.
        Works for any NSE symbol, whether held or not.
        """
        from yolovest.data.features import compute_features

        body = body or {}
        requested = body.get("symbols")

        # Get holdings context (for P&L display, not for filtering)
        holdings = []
        try:
            holdings = await ctx.broker.get_holdings()
        except Exception:
            pass
        holding_map = {h["tradingsymbol"]: h for h in (holdings or []) if h.get("quantity", 0) > 0}

        # Map open system-managed trades by symbol so the recommendation
        # payload can carry `trade_id` and `current_sl` — the Holdings UI
        # uses these to power the in-row "Tighten SL" action without an
        # extra round trip to look up the trade by symbol.
        open_trades_for_symbol: dict[str, dict[str, Any]] = {}
        try:
            for tr in await ctx.db.get_open_positions(mode=ctx.config.mode):
                sym = tr.get("symbol")
                if sym and sym not in open_trades_for_symbol:
                    open_trades_for_symbol[sym] = tr
        except Exception:
            logger.debug("review: failed to load open positions", exc_info=True)

        if requested:
            symbols = [s.upper() for s in requested]
        elif holding_map:
            symbols = list(holding_map.keys())
        else:
            return {"recommendations": [], "error": "Provide symbols or authenticate with Kite for holdings review"}

        from yolovest.data.features import IndicatorConfig
        ind = ctx.config.strategy.indicators
        indicator_cfg = IndicatorConfig(
            ema_periods=ctx.config.strategy.ema_periods,
            rsi=ind.rsi, macd=ind.macd, bollinger_bands=ind.bollinger_bands,
            vwap=ind.vwap, atr=ind.atr, volume_profile=ind.volume_profile,
            obv=ind.obv, supertrend=ind.supertrend,
        )
        recommendations = []

        for symbol in symbols:
            held = holding_map.get(symbol)
            open_trade = open_trades_for_symbol.get(symbol)
            rec: dict[str, Any] = {
                "symbol": symbol,
                "held": held is not None,
                "quantity": held.get("quantity", 0) if held else 0,
                "average_price": held.get("average_price", 0) if held else 0,
                "last_price": held.get("last_price", 0) if held else 0,
                "pnl_pct": 0,
                "action": "HOLD",
                "confidence": 0,
                "signal_type": "HOLD",
                "reasoning": "",
                # Open-trade context so the "Tighten SL" button can act
                # without an extra round trip. None when no system-tracked
                # trade exists for this symbol (e.g. a holding the user
                # acquired outside the system).
                "trade_id": open_trade.get("trade_id") if open_trade else None,
                "current_sl": (
                    float(open_trade.get("stop_loss_price") or 0)
                    if open_trade else 0
                ),
                "trade_signal_type": (
                    open_trade.get("signal_type") if open_trade else None
                ),
                "entry_price": (
                    float(open_trade.get("entry_price") or 0)
                    if open_trade else 0
                ),
            }

            entry = rec["average_price"]
            ltp = rec["last_price"]

            # Fetch LTP if not from holdings
            if ltp <= 0:
                try:
                    ltp = await ctx.market_data.get_ltp(symbol)
                    rec["last_price"] = ltp
                except Exception:
                    pass

            if entry > 0 and ltp > 0:
                rec["pnl_pct"] = round((ltp - entry) / entry * 100, 2)

            try:
                bars = await ctx.db.get_ohlcv(symbol, "daily", days=365)
                if not bars or len(bars) < 50:
                    rec["reasoning"] = f"Insufficient data ({len(bars) if bars else 0} bars)"
                    recommendations.append(rec)
                    continue

                features = compute_features(bars, indicator_cfg)
                if not features:
                    rec["reasoning"] = "Feature computation failed"
                    recommendations.append(rec)
                    continue

                # Run both models
                intra_pred = None
                swing_pred = None
                if ctx.ml:
                    try:
                        swing_pred = await ctx.ml.predict_swing(symbol, features, current_price=ltp or None)
                    except Exception:
                        pass
                    try:
                        intra_pred = await ctx.ml.predict_intraday(symbol, features, current_price=ltp or None)
                    except Exception:
                        pass

                # Pick best prediction
                pred = None
                if swing_pred and swing_pred.signal_type != "HOLD":
                    pred = swing_pred
                if intra_pred and intra_pred.signal_type != "HOLD":
                    if pred is None or intra_pred.confidence > pred.confidence:
                        pred = intra_pred

                if pred is None:
                    rsi = features.get("rsi_14", 50)
                    rec["action"] = "HOLD"
                    rec["confidence"] = max(
                        (swing_pred.confidence if swing_pred else 0),
                        (intra_pred.confidence if intra_pred else 0),
                    )
                    parts = []
                    if rsi < 30:
                        parts.append("oversold (RSI %.0f)" % rsi)
                    elif rsi > 70:
                        parts.append("overbought (RSI %.0f)" % rsi)
                    if held and rec["pnl_pct"] > 10:
                        parts.append("consider partial profit booking (%.1f%% up)" % rec["pnl_pct"])
                        rec["action"] = "TIGHTEN_SL"
                    elif held and rec["pnl_pct"] < -10:
                        parts.append("significant drawdown (%.1f%%)" % rec["pnl_pct"])
                    rec["reasoning"] = "; ".join(parts) if parts else "No strong directional signal"
                else:
                    rec["signal_type"] = pred.signal_type
                    rec["confidence"] = round(pred.confidence, 2)
                    if pred.signal_type == "SELL":
                        rec["action"] = "SELL" if held else "SHORT"
                        rec["target_price"] = round(pred.target_price, 2) if hasattr(pred, "target_price") else None
                        rec["stop_loss_price"] = round(pred.stop_loss_price, 2) if hasattr(pred, "stop_loss_price") else None
                        rec["reasoning"] = f"ML SELL signal at {pred.confidence:.0%} confidence"
                    elif pred.signal_type == "BUY":
                        rec["action"] = "BUY_MORE" if held else "BUY"
                        rec["target_price"] = round(pred.target_price, 2) if hasattr(pred, "target_price") else None
                        rec["stop_loss_price"] = round(pred.stop_loss_price, 2) if hasattr(pred, "stop_loss_price") else None
                        rec["reasoning"] = f"ML BUY signal at {pred.confidence:.0%} confidence"

            except Exception as e:
                rec["reasoning"] = f"Analysis failed: {e}"

            recommendations.append(rec)

        recommendations.sort(key=lambda r: (r["action"] != "HOLD", r["confidence"]), reverse=True)
        return {"recommendations": recommendations}

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
            return {"success": False, "error": str(e)}

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

    # ------------------------------------------------------------------
    # Predictions & Scoreboard
    # ------------------------------------------------------------------

    @app.get("/api/predictions/scoreboard")
    async def get_scoreboard(
        group_type: str | None = Query(None),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Prediction accuracy scoreboard."""
        return await ctx.db.get_prediction_scoreboard(group_type)

    @app.get("/api/recommendations")
    async def get_recommendations(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Today's signals with disposition (executed/pending/rejected)."""
        return await ctx.db.get_todays_recommendations()

    # ------------------------------------------------------------------
    # Historical Reports
    # ------------------------------------------------------------------

    @app.get("/api/reports")
    async def get_reports(
        report_type: str | None = Query(None, description="'daily' or 'weekly'"),
        start: str | None = Query(None),
        end: str | None = Query(None),
        limit: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Historical reports archive."""
        return await ctx.db.get_reports_history(
            report_type=report_type, start_date=start, end_date=end, limit=limit
        )

    # ------------------------------------------------------------------
    # Watchlist & Market Data
    # ------------------------------------------------------------------

    @app.get("/api/watchlist")
    async def get_watchlist(user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """Algorithmic watchlist (auto-generated by market-scan)."""
        return await ctx.db.get_watchlist()

    @app.get("/api/user-watchlist")
    async def get_user_watchlist(user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """User-managed watchlist with scores from algorithmic scan."""
        return await ctx.db.get_user_watchlist()

    @app.post("/api/user-watchlist")
    async def add_user_watchlist_symbol(
        body: dict[str, Any],
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Add a symbol to the user watchlist."""
        symbol = body.get("symbol", "").strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol is required")
        sector = body.get("sector")
        notes = body.get("notes")
        ok = await ctx.db.add_user_watchlist_symbol(symbol, sector, notes)
        return {"success": ok, "symbol": symbol}

    @app.delete("/api/user-watchlist/{symbol}")
    async def remove_user_watchlist_symbol(
        symbol: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Remove a symbol from the user watchlist."""
        ok = await ctx.db.remove_user_watchlist_symbol(symbol)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found in user watchlist")
        return {"success": True, "symbol": symbol.upper()}

    @app.get("/api/sectors")
    async def get_sector_rotation(user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Sector rotation analysis."""
        return await ctx.db.get_sector_rotation()

    @app.get("/api/universe-symbols")
    async def get_universe_symbols_list(
        _user: str = Depends(verify_credentials),
    ) -> list[str]:
        """All symbols in the OHLCV universe (for search/autocomplete)."""
        from yolovest.data.nse_symbols import get_universe_symbols as get_syms
        universe = ctx.config.scanning.universe
        return get_syms(universe)

    # ------------------------------------------------------------------
    # System
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health_check() -> dict[str, Any]:
        """System health (no auth required)."""
        db_ok = await ctx.db.health_check()
        return {
            "status": "ok" if db_ok else "degraded",
            "database": db_ok,
            "mode": ctx.config.mode,
            "timezone": ctx.config.market_hours.timezone,
        }

    @app.get("/api/slippage")
    async def get_slippage_stats(
        symbol: str | None = Query(None),
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Slippage analysis."""
        return await ctx.db.get_slippage_stats(symbol=symbol, days=days, mode=ctx.config.mode)

    @app.get("/api/llm-accuracy")
    async def get_llm_accuracy(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """LLM review accuracy vs actual trade outcomes."""
        return await ctx.db.get_llm_review_accuracy(days=days, mode=ctx.config.mode)

    @app.get("/api/model-drift")
    async def get_model_drift(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Model drift dashboard: predicted vs realised win rate per model.

        Detects when the ML model's calibration is decaying so retraining
        can be triggered before live performance silently degrades.
        """
        return await ctx.db.get_model_drift_stats(days=days, mode=ctx.config.mode)

    @app.get("/api/signal-class-distribution")
    async def get_signal_class_distribution(
        days: int = Query(7, ge=1, le=90),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """BUY/HOLD/SELL signal counts over the last N days (mode-scoped).

        Surfaces the same data the drift-watch class-collapse alert
        runs against, so the dashboard can render a visible
        early-warning widget even when no alert has fired yet.
        """
        return await ctx.db.get_signal_class_counts(
            days=days, mode=ctx.config.mode,
        )

    @app.get("/api/institutional-flows")
    async def get_institutional_flows(
        days: int = Query(30, ge=1, le=180),
        bulk_limit: int = Query(200, ge=1, le=2000),
        symbol: str | None = Query(None),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Combined FII/DII timeline + recent bulk/block deals.

        FII/DII values are in ₹ crore. Bulk-deal rows are NSE
        verbatim — same data the institutional-flow risk-check
        multiplier reads at signal-evaluation time.
        """
        timeline = await ctx.db.get_fii_dii_timeline(days)
        summary = await ctx.db.get_fii_dii_timeline_summary(days)
        deals = await ctx.db.get_bulk_deals_list(
            days=days, symbol=symbol, limit=bulk_limit,
        )
        return {
            "fii_dii_timeline": timeline,
            "fii_dii_summary": summary,
            "bulk_deals": deals,
        }

    @app.get("/api/audit")
    async def get_audit_log(
        limit: int = Query(50, ge=1, le=500),
        action_type: str | None = Query(None),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Recent audit log entries."""
        return await ctx.db.get_audit_log(limit=limit, action_type=action_type)

    @app.get("/api/logs")
    async def get_server_logs(
        lines: int = Query(200, ge=1, le=500),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Recent server log lines from the in-memory buffer."""
        from yolovest.log_buffer import get_log_buffer
        buf = get_log_buffer()
        if buf is None:
            return {"lines": [], "total": 0}
        log_lines = buf.get_lines(last_n=lines)
        return {"lines": log_lines, "total": len(log_lines)}

    # ------------------------------------------------------------------
    # Integrations
    # ------------------------------------------------------------------

    @app.get("/api/integrations")
    async def get_integrations_status(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Status of all external integrations."""
        results: dict[str, Any] = {}

        # --- Gemini LLM ---
        # Don't ping on page load (wastes quota and blocks for 20+s on 429).
        # Just report config status; user can click "Test Connection" to verify.
        llm_enabled = getattr(ctx.config.llm, "enabled", False)
        _llm_key_raw = ctx.config.llm.api_key.get_secret_value() if hasattr(ctx.config.llm.api_key, "get_secret_value") else str(ctx.config.llm.api_key)
        gemini_configured = bool(_llm_key_raw) and not _llm_key_raw.startswith("${")
        results["gemini"] = {
            "enabled": llm_enabled,
            "configured": gemini_configured,
            "connected": llm_enabled and gemini_configured,
            "model": getattr(ctx.config.llm, "model", ""),
        }

        # --- Zerodha Broker ---
        _broker_key_raw = ctx.config.broker.api_key.get_secret_value() if hasattr(ctx.config.broker.api_key, "get_secret_value") else str(ctx.config.broker.api_key)
        broker_configured = bool(_broker_key_raw) and not _broker_key_raw.startswith("${")
        # Verify token is actually valid (catches expired tokens)
        broker_authenticated = False
        if broker_configured:
            try:
                broker_authenticated = await ctx.broker.is_authenticated()
            except Exception:
                logger.debug("Broker auth check failed on integrations page", exc_info=True)
        broker_margins: dict[str, Any] | None = None
        results["zerodha"] = {
            "configured": broker_configured,
            "connected": broker_authenticated,
            "mode": ctx.config.mode,
            "login_url": ctx.broker.get_login_url() if broker_configured else None,
            "margins": broker_margins,
        }

        # --- Telegram Bot ---
        telegram_cfg = ctx.config.notifications.telegram if hasattr(ctx.config, "notifications") else None
        telegram_enabled = bool(telegram_cfg and getattr(telegram_cfg, "enabled", False))
        _bot_token_raw = getattr(telegram_cfg, "bot_token", None) if telegram_cfg else None
        bot_token = _bot_token_raw.get_secret_value() if hasattr(_bot_token_raw, "get_secret_value") else str(_bot_token_raw or "")
        chat_id = getattr(telegram_cfg, "chat_id", "") if telegram_cfg else ""
        telegram_configured = bool(telegram_cfg and bot_token and chat_id)

        # Build diagnostic hint
        telegram_hint = ""
        if not telegram_cfg:
            telegram_hint = "No telegram section found in config"
        elif not bot_token:
            telegram_hint = "bot_token is empty — ensure config.yaml has bot_token: ${TELEGRAM_BOT_TOKEN} and the env var is exported before startup"
        elif "${" in bot_token:
            telegram_hint = "bot_token placeholder was not expanded — env var TELEGRAM_BOT_TOKEN was not set when the app started"
        elif not chat_id:
            telegram_hint = "chat_id is empty — ensure config.yaml has chat_id: ${TELEGRAM_CHAT_ID} and the env var is exported before startup"
        elif "${" in chat_id:
            telegram_hint = "chat_id placeholder was not expanded — env var TELEGRAM_CHAT_ID was not set when the app started"
        elif not telegram_enabled:
            telegram_hint = "Tokens are set but telegram is disabled — set notifications.telegram.enabled: true in config.yaml"

        results["telegram"] = {
            "configured": telegram_configured,
            "enabled": telegram_enabled,
            "chat_id": chat_id if "${" not in chat_id else "",
            "hint": telegram_hint,
        }

        return results

    @app.post("/api/integrations/gemini/ping")
    async def ping_gemini(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Test Gemini LLM connectivity."""
        try:
            ok = await ctx.llm.ping()
            return {"success": ok}
        except Exception as exc:
            logger.warning("Gemini ping failed: %s", exc)
            return {"success": False, "error": str(exc)}

    @app.post("/api/integrations/zerodha/logout")
    async def logout_zerodha(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Drop the cached Kite access token (and the persisted one) so
        the broker reports as unauthenticated until the next manual
        re-auth. Also stops the live tick stream if it was running on
        the now-stale token — the ticker holds the token from process
        boot and won't pick up a fresh one without a restart, so it's
        cleaner to let the user re-auth and explicitly restart than to
        keep a half-alive WS open.
        """
        await ctx.broker.logout()
        ticker = getattr(ctx, "ticker", None)
        if ticker is not None:
            try:
                await ticker.stop()
            except Exception:
                logger.debug("Ticker stop on logout failed", exc_info=True)
            ctx.ticker = None  # type: ignore[assignment]
        return {"success": True}

    @app.post("/api/integrations/zerodha/authenticate")
    async def authenticate_zerodha(
        body: dict[str, Any],
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Authenticate Zerodha with a request token."""
        request_token = body.get("request_token", "").strip()
        if not request_token:
            raise HTTPException(status_code=400, detail="request_token is required")
        try:
            ok = await ctx.broker.authenticate(request_token)
            margins = None
            if ok:
                # Sync token to Kite data provider if enabled
                from yolovest.main import _sync_kite_data_token
                _sync_kite_data_token(ctx)
                try:
                    margins = await ctx.broker.get_margins()
                except Exception:
                    logger.debug("Failed to fetch margins after Zerodha auth", exc_info=True)
            return {"success": ok, "margins": margins}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @app.get("/api/auth/zerodha/callback", response_model=None)
    async def zerodha_oauth_callback(
        request_token: str = Query(default=""),
        status_param: str = Query(default="", alias="status"),
    ) -> RedirectResponse | HTMLResponse:
        """OAuth callback — Zerodha redirects here after user logs in.

        No auth required (this is the redirect target from Kite login).
        Extracts request_token from query params, exchanges for access_token,
        then redirects user to the dashboard integrations page.
        """
        if not request_token or status_param != "success":
            return HTMLResponse(
                "<h3>Zerodha login failed or was cancelled.</h3>"
                '<p><a href="/integrations">Back to Dashboard</a></p>',
                status_code=400,
            )

        try:
            ok = await ctx.broker.authenticate(request_token)
            if ok:
                logger.info("Zerodha authenticated via OAuth callback")
                from yolovest.main import _sync_kite_data_token
                _sync_kite_data_token(ctx)
                try:
                    await ctx.notify.send("Kite authenticated successfully.")
                except Exception:
                    logger.debug("Failed to send Kite auth success notification", exc_info=True)
                return RedirectResponse(url="/integrations?zerodha_auth=success")
            else:
                return RedirectResponse(url="/integrations?zerodha_auth=failed")
        except Exception as e:
            logger.warning("Zerodha OAuth callback failed: %s", e)
            return RedirectResponse(url="/integrations?zerodha_auth=failed")

    @app.post("/api/auth/zerodha/postback")
    async def zerodha_postback(request: Request) -> dict[str, str]:
        """Zerodha order postback. Fires on every order status change
        (COMPLETE / CANCELLED / REJECTED / partial-fill UPDATE).

        Two things happen:
          1. Checksum verification — SHA-256(order_id + order_timestamp +
             api_secret) must match the body's checksum field. Without
             this, anyone who knows the endpoint URL could spoof updates
             at our dashboard clients.
          2. Business logic — for terminal states (COMPLETE, CANCELLED,
             REJECTED) we route the update to _apply_order_postback,
             which updates the matching trade row immediately rather
             than waiting for the next 15-min heartbeat reconciliation.
        """
        import hashlib
        import json as _json

        raw = await request.body()
        try:
            body = _json.loads(raw or b"{}")
        except (ValueError, TypeError):
            logger.warning("Zerodha postback: invalid JSON body")
            raise HTTPException(status_code=400, detail="invalid body")

        order_id = str(body.get("order_id") or "")
        order_timestamp = str(body.get("order_timestamp") or "")
        received_checksum = body.get("checksum") or ""

        api_secret_val = ctx.config.broker.api_secret.get_secret_value() \
            if ctx.config.broker.api_secret else ""
        if api_secret_val and order_id and order_timestamp:
            expected = hashlib.sha256(
                f"{order_id}{order_timestamp}{api_secret_val}".encode(),
            ).hexdigest()
            if not secrets.compare_digest(expected, str(received_checksum)):
                logger.warning(
                    "Zerodha postback: checksum mismatch for order=%s "
                    "(possibly spoofed) — rejecting", order_id,
                )
                raise HTTPException(status_code=401, detail="invalid checksum")
        else:
            # Mode where checksum can't be computed (paper / dev). Log
            # but accept so local testing isn't blocked.
            logger.debug(
                "Zerodha postback: skipping checksum (api_secret/order_id/timestamp missing)",
            )

        status_str = (body.get("status") or "").upper()
        logger.info("Zerodha postback: order=%s status=%s", order_id, status_str)

        if status_str in ("COMPLETE", "CANCELLED", "REJECTED"):
            try:
                await _apply_order_postback(ctx, order_id, status_str, body)
            except Exception:
                logger.exception(
                    "Zerodha postback: business-logic failed for order=%s",
                    order_id,
                )

        try:
            await broadcast_ws("order_update", {
                "order_id": order_id,
                "status": status_str,
                "symbol": body.get("tradingsymbol"),
                "transaction_type": body.get("transaction_type"),
            })
        except Exception:
            logger.debug("Failed to broadcast order update via WebSocket", exc_info=True)

        return {"status": "ok"}

    @app.post("/api/integrations/telegram/test")
    async def test_telegram(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Send a test message via Telegram."""
        try:
            ok = await ctx.notify.send("YoloVest: Test message from dashboard")
            return {"success": ok}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @app.post("/api/integrations/telegram/send")
    async def send_telegram_message(
        body: dict[str, Any],
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Send a custom message via Telegram."""
        message = body.get("message", "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        try:
            ok = await ctx.notify.send(message)
            return {"success": ok}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Economic Calendar & Earnings
    # ------------------------------------------------------------------

    @app.get("/api/economic-calendar")
    async def get_economic_calendar(
        days: int = Query(30, ge=1, le=90),
        country: str | None = Query(None),
        event_type: str | None = Query(None),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Upcoming economic events (RBI MPC, FOMC, NSE earnings)."""
        return await ctx.db.get_upcoming_economic_events(
            days=days, country=country, event_type=event_type
        )

    @app.get("/api/earnings")
    async def get_earnings(
        symbol: str | None = Query(None),
        days: int = Query(30, ge=1, le=90),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Upcoming earnings events, optionally filtered by symbol."""
        return await ctx.db.get_earnings_events(symbol=symbol, days=days)

    # ------------------------------------------------------------------
    # Holidays & Early Close Days
    # ------------------------------------------------------------------

    @app.get("/api/holidays")
    async def get_holidays(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Return holidays and early close days from live config."""
        return {
            "holidays": ctx.config.market_hours.holidays,
            "early_close_days": ctx.config.market_hours.early_close_days,
        }

    @app.post("/api/holidays")
    async def add_holiday(
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Add a holiday or early close day.

        Body: {"date": "YYYY-MM-DD"} for full holiday
              {"date": "YYYY-MM-DD", "early_close": "13:00"} for early close
        """
        import re as _re

        body = await request.json()
        date_str: str = body.get("date", "").strip()
        early_close: str | None = body.get("early_close")

        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")

        if early_close:
            if not _re.match(r"^\d{2}:\d{2}$", early_close):
                raise HTTPException(400, "Invalid time format, expected HH:MM")
            ec = dict(ctx.config.market_hours.early_close_days)
            ec[date_str] = early_close
            ctx.config.market_hours.early_close_days = ec
            # Persist to DB
            import json as _json
            await ctx.db.set_config("market_hours.early_close_days", _json.dumps(ec))
        else:
            holidays = list(ctx.config.market_hours.holidays)
            if date_str not in holidays:
                holidays.append(date_str)
                holidays.sort()
            ctx.config.market_hours.holidays = holidays
            import json as _json
            await ctx.db.set_config("market_hours.holidays", _json.dumps(holidays))

        # Refresh market hours checker
        ctx.market_hours = MarketHoursChecker(ctx.config)
        logger.info("Holiday added: %s (early_close=%s)", date_str, early_close)
        return {"success": True, "date": date_str, "early_close": early_close}

    @app.delete("/api/holidays/{date_str}")
    async def remove_holiday(
        date_str: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Remove a holiday or early close day."""
        import json as _json

        removed = False
        # Remove from holidays list
        holidays = list(ctx.config.market_hours.holidays)
        if date_str in holidays:
            holidays.remove(date_str)
            ctx.config.market_hours.holidays = holidays
            await ctx.db.set_config("market_hours.holidays", _json.dumps(holidays))
            removed = True

        # Remove from early close days
        ec = dict(ctx.config.market_hours.early_close_days)
        if date_str in ec:
            del ec[date_str]
            ctx.config.market_hours.early_close_days = ec
            await ctx.db.set_config("market_hours.early_close_days", _json.dumps(ec))
            removed = True

        if not removed:
            raise HTTPException(404, f"Date {date_str} not found in holidays or early close days")

        ctx.market_hours = MarketHoursChecker(ctx.config)
        logger.info("Holiday removed: %s", date_str)
        return {"success": True, "date": date_str}

    # ------------------------------------------------------------------
    # News Feed & Sentiment
    # ------------------------------------------------------------------

    @app.get("/api/news")
    async def get_news_feed(
        symbol: str | None = Query(None),
        source: str | None = Query(None),
        date_from: str | None = Query(None, description="YYYY-MM-DD"),
        date_to: str | None = Query(None, description="YYYY-MM-DD (exclusive upper bound)"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Recent news articles with source attribution."""
        articles = await ctx.db.get_news_articles(
            symbol=symbol, source=source, date_from=date_from, date_to=date_to,
            limit=limit, offset=offset,
        )
        if symbol:
            # Defensive post-filter: legacy rows scraped by the old
            # substring matcher mis-tagged short symbols (e.g. ITC
            # inside BITCOIN, BPL inside REPUBLIC). Drop rows whose
            # headline doesn't contain the symbol as a standalone
            # word. New rows will already pass; old rows get hidden
            # without a destructive backfill.
            import re as _re
            pattern = _re.compile(
                rf"(?<![A-Z0-9]){_re.escape(symbol.upper())}(?![A-Z0-9])"
            )
            articles = [
                a for a in articles
                if pattern.search((a.get("headline") or "").upper())
            ]
        return articles

    @app.get("/api/sentiment/{symbol}")
    async def get_symbol_sentiment(
        symbol: str, user: str = Depends(verify_credentials)
    ) -> dict[str, Any]:
        """Latest sentiment analysis for a symbol."""
        result = await ctx.db.get_sentiment(symbol)
        if not result:
            return {"symbol": symbol, "sentiment": "neutral", "confidence": 0, "key_drivers": []}
        # SentimentResult is a Pydantic model or dict
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if hasattr(result, "dict"):
            return result.dict()
        return result if isinstance(result, dict) else {"symbol": symbol, "sentiment": "neutral", "confidence": 0, "key_drivers": []}

    # ------------------------------------------------------------------
    # ML Models & Performance
    # ------------------------------------------------------------------

    @app.get("/api/ml-models")
    async def get_ml_models(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """ML model information: production, shadow, and retired models."""
        result: dict[str, Any] = {"production": {}, "shadow": [], "retired": []}
        for model_type in ["intraday", "swing"]:
            try:
                model = await ctx.db.get_production_model(model_type)
                if model:
                    result["production"][model_type] = model
            except Exception:
                logger.debug("Failed to get production model for %s", model_type, exc_info=True)
        try:
            shadow_models = await ctx.db.get_all_shadow_models()
            result["shadow"] = shadow_models
        except Exception:
            logger.debug("Failed to get shadow models", exc_info=True)
        try:
            retired_models = await ctx.db.get_retired_models()
            result["retired"] = retired_models
        except Exception:
            logger.debug("Failed to get retired models", exc_info=True)
        return result

    @app.post("/api/ml-models/{model_type}/{version}/promote")
    async def promote_model(
        model_type: str,
        version: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Manually promote a shadow model to production."""
        await ctx.db.promote_model(model_type, version)
        if ctx.ml:
            try:
                await ctx.ml.load_model(model_type, version)
            except Exception as e:
                logger.warning("Failed to load promoted model %s/%s: %s", model_type, version, e)
        return {"promoted": True, "model_type": model_type, "version": version}

    @app.post("/api/ml-models/{model_type}/{version}/reshadow")
    async def reshadow_model(
        model_type: str,
        version: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Move a retired model back to shadow for re-evaluation."""
        # Check if .pkl file exists before changing status
        model_dir = _model_dir()
        pkl_path = Path(model_dir) / f"{version}.pkl"
        if not pkl_path.exists():
            return {
                "reshadowed": False,
                "error": f"Model file {version}.pkl not found — it was already deleted. Cannot re-shadow.",
            }

        ok = await ctx.db.reshadow_model(model_type, version)
        if ok and ctx.ml:
            try:
                await ctx.ml.load_shadow_model(model_type, version)
            except Exception as e:
                # Revert to retired if load fails
                await ctx.db.retire_model(model_type, version)
                logger.warning("Failed to load re-shadowed model %s/%s: %s", model_type, version, e)
                return {
                    "reshadowed": False,
                    "error": f"Model file exists but failed to load: {e}",
                }
        return {"reshadowed": ok, "model_type": model_type, "version": version}

    @app.post("/api/ml-models/{model_type}/{version}/retire")
    async def retire_model_endpoint(
        model_type: str,
        version: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Retire a shadow model (stop A/B testing, move to retired)."""
        await ctx.db.retire_model(model_type, version)
        if ctx.ml:
            ctx.ml.clear_shadow(model_type)
        logger.info("Retired %s model %s", model_type, version)
        return {"retired": True, "model_type": model_type, "version": version}

    @app.get("/api/ml-models/{model_type}/shadow-comparison")
    async def get_shadow_comparison(
        model_type: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Head-to-head shadow vs production prediction metrics."""
        shadow_models = await ctx.db.get_all_shadow_models()
        shadow = next((s for s in shadow_models if s["model_type"] == model_type), None)
        if not shadow:
            return {"shadow": {}, "production": {}}
        return await ctx.db.get_shadow_vs_production_metrics(
            model_type, since_date=shadow.get("shadow_start_date", "2000-01-01"),
        )

    # ------------------------------------------------------------------
    # Predictions Detail & Failures
    # ------------------------------------------------------------------

    @app.get("/api/predictions/today")
    async def get_todays_predictions(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = Query(default=None),
        direction: str | None = Query(default=None, pattern=r"^(BUY|SELL)$"),
        model: str | None = Query(default=None),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Today's predictions with linked symbols and confidence."""
        return await ctx.db.get_todays_predictions(
            limit=limit, offset=offset, symbol=symbol,
            direction=direction, model=model, mode=ctx.config.mode,
        )

    @app.get("/api/predictions/unscored")
    async def get_unscored_predictions(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = Query(default=None),
        direction: str | None = Query(default=None, pattern=r"^(BUY|SELL)$"),
        model: str | None = Query(default=None),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """All predictions awaiting scoring."""
        return await ctx.db.get_all_awaiting_predictions(
            limit=limit, offset=offset, symbol=symbol,
            direction=direction, model=model,
        )

    @app.get("/api/predictions/outcomes")
    async def get_prediction_outcomes(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = Query(default=None),
        direction: str | None = Query(default=None, pattern=r"^(BUY|SELL)$"),
        direction_correct: int | None = Query(default=None, ge=0, le=1),
        target_hit: int | None = Query(default=None, ge=0, le=1),
        model: str | None = Query(default=None),
        min_confidence: float | None = Query(default=None, ge=0, le=1),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Scored prediction outcomes with filters."""
        return await ctx.db.get_prediction_outcomes_paginated(
            limit=limit, offset=offset, symbol=symbol,
            direction=direction, direction_correct=direction_correct,
            target_hit=target_hit, model=model,
            min_confidence=min_confidence,
        )

    # ------------------------------------------------------------------
    # Weekly Summary
    # ------------------------------------------------------------------

    @app.get("/api/weekly/trades")
    async def get_weekly_trades(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's trades."""
        return await ctx.db.get_weekly_trades(mode=ctx.config.mode)

    @app.get("/api/weekly/predictions")
    async def get_weekly_predictions(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's predictions."""
        return await ctx.db.get_weekly_predictions(mode=ctx.config.mode)

    @app.get("/api/weekly/llm-reviews")
    async def get_weekly_llm_reviews(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's LLM reviews with linked trade outcomes."""
        return await ctx.db.get_weekly_llm_reviews()

    # ------------------------------------------------------------------
    # Risk Exposure
    # ------------------------------------------------------------------

    @app.get("/api/risk-exposure")
    async def get_risk_exposure(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Portfolio risk breakdown by stock and sector."""
        portfolio = await ctx.db.get_portfolio_state(mode=ctx.config.mode)
        positions = await ctx.db.get_open_positions(mode=ctx.config.mode)
        stock_exposures = portfolio.get("stock_exposures", {})
        sector_counts = portfolio.get("sector_counts", {})

        # Build sector exposure from positions
        sector_exposure: dict[str, float] = {}
        for pos in positions:
            sector = await ctx.db.get_stock_sector(pos.get("symbol", ""))
            sector_name = sector or "Unknown"
            value = pos.get("fill_price", 0) * pos.get("quantity", 0)
            sector_exposure[sector_name] = sector_exposure.get(sector_name, 0) + value

        total_capital = portfolio.get("total_capital", 1)
        return {
            "total_capital": total_capital,
            "exposure_pct": portfolio.get("exposure_pct", 0),
            "stock_exposures": stock_exposures,
            "sector_counts": sector_counts,
            "sector_exposure_value": sector_exposure,
            "sector_exposure_pct": {
                k: round(v / total_capital * 100, 2) if total_capital > 0 else 0
                for k, v in sector_exposure.items()
            },
            "positions_count": len(positions),
        }

    # ------------------------------------------------------------------
    # NSE Universe
    # ------------------------------------------------------------------

    @app.get("/api/nse-universe")
    async def get_nse_universe(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """All symbols in the NSE tracking universe."""
        return await ctx.db.get_nse_universe()

    # ------------------------------------------------------------------
    # Pre-Market Data
    # ------------------------------------------------------------------

    @app.get("/api/premarket")
    async def get_premarket(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Latest pre-market data (GIFT Nifty, market bias)."""
        data = await ctx.db.get_latest_premarket()
        return data or {"date": None, "gift_nifty_change_pct": None, "market_bias": None}

    # ------------------------------------------------------------------
    # System State & Kill Switch
    # ------------------------------------------------------------------

    @app.get("/api/system-state")
    async def get_system_state(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """System state including kill switch, degraded features, and auto-approvals."""
        kill_switch = await ctx.db.is_kill_switch_active()
        # Which command activated the pause? pause / stop / kill. Empty
        # string when kill switch is inactive or when the value is
        # missing (older installs that pre-date kill_switch_mode).
        kill_switch_mode = (
            (await ctx.db.get_system_state("kill_switch_mode")) or ""
        ) if kill_switch else ""
        orchestrator_state = await ctx.db.get_system_state("orchestrator")

        # Build degraded mode report: which features are running with fallbacks
        degraded: list[dict[str, str]] = []

        if not ctx.config.llm.enabled:
            degraded.append({
                "feature": "LLM (Gemini)",
                "status": "disabled",
                "impact": "Sentiment analysis off, trade review auto-approves, "
                          "market summaries unavailable",
            })
        elif not ctx.config.llm.api_key.get_secret_value():
            degraded.append({
                "feature": "LLM (Gemini)",
                "status": "no_api_key",
                "impact": "LLM enabled but no API key — all LLM calls use stub defaults",
            })

        if not ctx.config.market_data.news_enabled:
            degraded.append({
                "feature": "News sources",
                "status": "disabled",
                "impact": "No sentiment data from MoneyControl, ET Markets, LiveMint",
            })

        if not ctx.config.market_data.scrapers_enabled:
            degraded.append({
                "feature": "Scrapers",
                "status": "disabled",
                "impact": "No fundamentals (Screener.in), technicals (Trendlyne), "
                          "economic calendar, or Google Finance data",
            })

        if not ctx.config.notifications.telegram.enabled:
            degraded.append({
                "feature": "Telegram",
                "status": "disabled",
                "impact": "No Telegram alerts — console/dashboard only",
            })

        if not ctx.config.risk.llm_review_enabled:
            degraded.append({
                "feature": "LLM trade review",
                "status": "disabled",
                "impact": "All trades auto-approved without AI review",
            })
        elif not ctx.config.llm.enabled:
            degraded.append({
                "feature": "LLM trade review",
                "status": "fallback",
                "impact": "LLM review enabled but LLM disabled — "
                          "trades auto-approved via rules-only fallback",
            })

        # Count today's auto-approved trades (no LLM review)
        auto_approved_today = 0
        llm_reviewed_today = 0
        try:
            cursor = await ctx.db.conn.execute(
                "SELECT decision, COUNT(*) as cnt FROM llm_reviews "
                "WHERE created_at >= date('now', 'start of day') "
                "GROUP BY decision"
            )
            rows = await cursor.fetchall()
            for row in rows:
                decision = (dict(row).get("decision") or "").upper()
                cnt = dict(row).get("cnt", 0)
                if decision == "AUTO_APPROVE":
                    auto_approved_today += cnt
                else:
                    llm_reviewed_today += cnt
        except Exception:
            logger.debug("Failed to fetch LLM review counts", exc_info=True)

        return {
            "kill_switch_active": kill_switch,
            "kill_switch_mode": kill_switch_mode,
            "orchestrator": orchestrator_state,
            "mode": ctx.config.mode,
            "degraded_features": degraded,
            "is_degraded": len(degraded) > 0,
            "show_degraded_banner": ctx.config.dashboard.show_degraded_banner,
            "auto_approved_today": auto_approved_today,
            "llm_reviewed_today": llm_reviewed_today,
        }

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
        import asyncio as _asyncio
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
                row = await ctx.db.read_conn.execute_fetchall(
                    "SELECT close FROM ohlcv WHERE symbol = ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (sym,),
                )
                if row and row[0][0] is not None:
                    return sym, float(row[0][0])
            except Exception:
                pass
            return sym, None

        pairs = await _asyncio.gather(*[_resolve(s) for s in syms])
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

    # ------------------------------------------------------------------
    # Risk Simulator (Feature #6)
    # ------------------------------------------------------------------

    @app.post("/api/risk-simulator")
    async def run_risk_simulation(
        body: dict[str, Any],
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Replay historical signals or executed trades against modified
        risk parameters.

        `source` ("signals" — default — or "trades") controls the
        replay set. "trades" reads from the trades table so the user
        can see "what if I'd applied tighter caps to my actual fills"
        — useful when most signals never executed (paper mode quirks,
        risk rejections, etc.) and the signal-derived view feels
        misleadingly empty.
        """
        max_exposure_pct = body.get("max_exposure_pct", ctx.config.risk.max_portfolio_exposure_pct)
        max_single_stock_pct = body.get("max_single_stock_pct", ctx.config.risk.max_single_stock_pct)
        max_positions = body.get("max_positions", ctx.config.risk.max_open_positions)
        initial_capital = body.get("initial_capital", 100000)
        date_from = body.get("date_from")  # YYYY-MM-DD or None
        date_to = body.get("date_to")  # YYYY-MM-DD or None
        source = body.get("source", "signals")

        if source == "trades":
            # Pull executed/closed trades for the current mode and
            # reshape into the same dict structure the signal path
            # uses below so the simulation loop stays unified.
            raw = await ctx.db.get_trades_history(
                start_date=date_from, end_date=date_to,
                limit=2000, mode=ctx.config.mode,
            )
            # Closed trades carry pnl; open ones don't (skipped below).
            raw.sort(key=lambda t: t.get("created_at") or "")
            signals: list[dict[str, Any]] = []
            for t in raw:
                signals.append({
                    "symbol": t.get("symbol"),
                    "signal_type": t.get("signal_type"),
                    "entry_price": t.get("fill_price") or t.get("entry_price"),
                    "quantity": t.get("quantity"),
                    "position_size": t.get("quantity"),
                    "pnl": t.get("pnl"),
                    "created_at": t.get("created_at"),
                })
        else:
            signals = await ctx.db.get_historical_signals(
                limit=500, date_from=date_from, date_to=date_to,
            )

        # Simple simulation
        capital = float(initial_capital)
        open_pos = 0
        exposure = 0.0
        stock_exposure: dict[str, float] = {}
        trades_taken = 0
        trades_skipped = 0
        signals_without_pnl = 0
        total_pnl = 0.0
        wins = 0
        losses = 0
        peak = capital
        max_drawdown = 0.0

        for sig in signals:
            pnl = sig.get("pnl")
            if pnl is None:
                signals_without_pnl += 1
                continue

            qty = sig.get("quantity", sig.get("position_size", 0))
            entry = sig.get("entry_price", 0)
            value = qty * entry if qty and entry else 0
            symbol = sig.get("symbol", "")
            new_exposure = (exposure + value) / capital if capital > 0 else 1

            # Apply risk filters
            if new_exposure > max_exposure_pct:
                trades_skipped += 1
                continue
            sym_exp = (stock_exposure.get(symbol, 0) + value) / capital if capital > 0 else 1
            if sym_exp > max_single_stock_pct:
                trades_skipped += 1
                continue
            if open_pos >= max_positions:
                trades_skipped += 1
                continue

            # Take trade
            trades_taken += 1
            exposure += value
            stock_exposure[symbol] = stock_exposure.get(symbol, 0) + value
            open_pos += 1
            capital += pnl
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            if capital > peak:
                peak = capital
            dd = (peak - capital) / peak if peak > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

        win_rate = wins / trades_taken if trades_taken > 0 else 0

        return {
            "params": {
                "max_exposure_pct": max_exposure_pct,
                "max_single_stock_pct": max_single_stock_pct,
                "max_positions": max_positions,
                "initial_capital": initial_capital,
                "date_from": date_from,
                "date_to": date_to,
                "source": source,
            },
            "signals_available": len(signals),
            "signals_without_pnl": signals_without_pnl,
            "results": {
                "trades_taken": trades_taken,
                "trades_skipped": trades_skipped,
                "total_pnl": round(total_pnl, 2),
                "final_capital": round(capital, 2),
                "win_rate": round(win_rate, 4),
                "wins": wins,
                "losses": losses,
                "max_drawdown_pct": round(max_drawdown * 100, 2),
                "return_pct": round((capital - initial_capital) / initial_capital * 100, 2),
            },
        }

    # ------------------------------------------------------------------
    # Data Management: Storage Stats & Cleanup
    # ------------------------------------------------------------------

    @app.get("/api/storage-stats")
    async def storage_stats(_user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Get row counts, date ranges, and DB file size for all tables."""
        return await ctx.db.get_storage_stats()

    @app.post("/api/cleanup")
    async def cleanup_data(
        table: str = Query(..., description="Table to clean up"),
        older_than_days: int = Query(..., ge=1, description="Delete rows older than N days"),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete old data from a specific table to free up space."""
        try:
            deleted = await ctx.db.cleanup_table(table, older_than_days)
            # VACUUM to reclaim disk space after large deletes
            if deleted > 100:
                await ctx.db.conn.execute("VACUUM")
            ctx.db.invalidate_storage_stats_cache()
            return {"success": True, "table": table, "rows_deleted": deleted}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # ------------------------------------------------------------------
    # Dry-Run Signal Preview
    # ------------------------------------------------------------------

    @app.post("/api/dry-run")
    async def run_dry_run_signals(
        mode: str | None = Query(
            default=None,
            pattern=r"^(intraday|short_term|balanced|long_term)$",
            description="Strategy mode override (intraday, short_term, balanced, long_term)",
        ),
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Run market-scan + signal generation on current data (read-only, no trades).

        Works regardless of market hours. Results are stored for next-day comparison.
        Optional `mode` query param overrides the configured strategy.mode for this run.
        """
        from datetime import datetime as dt

        from yolovest.config import _MODE_HOLDING_DAYS, _MODE_HOLDING_PERIODS
        from yolovest.costs import compute_transaction_costs
        from yolovest.data.features import IndicatorConfig, compute_features
        from yolovest.skills.generate_signals import _format_class_probs
        from yolovest.strategy.holding_period import adjust_sell_for_holdings, decide_holding_period, interpolate_atr_multipliers
        from yolovest.timezone import IST

        run_id = str(uuid.uuid4())[:8]
        cfg = ctx.config

        # Resolve effective strategy mode and allowed holding periods
        effective_mode = mode or cfg.strategy.mode
        mode_days_range = _MODE_HOLDING_DAYS.get(effective_mode)
        allowed_periods = _MODE_HOLDING_PERIODS.get(
            effective_mode, cfg.strategy.allowed_holding_periods or ["intraday", "short_term", "long_term"],
        )

        # Step 1: Run market-scan logic (without writing to watchlist)
        universe = await ctx.db.get_nse_universe()
        if not universe:
            universe = [
                {"symbol": s, "avg_daily_volume": cfg.scanning.min_avg_daily_volume + 1}
                for s in cfg.scanning.seed_symbols
            ]
        liquid = [
            s for s in universe
            if (s.get("avg_daily_volume") or 0) >= cfg.scanning.min_avg_daily_volume
        ]

        # Score stocks (including volatility)
        weights = cfg.scanning.weights
        scored = []
        for stock in liquid:
            sub = _compute_scan_scores(stock, cfg.scanning.min_avg_daily_volume, cfg.strategy.volatility)
            composite = (
                sub["technical_score"] * weights.technical
                + sub["volume_momentum_score"] * weights.volume_momentum
                + sub["news_sentiment_score"] * weights.news_sentiment
                + sub["fundamental_score"] * weights.fundamental
                + sub["volatility_score"] * weights.volatility
            )
            scored.append({**stock, **sub, "composite_score": composite})

        scored.sort(
            key=lambda s: (s["composite_score"], s.get("avg_daily_volume") or 0),
            reverse=True,
        )
        shortlist = scored[: cfg.scanning.shortlist_size]

        if not shortlist:
            return {
                "success": True,
                "run_id": run_id,
                "mode": effective_mode,
                "universe_size": len(universe),
                "shortlist_size": 0,
                "signals": [],
                "diagnostics": {
                    "min_confidence_threshold": min(cfg.risk.min_confidence_buy, cfg.risk.min_confidence_sell),
                    "min_confidence_buy": cfg.risk.min_confidence_buy,
                    "min_confidence_sell": cfg.risk.min_confidence_sell,
                    "ml_available": ctx.ml is not None,
                    "filter_counts": {
                        "insufficient_bars": 0, "feature_computation_failed": 0,
                        "ml_unavailable": 0, "hold_signal": 0,
                        "low_confidence": 0, "error": 0, "passed": 0,
                    },
                    "rejection_details": [],
                },
            }

        # Step 2: Generate signals from shortlisted stocks
        signals_out: list[dict[str, Any]] = []
        ml_unavailable = ctx.ml is None

        # Build held symbols set for SELL signal adjustment
        open_positions = await ctx.db.get_open_positions()
        held_symbols = {p["symbol"] for p in open_positions}
        min_confidence_buy = cfg.risk.min_confidence_buy
        min_confidence_sell = cfg.risk.min_confidence_sell

        if ml_unavailable:
            logger.warning("Dry-run: ML model not loaded — cannot generate signals. "
                           "Train a model first via the model-retrain skill.")

        # Diagnostics: track why stocks get filtered out
        filter_counts = {
            "insufficient_bars": 0,
            "feature_computation_failed": 0,
            "ml_unavailable": 0,
            "hold_signal": 0,
            "low_confidence": 0,
            "error": 0,
            "passed": 0,
        }
        rejection_details: list[dict[str, str]] = []

        indicator_cfg = IndicatorConfig(
            ema_periods=cfg.strategy.ema_periods,
            rsi=cfg.strategy.indicators.rsi,
            macd=cfg.strategy.indicators.macd,
            bollinger_bands=cfg.strategy.indicators.bollinger_bands,
            vwap=cfg.strategy.indicators.vwap,
            atr=cfg.strategy.indicators.atr,
            volume_profile=cfg.strategy.indicators.volume_profile,
            obv=cfg.strategy.indicators.obv,
            supertrend=cfg.strategy.indicators.supertrend,
        )

        for stock in shortlist:
            symbol = stock["symbol"]
            try:
                bars = await ctx.db.get_ohlcv(symbol, "daily", days=365)
                if len(bars) < 50:
                    filter_counts["insufficient_bars"] += 1
                    rejection_details.append({
                        "symbol": symbol,
                        "reason": "insufficient_bars",
                        "detail": f"{len(bars)} bars < 50 required",
                    })
                    logger.info("Dry-run: Insufficient data for %s (%d bars)", symbol, len(bars))
                    continue

                features = compute_features(bars, indicator_cfg)
                if not features:
                    filter_counts["feature_computation_failed"] += 1
                    rejection_details.append({
                        "symbol": symbol,
                        "reason": "feature_computation_failed",
                        "detail": "compute_features returned empty",
                    })
                    logger.info("Dry-run: Feature computation failed for %s", symbol)
                    continue

                if ctx.ml is None:
                    filter_counts["ml_unavailable"] += 1
                    rejection_details.append({
                        "symbol": symbol,
                        "reason": "ml_unavailable",
                        "detail": "ML model not loaded",
                    })
                    continue

                # Fetch fresh LTP for realistic entry/target/SL
                current_price: float | None = None
                try:
                    current_price = await ctx.market_data.get_ltp(symbol)
                except Exception:
                    logger.debug("LTP unavailable for dry-run %s, using bar close", symbol)

                # Decide holding period based on features and selected strategy mode
                now_time = dt.now(IST).time()
                holding_period, product, expected_days = decide_holding_period(
                    features, allowed_periods, cfg.strategy.volatility, now_time,
                    mode_days_range=mode_days_range,
                )
                use_intraday = holding_period == "intraday"
                is_balanced = effective_mode == "balanced"

                if is_balanced:
                    # Balanced mode: run both models, pick higher confidence
                    import asyncio as _aio

                    intra_feat = {**features}
                    intraday_bars = await ctx.db.get_ohlcv(symbol, "5minute", days=1)
                    if intraday_bars:
                        intra_feat["close"] = intraday_bars[-1].close

                    intra_pred, swing_pred = await _aio.gather(
                        ctx.ml.predict_intraday(symbol, intra_feat, current_price=current_price),
                        ctx.ml.predict_swing(symbol, features, current_price=current_price),
                        return_exceptions=True,
                    )
                    if isinstance(intra_pred, BaseException):
                        intra_pred = None
                    if isinstance(swing_pred, BaseException):
                        swing_pred = None

                    intra_conf = intra_pred.confidence if intra_pred and intra_pred.signal_type != "HOLD" else -1
                    swing_conf = swing_pred.confidence if swing_pred and swing_pred.signal_type != "HOLD" else -1

                    if intra_conf < 0 and swing_conf < 0:
                        prediction = swing_pred or intra_pred
                    elif intra_conf >= swing_conf:
                        prediction = intra_pred
                        holding_period, product, expected_days = "intraday", "MIS", 0
                    else:
                        prediction = swing_pred
                        _, product, expected_days = decide_holding_period(
                            features, ["short_term", "long_term"],
                            cfg.strategy.volatility, now_time,
                            mode_days_range=(max(1, mode_days_range[0]) if mode_days_range else 1, mode_days_range[1] if mode_days_range else 15),
                        )
                        holding_period = "swing" if expected_days <= 5 else "positional" if expected_days <= 15 else "long_term"
                        product = "CNC"
                    use_intraday = holding_period == "intraday"
                elif use_intraday:
                    prediction = await ctx.ml.predict_intraday(
                        symbol, features, current_price=current_price,
                    )
                else:
                    prediction = await ctx.ml.predict_swing(
                        symbol, features, current_price=current_price,
                    )

                if prediction.signal_type == "HOLD":
                    filter_counts["hold_signal"] += 1
                    rejection_details.append({
                        "symbol": symbol,
                        "reason": "hold_signal",
                        "detail": f"HOLD @ confidence {prediction.confidence:.2f}",
                    })
                    logger.info(
                        "Dry-run: HOLD signal for %s (%s)",
                        symbol, _format_class_probs(prediction),
                    )
                    continue

                threshold = cfg.risk.resolve_min_confidence(
                    holding_period, prediction.signal_type,
                )
                if prediction.confidence < threshold:
                    filter_counts["low_confidence"] += 1
                    rejection_details.append({
                        "symbol": symbol,
                        "reason": "low_confidence",
                        "detail": f"{prediction.signal_type} @ confidence {prediction.confidence:.2f} < {threshold}",
                    })
                    logger.info(
                        "Dry-run: Low confidence for %s: %s @ %.2f < %.2f (%s)",
                        symbol, prediction.signal_type, prediction.confidence, threshold,
                        _format_class_probs(prediction),
                    )
                    continue

                # Adjust SELL: force to MIS/intraday if user doesn't hold
                # the stock. Drop when the per-symbol decision is swing —
                # mirrors generate-signals so the dry-run preview matches.
                _adjusted = adjust_sell_for_holdings(
                    prediction.signal_type, holding_period, product,
                    symbol, held_symbols, expected_days,
                )
                if _adjusted is None:
                    filter_counts.setdefault("short_on_swing_horizon", 0)
                    filter_counts["short_on_swing_horizon"] += 1
                    rejection_details.append({
                        "symbol": symbol,
                        "reason": "short_on_swing_horizon",
                        "detail": (
                            f"SELL on non-held {symbol} with "
                            f"holding_period='{holding_period}' would require "
                            f"intraday/MIS — dropped"
                        ),
                    })
                    continue
                holding_period, product, expected_days = _adjusted

                # Apply ATR multipliers interpolated for holding duration.
                # Mirror generate-signals: clamp intraday ATR at
                # holding_periods.intraday.max_atr_pct_for_target so the
                # dry-run preview shows the same target/SL geometry the
                # live engine would produce.
                entry = prediction.entry_price
                atr = features.get("atr_14", entry * 0.02)
                if holding_period == "intraday":
                    max_atr_pct = float(
                        cfg.strategy.holding_periods.intraday
                            .max_atr_pct_for_target
                    )
                    if max_atr_pct > 0:
                        atr = min(atr, entry * max_atr_pct)
                target_mult, sl_mult = interpolate_atr_multipliers(
                    expected_days, cfg.strategy.holding_periods,
                )

                if prediction.signal_type == "BUY":
                    target_price = round(max(entry + target_mult * atr, 0.01), 2)
                    stop_loss_price = round(max(entry - sl_mult * atr, 0.01), 2)
                elif prediction.signal_type == "SELL":
                    target_price = round(max(entry - target_mult * atr, 0.01), 2)
                    stop_loss_price = round(max(entry + sl_mult * atr, 0.01), 2)
                else:
                    target_price = prediction.target_price
                    stop_loss_price = prediction.stop_loss_price

                # Estimate transaction costs
                est_costs = compute_transaction_costs(
                    entry, target_price, prediction.position_size,
                    product=product, cost_config=cfg.transaction_costs,
                )

                filter_counts["passed"] += 1
                logger.info(
                    "Dry-run: PASSED %s for %s @ %.2f (%s)",
                    prediction.signal_type, symbol,
                    prediction.confidence,
                    _format_class_probs(prediction),
                )
                signals_out.append({
                    "symbol": symbol,
                    "signal_type": prediction.signal_type,
                    "entry_price": entry,
                    "target_price": target_price,
                    "stop_loss_price": stop_loss_price,
                    "confidence_score": prediction.confidence,
                    "position_size": prediction.position_size,
                    "model_version": prediction.model_version,
                    "holding_period": holding_period,
                    "expected_holding_days": expected_days,
                    "product": product,
                    "estimated_costs": est_costs,
                    "composite_score": stock.get("composite_score"),
                    "technical_score": stock.get("technical_score"),
                    "volume_momentum_score": stock.get("volume_momentum_score"),
                    "news_sentiment_score": stock.get("news_sentiment_score"),
                    "fundamental_score": stock.get("fundamental_score"),
                    "volatility_score": stock.get("volatility_score"),
                    "strategy_mode": effective_mode,
                })
            except Exception as e:
                filter_counts["error"] += 1
                rejection_details.append({
                    "symbol": symbol,
                    "reason": "error",
                    "detail": str(e),
                })
                logger.warning("Dry-run signal failed for %s: %s", symbol, e)

        # Log diagnostics summary (always, not just on 0 signals)
        logger.info(
            "Dry-run %s (%s mode) complete: scanned %d stocks, shortlisted %d, "
            "generated %d signals — %s",
            run_id, effective_mode, len(universe), len(shortlist),
            len(signals_out), filter_counts,
        )

        # Step 3: Persist for next-day comparison
        if signals_out:
            await ctx.db.insert_dry_run_results(run_id, signals_out)

        result: dict[str, Any] = {
            "success": True,
            "run_id": run_id,
            "mode": effective_mode,
            "universe_size": len(universe),
            "shortlist_size": len(shortlist),
            "signals": signals_out,
            "diagnostics": {
                "min_confidence_threshold": min(cfg.risk.min_confidence_buy, cfg.risk.min_confidence_sell),
                "min_confidence_buy": cfg.risk.min_confidence_buy,
                "min_confidence_sell": cfg.risk.min_confidence_sell,
                "ml_available": ctx.ml is not None,
                "filter_counts": filter_counts,
                "rejection_details": rejection_details,
            },
        }
        if ml_unavailable:
            result["warning"] = (
                "ML model is not loaded — 0 signals generated. "
                "Run the model-retrain skill first to train an XGBoost model, "
                "then re-run the dry run."
            )
        return result

    @app.get("/api/dry-run/history")
    async def get_dry_run_history(
        limit: int = Query(default=10, ge=1, le=50),
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get past dry-run summaries."""
        return await ctx.db.get_dry_run_history(limit)

    @app.get("/api/dry-run/{run_id}")
    async def get_dry_run_detail(
        run_id: str,
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get all signals for a specific dry-run."""
        return await ctx.db.get_dry_run_signals(run_id)

    @app.post("/api/dry-run/{run_id}/score")
    async def score_dry_run(
        run_id: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Score a dry-run against actual next-day market data."""
        return await ctx.db.score_dry_run(run_id)

    @app.delete("/api/dry-run/{run_id}")
    async def delete_dry_run(
        run_id: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a dry-run and all its signals."""
        deleted = await ctx.db.delete_dry_run(run_id)
        logger.info("Dry-run %s deleted (%d signals removed)", run_id, deleted)
        return {"success": True, "run_id": run_id, "deleted": deleted}

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

    def _model_dir() -> str:
        return getattr(ctx.config.strategy, "model_dir", "./models")

    @app.post("/api/backup")
    async def create_backup(_user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Create a manual database backup including ML model artifacts."""
        backup_dir = ctx.config.database.backup_dir
        backup_path = await ctx.db.backup(backup_dir, model_dir=_model_dir())
        return {"success": True, "backup_path": backup_path}

    @app.get("/api/backups")
    async def list_backups(_user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """List available database backups."""
        backup_dir = ctx.config.database.backup_dir
        return await ctx.db.list_backups(backup_dir)

    @app.post("/api/restore/{filename}")
    async def restore_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Restore a database backup. Application should be restarted after restore."""
        backup_dir = ctx.config.database.backup_dir
        result = await ctx.db.restore_backup(
            backup_dir, filename, model_dir=_model_dir(),
        )
        ctx.db.invalidate_storage_stats_cache()
        return {"success": True, **result}

    @app.delete("/api/backups/{filename}")
    async def delete_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a single backup file. Returns the freed size in bytes."""
        backup_dir = ctx.config.database.backup_dir
        try:
            result = await ctx.db.delete_backup(backup_dir, filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except PermissionError as e:
            # Locked backup — refuse with 409 Conflict so the UI can
            # prompt the user to unlock first.
            raise HTTPException(status_code=409, detail=str(e))
        logger.info("Deleted backup %s (%d bytes)", filename, result["size_bytes"])
        return {"success": True, **result}

    @app.post("/api/backups/{filename}/lock")
    async def lock_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Mark a backup as locked so the daily prune + manual delete
        paths skip it. Idempotent.
        """
        backup_dir = ctx.config.database.backup_dir
        try:
            result = await ctx.db.set_backup_lock(backup_dir, filename, locked=True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        logger.info("Locked backup %s", filename)
        return {"success": True, **result}

    @app.post("/api/backups/{filename}/unlock")
    async def unlock_backup(
        filename: str, _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Clear the lock sentinel on a backup so it's eligible for
        prune / delete again. Idempotent.
        """
        backup_dir = ctx.config.database.backup_dir
        try:
            result = await ctx.db.set_backup_lock(backup_dir, filename, locked=False)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        logger.info("Unlocked backup %s", filename)
        return {"success": True, **result}

    @app.post("/api/bulk-delete/{group}")
    async def bulk_delete(
        group: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a group of related data: paper, live, dry_runs, predictions, signals."""
        try:
            deleted = await ctx.db.bulk_delete(group)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        ctx.db.invalidate_storage_stats_cache()
        total = sum(deleted.values())
        logger.info("Bulk delete [%s]: %d rows", group, total)
        return {"success": True, "group": group, "deleted": deleted, "total": total}

    @app.post("/api/reset")
    async def reset_all_data(_user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Delete ALL data from all tables and model artifacts. Schema is preserved."""
        deleted = await ctx.db.reset_all_data()
        ctx.db.invalidate_storage_stats_cache()
        total = sum(deleted.values())
        # Also clean up all model artifacts
        model_cleanup = await ctx.db.cleanup_orphaned_models(_model_dir())
        return {
            "success": True,
            "total_rows_deleted": total,
            "by_table": deleted,
            "model_files_deleted": model_cleanup.get("orphaned_files_deleted", 0),
        }

    @app.delete("/api/ml-models/{model_type}/{version}")
    async def delete_model(
        model_type: str, version: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Delete a model version (DB record + .pkl artifact)."""
        result = await ctx.db.delete_model_version(
            model_type, version, model_dir=_model_dir(),
        )
        return {"success": True, **result}

    # ------------------------------------------------------------------
    # Pending Trades (manual approval)
    # ------------------------------------------------------------------

    @app.get("/api/pending-trades")
    async def get_pending_trades(
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Get all trades awaiting manual approval."""
        # Defensive sweep — heartbeat already runs this each cycle, but
        # the UI render happens to be a convenient backstop if the
        # heartbeat is paused or wedged.
        expiry_min = ctx.config.execution.pending_expiry_minutes
        expired = await ctx.db.expire_pending_trades(max_age_minutes=expiry_min)
        if expired:
            logger.info("Expired %d stale pending trades (>%dmin old)", expired, expiry_min)
            try:
                await broadcast_ws("pending_expired", {"count": expired})
            except Exception:
                logger.debug("pending_expired broadcast failed", exc_info=True)
        return await ctx.db.get_pending_trades()

    @app.post("/api/clear-signals")
    async def clear_todays_signals(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Clear today's signals and pending trades to allow signal regeneration."""
        result = await ctx.db.clear_todays_signals()
        return {"success": True, **result}

    @app.post("/api/kill-switch/{command}")
    async def kill_switch(
        command: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Stop / kill / resume trading from the dashboard.

        - stop: cancel pending orders + pause; positions untouched.
        - kill: cancel pending orders + square off every position + pause.
        - resume: clear the pause flag.

        Mirrors the /stop /kill /resume Telegram commands. The generic
        /api/skills/{name}/run endpoint can't carry a command parameter,
        so this is a dedicated surface.
        """
        if command not in {"pause", "stop", "kill", "resume"}:
            raise HTTPException(
                status_code=400,
                detail="command must be one of: pause, stop, kill, resume",
            )
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(ctx)
        result = await skill.execute(command=command)
        return {
            "success": result.success,
            "command": command,
            "data": result.data or {},
            "error": result.error,
        }

    @app.post("/api/pending-trades/{trade_id}/approve")
    async def approve_pending_trade(
        trade_id: int,
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Approve a pending trade for execution, with optional overrides."""
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        overrides = body.get("overrides")  # optional: {signal_type, entry_price, target_price, stop_loss_price, product}

        signal = await ctx.db.decide_pending_trade(
            trade_id, "approved", "dashboard", overrides=overrides,
        )
        if signal is None:
            raise HTTPException(status_code=404, detail="Trade not found or already decided")

        # Execute the trade
        from yolovest.skills.trade_execute import TradeExecuteSkill
        skill = TradeExecuteSkill(ctx)
        logger.info(
            "Executing approved trade #%d: %s %s (mode=%s)",
            trade_id, signal.get("signal_type"), signal.get("symbol"), ctx.config.mode,
        )
        result = await skill.safe_execute(signal=signal)

        if result.success:
            trade = result.data.get("trade", {}) if result.data else {}
            exec_mode = result.data.get("mode", ctx.config.mode) if result.data else ctx.config.mode
            logger.info(
                "Trade #%d executed: %s %s mode=%s order=%s trade_id=%s",
                trade_id, trade.get("signal_type"), trade.get("symbol"),
                exec_mode, trade.get("order_id", "N/A"), trade.get("trade_id", "N/A"),
            )
            # Mark the originating signal as executed so Today's
            # Recommendations stops showing it as AWAITING APPROVAL.
            # Auto-mode path does this in orchestrator._run_signal; the
            # manual approve flow has to do it here.
            try:
                await ctx.db.update_signal_disposition(
                    signal.get("symbol", ""), "executed",
                    f"trade_id={trade.get('trade_id') or trade.get('order_id')}",
                    position_size=int(trade.get("quantity") or 0) or None,
                )
            except Exception:
                logger.debug("Failed to mark signal executed", exc_info=True)
            try:
                await broadcast_ws("pending_approved", {
                    "trade_id": trade_id, "symbol": signal.get("symbol"),
                })
            except Exception:
                logger.debug("pending_approved broadcast failed", exc_info=True)
            return {"success": True, "trade": trade, "mode": exec_mode}
        logger.error(
            "Trade #%d execution failed: %s", trade_id, result.error,
        )
        # Revert pending trade back to 'pending' so user can retry
        try:
            await ctx.db.conn.execute(
                "UPDATE pending_trades SET status = 'pending', decided_at = NULL, "
                "decided_by = NULL WHERE id = ? AND status = 'approved'",
                (trade_id,),
            )
            await ctx.db.conn.commit()
            logger.info("Reverted pending trade #%d back to pending", trade_id)
        except Exception:
            logger.debug("Failed to revert pending trade #%d", trade_id, exc_info=True)
        return {"success": False, "error": result.error, "reverted": True}

    @app.post("/api/manual-trade")
    async def create_manual_trade(
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Place a manual trade directly (not from ML prediction)."""
        body = await request.json()
        required = ["symbol", "signal_type", "entry_price", "target_price", "stop_loss_price"]
        missing = [k for k in required if k not in body]
        if missing:
            raise HTTPException(400, f"Missing fields: {missing}")

        body["decided_by"] = "dashboard"
        trade_id = await ctx.db.insert_manual_trade(body)

        # Execute immediately
        from yolovest.skills.trade_execute import TradeExecuteSkill
        signal = {**body, "position_size": body.get("position_size", 1)}
        skill = TradeExecuteSkill(ctx)
        result = await skill.execute(signal=signal)

        trade = result.data.get("trade", {}) if result.data else {}
        return {"success": result.success, "trade": trade, "pending_id": trade_id, "error": result.error}

    @app.post("/api/pending-trades/{trade_id}/reject")
    async def reject_pending_trade(
        trade_id: int,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Reject a pending trade."""
        await ctx.db.decide_pending_trade(trade_id, "rejected", "dashboard")
        logger.info("Rejected pending trade #%d", trade_id)
        try:
            await broadcast_ws("pending_rejected", {"trade_id": trade_id})
        except Exception:
            logger.debug("pending_rejected broadcast failed", exc_info=True)
        return {"success": True}

    @app.post("/api/change-password")
    async def change_password(
        body: dict[str, Any],
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Change the dashboard password at runtime."""
        new_password = body.get("new_password", "").strip()
        if len(new_password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
        _password["current"] = new_password
        # Persist to DB so it survives restarts
        await ctx.db.set_system_state("dashboard_password", new_password)
        return {"success": True}

    @app.post("/api/config/reload")
    async def reload_config(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Reload config.yaml without restart (same as kill -HUP).

        Only reloads safe runtime settings. Structural changes
        (broker, DB, LLM provider) still require a full restart.
        """
        reload_fn = getattr(ctx, "_reload_config", None)
        if reload_fn is None:
            raise HTTPException(
                status_code=501,
                detail="Config reload not available (missing reload handler)",
            )
        try:
            result = reload_fn()
            return result
        except Exception as e:
            logger.error("Config reload via API failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Reload failed: {e}")

    # ------------------------------------------------------------------
    # Config (UI-editable settings)
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Return all DB-editable config values grouped by section."""
        from yolovest.config import config_to_ui_sections, FILE_ONLY_KEYS
        sections = config_to_ui_sections(ctx.config)
        return {"sections": sections}

    @app.put("/api/config")
    async def update_config(
        request: Request,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Update config values. Body: {"updates": {"risk.max_open_positions": 5, ...}}

        Validates all changes through Pydantic before persisting.
        Returns the updated config sections.
        """
        from yolovest.config import (
            FILE_ONLY_KEYS,
            apply_db_config,
            config_to_ui_sections,
            _flatten_model,
        )

        body = await request.json()
        updates: dict[str, Any] = body.get("updates", {})
        if not updates:
            raise HTTPException(status_code=400, detail="No updates provided")

        # Reject file-only keys
        rejected = [k for k in updates if k in FILE_ONLY_KEYS]
        if rejected:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot modify file-only keys via UI: {rejected}",
            )

        # Convert all values to strings for DB storage
        import json as _json

        str_updates: dict[str, str] = {}
        for k, v in updates.items():
            if isinstance(v, (bool, list, dict)) or v is None:
                str_updates[k] = _json.dumps(v)
            else:
                str_updates[k] = str(v)

        # Load current DB config, overlay updates, validate via Pydantic
        db_values = await ctx.db.get_all_config()
        db_values.update(str_updates)
        try:
            new_config = apply_db_config(ctx.config, db_values)
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Validation failed: {e}",
            )

        # Capture old values BEFORE persisting so the diff log shows
        # what each key actually changed from. `db_values` was loaded
        # above before the overlay was applied, so it still holds the
        # pre-update state.
        old_values: dict[str, str | None] = {}
        for k in updates:
            # Note: read from the freshly-loaded snapshot (it was
            # mutated by the overlay; use ctx.config flattened instead
            # to be robust).
            try:
                v = await ctx.db.get_config(k)
            except Exception:
                v = None
            old_values[k] = v

        # Persist to DB
        await ctx.db.set_config_bulk(str_updates)

        # Hot-apply to running config
        old_config = ctx.config
        ctx.config = new_config
        ctx.market_hours = MarketHoursChecker(ctx.config)
        # Sync Notifier's config reference
        if hasattr(ctx.notify, "_config"):
            ctx.notify._config = ctx.config

        # Side effects for specific keys
        if any(k.startswith("log.") for k in updates):
            try:
                from yolovest.main import setup_logging
                setup_logging(ctx.config)
                logger.info("Log levels reloaded: console=%s, file=%s",
                            ctx.config.log.level, ctx.config.log.file_level)
            except Exception as e:
                logger.warning("Failed to reload log levels: %s", e)

        if "mode" in updates:
            logger.info("Trading mode changed: %s -> %s", old_config.mode, new_config.mode)
            # Sync to broker — it stores its own _mode for order routing
            if hasattr(ctx.broker, "_mode"):
                ctx.broker._mode = new_config.mode
                logger.info("Broker mode synced to: %s", new_config.mode)

        # Emit per-key diff so the audit trail records what each key
        # actually changed from -> to (instead of just the key list).
        for k, new_v in str_updates.items():
            old_v = old_values.get(k)
            if old_v == new_v:
                continue
            logger.info(
                "Config updated via UI: %s: %r -> %r",
                k,
                old_v if old_v is not None else "<unset>",
                new_v,
            )

        sections = config_to_ui_sections(ctx.config)
        return {"status": "ok", "updated": list(updates.keys()), "sections": sections}

    # ------------------------------------------------------------------
    # Manual Skill Trigger
    # ------------------------------------------------------------------

    @app.get("/api/skills")
    async def list_skills(
        _user: str = Depends(verify_credentials),
    ) -> list[dict[str, str | None]]:
        """List all registered skills with metadata and runtime schedules."""
        from yolovest.skills import SKILL_REGISTRY

        out = []
        for name, cls in sorted(SKILL_REGISTRY.items()):
            # Instantiate to get runtime schedule (set from config in __init__)
            try:
                instance = cls(ctx)
                schedule = instance.schedule
            except Exception:
                logger.debug("Failed to instantiate skill %s for schedule", name, exc_info=True)
                schedule = cls.schedule
            out.append({
                "name": name,
                "description": cls.description,
                "trigger": cls.trigger.value,
                "schedule": schedule,
            })
        return out

    # Track background skill tasks
    _running_skills: dict[str, asyncio.Task[Any]] = {}

    @app.post("/api/skills/{skill_name}/run")
    async def run_skill(
        skill_name: str,
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Manually trigger a registered skill by name.

        Long-running skills run in the background and return immediately.
        Results are broadcast via WebSocket when complete.
        """
        from yolovest.skills import SKILL_REGISTRY

        if skill_name not in SKILL_REGISTRY:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown skill: {skill_name}. "
                f"Available: {sorted(SKILL_REGISTRY.keys())}",
            )

        # Check if already running
        existing = _running_skills.get(skill_name)
        if existing and not existing.done():
            return {"success": True, "skill": skill_name, "status": "already_running"}

        skill_cls = SKILL_REGISTRY[skill_name]
        skill = skill_cls(ctx)

        async def _run_in_background() -> None:
            logger.info("Background skill started: %s", skill_name)
            try:
                result = await skill.safe_execute()
                logger.info(
                    "Background skill %s completed: success=%s, duration=%.1fms",
                    skill_name, result.success, result.duration_ms,
                )
                # Audit log
                try:
                    await ctx.db.log_audit(
                        action_type="manual_skill_execution",
                        skill_name=skill_name,
                        output_summary={
                            "success": result.success,
                            "duration_ms": round(result.duration_ms, 1),
                            "error": result.error,
                        },
                        duration_ms=result.duration_ms,
                    )
                except Exception:
                    logger.debug("Failed to log audit for manual skill %s", skill_name, exc_info=True)
                await broadcast_ws("skill_completed", {
                    "skill": skill_name,
                    "success": result.success,
                    "duration_ms": round(result.duration_ms, 1),
                    "error": result.error,
                    "data": {k: v for k, v in result.data.items()
                             if isinstance(v, (str, int, float, bool, type(None)))}
                    if result.data else {},
                })
            except Exception as e:
                logger.exception("Background skill run failed: %s", skill_name)
                await broadcast_ws("skill_completed", {
                    "skill": skill_name,
                    "success": False,
                    "error": str(e),
                })
            finally:
                _running_skills.pop(skill_name, None)

        import asyncio
        task = asyncio.create_task(_run_in_background())
        _running_skills[skill_name] = task

        return {"success": True, "skill": skill_name, "status": "started"}

    # ------------------------------------------------------------------
    # WebSocket Live Updates
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WebSocket for real-time trade and position updates."""
        await websocket.accept()
        _ws_clients.add(websocket)
        try:
            while True:
                # Keep connection alive, listen for client messages
                await websocket.receive_text()
        except WebSocketDisconnect:
            _ws_clients.discard(websocket)

    # ------------------------------------------------------------------
    # Static frontend serving (production)
    # ------------------------------------------------------------------
    frontend_dist = Path(__file__).resolve().parent.parent.parent.parent.parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        # Serve built React assets
        app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="static")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:
            """Serve the React SPA for any non-API route."""
            file_path = frontend_dist / full_path
            if file_path.is_file():
                return FileResponse(str(file_path))
            return FileResponse(str(frontend_dist / "index.html"))

    return app


async def broadcast_ws(event_type: str, data: dict[str, Any]) -> None:
    """Broadcast an event to all connected WebSocket clients."""
    global _ws_clients
    message = json.dumps({"type": event_type, "data": data})
    disconnected = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    _ws_clients -= disconnected
