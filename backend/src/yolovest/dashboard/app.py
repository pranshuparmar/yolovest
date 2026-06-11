"""FastAPI dashboard for YoloVest.

REST API + WebSocket for portfolio overview, trade detail, reports, and auth.
All endpoints read from the shared database via AppContext.

Security:
- Session token auth: POST /api/auth/login returns a signed HMAC token
- Bearer token in Authorization header for all subsequent requests
- Basic auth still supported for backwards compatibility (CLI, curl)
- CSRF protection: state-changing endpoints require X-CSRF-Token header
"""

import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from yolovest.context import AppContext
from yolovest.data.db import DASHBOARD_READ_CONN

logger = logging.getLogger(__name__)

# Auth primitives + shared helpers moved to sibling modules; re-exported
# here because main.py and the test-suite import them from dashboard.app.
from yolovest.dashboard import ws as ws_module
from yolovest.dashboard.deps import Deps
from yolovest.dashboard.helpers import (  # noqa: F401  (re-exports)
    _build_cdsl_response,
    _compute_capital_breakdown,
    _compute_cdsl_alert_gate,
    _compute_cdsl_status,
    _compute_holdings_breakdown,
    _compute_scan_scores,
    _compute_total_capital,
    _compute_volatility_score,
    _extract_available_cash,
    _extract_broker_capital,
    _extract_utilised_margin,
    _holdings_value,
    _is_cdsl_tpin_error,
)
from yolovest.dashboard.postback import (  # noqa: F401  (re-exports)
    _apply_order_postback,
    _close_on_exit_fill,
)
from yolovest.dashboard.routes import (
    analytics,
    config_api,
    data_mgmt,
    dryrun,
    holdings,
    integrations,
    market,
    models,
    portfolio,
    positions,
    predictions,
    quarantine,
    simulator,
    skills_api,
    symbols,
    system,
    trades,
    trading_controls,
    watchlist,
)
from yolovest.dashboard.security import (
    _TOKEN_TTL_SEC,
    _client_ip,
    _LoginThrottle,
    _sign_token,
    _verify_token,
    security,
)
from yolovest.dashboard.ws import _ws_clients, broadcast_ws  # noqa: F401


class _DashboardReadConnMiddleware:
    """Pure-ASGI middleware that flags each HTTP request so DB reads route to
    the dedicated dashboard read connection (see db.DASHBOARD_READ_CONN).

    Set on the inbound path before any downstream middleware/endpoint runs, so
    the value is captured when Starlette's BaseHTTPMiddleware copies the
    context into its task. WebSocket/lifespan scopes are passed through
    untouched (they default to the engine read connection).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = DASHBOARD_READ_CONN.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            DASHBOARD_READ_CONN.reset(token)



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

    # Route DB reads for HTTP requests to the dedicated dashboard read
    # connection. Registered last so it's the outermost middleware — the flag
    # is set before any inner middleware spawns its own task and copies the
    # context, so it propagates all the way to the endpoint.
    app.add_middleware(_DashboardReadConnMiddleware)

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

    # Brute-force throttle shared by /api/auth/login and Basic auth.
    _throttle = _LoginThrottle()

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

        # Fall back to Basic auth. Password attempts count toward the
        # same brute-force throttle as the login endpoint — otherwise
        # Basic auth on any protected route is an unthrottled oracle.
        if credentials is not None:
            ip = _client_ip(request)
            wait = _throttle.locked_for(ip)
            if wait > 0:
                raise HTTPException(
                    status_code=429,
                    detail="Too many failed attempts. Try again later.",
                    headers={"Retry-After": str(int(wait) + 1)},
                )
            correct = secrets.compare_digest(credentials.password, _password["current"])
            if correct:
                _throttle.record_success(ip)
                return credentials.username
            _throttle.record_failure(ip)

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Bearer, Basic realm="YoloVest"'},
        )

    def verify_download_credentials(
        request: Request,
        token: str | None = Query(None),
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> str:
        """Auth for large file downloads. Accepts a short-lived `?token=`
        query param (so the browser can stream the file to disk natively
        — an <a> download can't carry the Authorization header) and falls
        back to the normal Bearer/Basic header auth.
        """
        if token:
            return _verify_token(token)
        return verify_credentials(request, credentials)

    # Download token — short-lived, for native streaming downloads of
    # large files (DB backups, model artifacts) where fetch()+blob() would
    # otherwise buffer the whole payload in browser memory.
    _DOWNLOAD_TOKEN_TTL = 120

    @app.get("/api/download-token")
    async def issue_download_token(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        return {"token": _sign_token(user, ttl=_DOWNLOAD_TOKEN_TTL),
                "expires_in": _DOWNLOAD_TOKEN_TTL}

    # CSRF token — one per process, sent to client on login
    _csrf_token = secrets.token_hex(32)

    # Login endpoint — issues session token + CSRF token
    @app.post("/api/auth/login")
    async def login(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Authenticate with password and receive a session token."""
        ip = _client_ip(request)
        wait = _throttle.locked_for(ip)
        if wait > 0:
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts. Try again later.",
                headers={"Retry-After": str(int(wait) + 1)},
            )
        pw = body.get("password", "")
        if not secrets.compare_digest(pw, _password["current"]):
            _throttle.record_failure(ip)
            raise HTTPException(status_code=401, detail="Invalid password")
        _throttle.record_success(ip)
        username = body.get("username", "admin")
        token = _sign_token(username)
        return {
            "token": token,
            "csrf_token": _csrf_token,
            "expires_in": _TOKEN_TTL_SEC,
        }


    # ------------------------------------------------------------------
    # Route modules — registration order preserved from the original
    # monolith (matters for static-vs-param path precedence).
    # ------------------------------------------------------------------
    deps = Deps(
        verify_credentials=verify_credentials,
        verify_download_credentials=verify_download_credentials,
        password=_password,
    )
    portfolio.register(app, ctx, deps)
    positions.register(app, ctx, deps)
    holdings.register(app, ctx, deps)
    trades.register(app, ctx, deps)
    analytics.register(app, ctx, deps)
    watchlist.register(app, ctx, deps)
    system.register(app, ctx, deps)
    integrations.register(app, ctx, deps)
    market.register(app, ctx, deps)
    models.register(app, ctx, deps)
    predictions.register(app, ctx, deps)
    symbols.register(app, ctx, deps)
    simulator.register(app, ctx, deps)
    data_mgmt.register(app, ctx, deps)
    dryrun.register(app, ctx, deps)
    quarantine.register(app, ctx, deps)
    trading_controls.register(app, ctx, deps)
    config_api.register(app, ctx, deps)
    skills_api.register(app, ctx, deps)
    ws_module.register(app, ctx, deps)


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
