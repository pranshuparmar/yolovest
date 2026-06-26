"""Zerodha / Gemini / Telegram integration status, OAuth callback, signed order postback.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import hashlib
import logging
import secrets
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from yolovest.dashboard.postback import _apply_order_postback
from yolovest.dashboard.ws import broadcast_ws

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

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
        bot_token = (
            _bot_token_raw.get_secret_value()
            if _bot_token_raw is not None and hasattr(_bot_token_raw, "get_secret_value")
            else str(_bot_token_raw or "")
        )
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
            return {"success": False, "error": "Gemini connection test failed"}

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
            ctx.ticker = None
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
            logger.warning("Zerodha authenticate failed: %s", exc)
            return {"success": False, "error": "Authentication failed"}

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
        import json as _json

        raw = await request.body()
        try:
            body = _json.loads(raw or b"{}")
        except (ValueError, TypeError):
            logger.warning("Zerodha postback: invalid JSON body")
            raise HTTPException(status_code=400, detail="invalid body") from None

        order_id = str(body.get("order_id") or "")
        order_timestamp = str(body.get("order_timestamp") or "")
        received_checksum = body.get("checksum") or ""

        api_secret_val = ctx.config.broker.api_secret.get_secret_value() \
            if ctx.config.broker.api_secret else ""
        if not api_secret_val:
            # No broker secret configured (paper-only install): there is
            # nothing to verify a checksum against, so the endpoint is
            # disabled rather than left open to forged order updates that
            # would mutate trade rows via _apply_order_postback.
            raise HTTPException(
                status_code=403,
                detail="postback disabled (broker api_secret not configured)",
            )
        if not (order_id and order_timestamp):
            # Without these fields the checksum can't be recomputed —
            # previously this skipped verification entirely, which let a
            # crafted body bypass the signature check.
            raise HTTPException(
                status_code=400, detail="missing order_id/order_timestamp",
            )
        expected = hashlib.sha256(
            f"{order_id}{order_timestamp}{api_secret_val}".encode(),
        ).hexdigest()
        if not secrets.compare_digest(expected, str(received_checksum)):
            logger.warning(
                "Zerodha postback: checksum mismatch for order=%s "
                "(possibly spoofed) — rejecting", order_id,
            )
            raise HTTPException(status_code=401, detail="invalid checksum")

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
            logger.warning("Telegram test message failed: %s", exc)
            return {"success": False, "error": "Telegram test message failed"}

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
            logger.warning("Telegram send failed: %s", exc)
            return {"success": False, "error": "Telegram message send failed"}

