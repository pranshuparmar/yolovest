"""FastAPI dashboard for YoloVest.

Covers: FR-8.1 (portfolio overview), FR-8.2 (WebSocket), FR-8.3 (trade detail),
FR-8.7 (historical reports), FR-8.9 (basic auth).

All endpoints read from the shared database via AppContext.
"""

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from yolovest.context import AppContext

logger = logging.getLogger(__name__)

security = HTTPBasic()

# WebSocket connection manager
_ws_clients: set[WebSocket] = set()


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

    # Store context for dependency injection
    app.state.ctx = ctx

    # Auth config
    dash_password = (
        ctx.config.dashboard.password
        if hasattr(ctx.config.dashboard, "password")
        else "yolovest"
    )

    def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:  # noqa: B008
        """FR-8.9: Basic password protection."""
        correct = secrets.compare_digest(credentials.password, dash_password)
        if not correct:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    # ------------------------------------------------------------------
    # FR-8.1: Portfolio Overview
    # ------------------------------------------------------------------

    @app.get("/api/portfolio")
    async def get_portfolio(user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Portfolio overview: capital, exposure, open positions, PnL."""
        portfolio = await ctx.db.get_portfolio_state()
        return portfolio

    @app.get("/api/positions")
    async def get_positions(user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """Current open positions."""
        return await ctx.db.get_open_positions()

    @app.get("/api/trades/today")
    async def get_todays_trades(user: str = Depends(verify_credentials)) -> list[dict[str, Any]]:
        """Today's trades."""
        return await ctx.db.get_todays_trades()

    @app.get("/api/trades")
    async def get_trades(
        start: str | None = Query(None, description="Start date YYYY-MM-DD"),
        end: str | None = Query(None, description="End date YYYY-MM-DD"),
        symbol: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Trade history with optional date range and symbol filter."""
        return await ctx.db.get_trades_history(
            start_date=start, end_date=end, symbol=symbol, limit=limit
        )

    @app.get("/api/equity-curve")
    async def get_equity_curve(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Daily equity curve data for charting."""
        return await ctx.db.get_equity_curve(days=days)

    # ------------------------------------------------------------------
    # FR-8.3: Trade Detail View
    # ------------------------------------------------------------------

    @app.get("/api/trades/{trade_id}")
    async def get_trade_detail(
        trade_id: str, user: str = Depends(verify_credentials)
    ) -> dict[str, Any]:
        """Full reasoning chain for a trade: signal → risk → LLM → execution → outcome."""
        detail = await ctx.db.get_trade_detail(trade_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Trade not found")
        return detail

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

    # ------------------------------------------------------------------
    # FR-8.7: Historical Reports
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
        """Current watchlist with scores."""
        return await ctx.db.get_watchlist()

    @app.post("/api/watchlist")
    async def add_watchlist_symbol(
        body: dict[str, Any],
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Add a symbol to the watchlist."""
        symbol = body.get("symbol", "").strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol is required")
        sector = body.get("sector")
        ok = await ctx.db.add_watchlist_symbol(symbol, sector)
        return {"success": ok, "symbol": symbol}

    @app.delete("/api/watchlist/{symbol}")
    async def remove_watchlist_symbol(
        symbol: str,
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Remove a symbol from the watchlist."""
        ok = await ctx.db.remove_watchlist_symbol(symbol)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found in watchlist")
        return {"success": True, "symbol": symbol.upper()}

    @app.get("/api/sectors")
    async def get_sector_rotation(user: str = Depends(verify_credentials)) -> dict[str, Any]:
        """Sector rotation analysis."""
        return await ctx.db.get_sector_rotation()

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
        }

    @app.get("/api/slippage")
    async def get_slippage_stats(
        symbol: str | None = Query(None),
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Slippage analysis (FR-6.7)."""
        return await ctx.db.get_slippage_stats(symbol=symbol, days=days)

    @app.get("/api/llm-accuracy")
    async def get_llm_accuracy(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """LLM review accuracy vs actual trade outcomes (FR-7.8)."""
        return await ctx.db.get_llm_review_accuracy(days=days)

    @app.get("/api/audit")
    async def get_audit_log(
        limit: int = Query(50, ge=1, le=500),
        action_type: str | None = Query(None),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Recent audit log entries (FR-8.8)."""
        return await ctx.db.get_audit_log(limit=limit, action_type=action_type)

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
        gemini_configured = bool(getattr(ctx.config.llm, "api_key", ""))
        gemini_ok = False
        if gemini_configured:
            try:
                gemini_ok = await ctx.llm.ping()
            except Exception:
                gemini_ok = False
        results["gemini"] = {
            "configured": gemini_configured,
            "connected": gemini_ok,
            "model": getattr(ctx.config.llm, "model", ""),
        }

        # --- Zerodha Broker ---
        broker_configured = bool(getattr(ctx.config.broker, "api_key", ""))
        broker_authenticated = False
        broker_margins: dict[str, Any] | None = None
        if broker_configured:
            try:
                broker_authenticated = await ctx.broker.is_authenticated()
            except Exception:
                broker_authenticated = False
            if broker_authenticated:
                try:
                    broker_margins = await ctx.broker.get_margins()
                except Exception:
                    pass
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
        bot_token = getattr(telegram_cfg, "bot_token", "") if telegram_cfg else ""
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
            return {"success": False, "error": str(exc)}

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
                try:
                    margins = await ctx.broker.get_margins()
                except Exception:
                    pass
            return {"success": ok, "margins": margins}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

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
    # News Feed & Sentiment
    # ------------------------------------------------------------------

    @app.get("/api/news")
    async def get_news_feed(
        symbol: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Recent news articles with source attribution."""
        articles = await ctx.db.get_news_articles(symbol=symbol, limit=limit)
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
        """ML model information: production models and shadow candidates."""
        result: dict[str, Any] = {"production": {}, "shadow": []}
        for model_type in ["intraday", "swing"]:
            try:
                model = await ctx.db.get_production_model(model_type)
                if model:
                    result["production"][model_type] = model
            except Exception:
                pass
        try:
            shadow_days = getattr(ctx.config, "ml", None)
            days = getattr(shadow_days, "shadow_mode_days", 14) if shadow_days else 14
            result["shadow"] = await ctx.db.get_shadow_models_ready(days)
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------
    # Predictions Detail & Failures
    # ------------------------------------------------------------------

    @app.get("/api/predictions/today")
    async def get_todays_predictions(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Today's predictions with linked symbols and confidence."""
        return await ctx.db.get_todays_predictions()

    @app.get("/api/predictions/unscored")
    async def get_unscored_predictions(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Predictions awaiting scoring (holding period not yet elapsed)."""
        return await ctx.db.get_unscored_predictions()

    @app.get("/api/predictions/outcomes")
    async def get_prediction_outcomes(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Scored prediction outcomes for failure analysis."""
        return await ctx.db.get_prediction_outcomes()

    # ------------------------------------------------------------------
    # Weekly Summary
    # ------------------------------------------------------------------

    @app.get("/api/weekly/trades")
    async def get_weekly_trades(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's trades."""
        return await ctx.db.get_weekly_trades()

    @app.get("/api/weekly/predictions")
    async def get_weekly_predictions(
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """This week's predictions."""
        return await ctx.db.get_weekly_predictions()

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
        portfolio = await ctx.db.get_portfolio_state()
        positions = await ctx.db.get_open_positions()
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
        """System state including kill switch and orchestrator status."""
        kill_switch = await ctx.db.is_kill_switch_active()
        orchestrator_state = await ctx.db.get_system_state("orchestrator")
        return {
            "kill_switch_active": kill_switch,
            "orchestrator": orchestrator_state,
            "mode": ctx.config.mode,
        }

    # ------------------------------------------------------------------
    # Symbol Deep-Dive (Feature #3)
    # ------------------------------------------------------------------

    @app.get("/api/symbol/{symbol}/ohlcv")
    async def get_symbol_ohlcv(
        symbol: str,
        days: int = Query(60, ge=1, le=365),
        interval: str = Query("1d"),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """OHLCV bars for a symbol."""
        bars = await ctx.db.get_ohlcv(symbol.upper(), interval, days)
        return [
            {
                "timestamp": b.timestamp.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]

    @app.get("/api/symbol/{symbol}/trades")
    async def get_symbol_trades(
        symbol: str,
        limit: int = Query(50, ge=1, le=200),
        user: str = Depends(verify_credentials),
    ) -> list[dict[str, Any]]:
        """Trades for a specific symbol."""
        return await ctx.db.get_symbol_trades(symbol.upper(), limit)

    @app.get("/api/symbol/{symbol}/predictions")
    async def get_symbol_predictions(
        symbol: str, user: str = Depends(verify_credentials)
    ) -> list[dict[str, Any]]:
        """Predictions for a specific symbol."""
        return await ctx.db.get_symbol_predictions(symbol.upper())

    # ------------------------------------------------------------------
    # Strategy Performance (Feature #5)
    # ------------------------------------------------------------------

    @app.get("/api/strategy-performance")
    async def get_strategy_performance(
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Aggregate trade performance by signal type, product, sector, time, holding period."""
        return await ctx.db.get_strategy_performance()

    # ------------------------------------------------------------------
    # Execution Quality (Feature #8)
    # ------------------------------------------------------------------

    @app.get("/api/execution-quality")
    async def get_execution_quality(
        days: int = Query(30, ge=1, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Detailed execution quality metrics: slippage by hour/size, fill rate."""
        return await ctx.db.get_execution_quality(days=days)

    # ------------------------------------------------------------------
    # Correlation Data (Feature #7)
    # ------------------------------------------------------------------

    @app.get("/api/correlations")
    async def get_correlations(
        days: int = Query(60, ge=7, le=365),
        user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Correlation matrix for open positions' symbols."""
        positions = await ctx.db.get_open_positions()
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
        """Replay historical signals against modified risk parameters."""
        max_exposure_pct = body.get("max_exposure_pct", ctx.config.risk.max_portfolio_exposure_pct)
        max_single_stock_pct = body.get("max_single_stock_pct", ctx.config.risk.max_single_stock_exposure_pct)
        max_positions = body.get("max_positions", ctx.config.risk.max_open_positions)
        initial_capital = body.get("initial_capital", 100000)

        signals = await ctx.db.get_historical_signals(200)

        # Simple simulation
        capital = float(initial_capital)
        open_pos = 0
        exposure = 0.0
        stock_exposure: dict[str, float] = {}
        trades_taken = 0
        trades_skipped = 0
        total_pnl = 0.0
        wins = 0
        losses = 0
        peak = capital
        max_drawdown = 0.0

        for sig in signals:
            pnl = sig.get("pnl")
            if pnl is None:
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
            },
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
            return {"success": True, "table": table, "rows_deleted": deleted}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # ------------------------------------------------------------------
    # FR-8.2: WebSocket Live Updates
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
