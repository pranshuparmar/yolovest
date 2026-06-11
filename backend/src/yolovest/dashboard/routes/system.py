"""Health, audit, logs, drift/slippage analytics, system state.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    Query,
)

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

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

        # Surface the cached CDSL TPIN status so the dashboard can
        # render a "Authorise CDSL" banner without an extra round
        # trip. Cache is populated by the cdsl-auth-check CRON skill
        # at market open and by manual refresh from the banner.
        cdsl_auth: dict[str, Any] | None = None
        try:
            import json as _json
            raw = await ctx.db.get_system_state("cdsl_auth_status")
            if raw:
                cdsl_auth = _json.loads(raw)
        except Exception:
            logger.debug("Failed to read cached cdsl_auth_status", exc_info=True)

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
            "cdsl_auth": cdsl_auth,
        }

