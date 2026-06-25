"""Pending-trade approval, kill switch, drift suspension, risk gates, manual trade, password/config admin.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)

from yolovest.dashboard.security import (
    DEFAULT_DASHBOARD_PASSWORD,
    MIN_PASSWORD_LENGTH,
)
from yolovest.dashboard.ws import broadcast_ws

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials
    _password = deps.password

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

    @app.get("/api/drift-suspension")
    async def get_drift_suspension(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Return the current drift-watch suspension state. When non-
        empty, generate-signals is paused until either a successful
        model-retrain clears it or POST /api/drift-suspension with
        empty body clears it manually."""
        try:
            reason = await ctx.db.get_system_state("signal_gen_suspended_by_drift")
        except Exception:
            reason = None
        return {
            "suspended": bool(reason),
            "reason": reason or None,
        }

    @app.delete("/api/drift-suspension")
    async def clear_drift_suspension(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Manually clear the drift-watch suspension flag without
        running a retrain. Useful when the user inspected the drift,
        decided it was a transient bad week, and wants to resume
        signal generation immediately."""
        try:
            await ctx.db.set_system_state("signal_gen_suspended_by_drift", "")
            return {"success": True, "suspended": False}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.get("/api/risk-gates")
    async def get_risk_gates(
        _user: str = Depends(verify_credentials),
    ) -> dict[str, Any]:
        """Consolidated status of the opt-in risk gates so the
        dashboard can show in one round-trip what's currently
        blocking / constraining trades:

        - drift suspension (active + reason)
        - portfolio beta vs cap (current beta-weighted exposure)
        - earnings blackout (open-position / watchlist symbols with a
          scheduled earnings event inside the blackout window)
        """
        cfg = ctx.config.risk
        mode = ctx.config.mode

        # --- Drift suspension ---
        try:
            drift_reason = await ctx.db.get_system_state(
                "signal_gen_suspended_by_drift",
            )
        except Exception:
            drift_reason = None
        drift = {
            "enabled": cfg.drift_auto_suspend_enabled,
            "suspended": bool(drift_reason),
            "reason": drift_reason or None,
        }

        positions = await ctx.db.get_open_positions(mode=mode)
        capital = 0.0
        try:
            portfolio = await ctx.db.get_portfolio_state(mode=mode)
            capital = float(portfolio.get("total_capital") or 0)
        except Exception:
            logger.debug("risk-gates: portfolio state fetch failed", exc_info=True)

        # --- Portfolio beta ---
        beta_cap_value = capital * cfg.max_portfolio_beta if cfg.max_portfolio_beta > 0 else 0.0
        beta_weighted = 0.0
        per_symbol_beta: list[dict[str, Any]] = []
        if cfg.max_portfolio_beta > 0:
            for p in positions:
                sym = p.get("symbol")
                qty = float(p.get("quantity") or 0)
                entry = float(p.get("fill_price") or p.get("entry_price") or 0)
                if not sym or qty <= 0 or entry <= 0:
                    continue
                try:
                    beta = await ctx.db.compute_symbol_beta(sym)
                except Exception:
                    beta = None
                eff_beta = beta if beta is not None else 1.0
                notional = qty * entry
                contribution = notional * abs(eff_beta)
                beta_weighted += contribution
                per_symbol_beta.append({
                    "symbol": sym,
                    "beta": round(eff_beta, 2),
                    "notional": round(notional, 0),
                    "beta_weighted": round(contribution, 0),
                    "estimated": beta is None,
                })
        beta_panel: dict[str, Any] = {
            "enabled": cfg.max_portfolio_beta > 0,
            "cap_multiple": cfg.max_portfolio_beta,
            "cap_value": round(beta_cap_value, 0),
            "current_beta_weighted": round(beta_weighted, 0),
            "utilization_pct": (
                round(beta_weighted / beta_cap_value * 100, 1)
                if beta_cap_value > 0 else 0.0
            ),
            "positions": sorted(
                per_symbol_beta, key=lambda x: x["beta_weighted"], reverse=True,
            ),
        }

        # --- Earnings blackout ---
        blackout_symbols: list[dict[str, Any]] = []
        if cfg.earnings_blackout_days > 0:
            # Check open positions + algo/user watchlist symbols.
            candidate_syms: set[str] = {
                (p.get("symbol") or "").upper() for p in positions if p.get("symbol")
            }
            try:
                wl = await ctx.db.get_combined_watchlist()
                candidate_syms |= {
                    (w.get("symbol") or "").upper() for w in wl if w.get("symbol")
                }
            except Exception:
                logger.debug("risk-gates: watchlist fetch failed", exc_info=True)
            for sym in sorted(candidate_syms):
                try:
                    events = await ctx.db.get_earnings_events(
                        symbol=sym, days=cfg.earnings_blackout_days,
                    )
                except Exception:
                    events = []
                if events:
                    ev = events[0]
                    blackout_symbols.append({
                        "symbol": sym,
                        "event_date": ev.get("event_date"),
                        "title": ev.get("title"),
                        "held": sym in {
                            (p.get("symbol") or "").upper() for p in positions
                        },
                    })
        earnings = {
            "enabled": cfg.earnings_blackout_days > 0,
            "window_days": cfg.earnings_blackout_days,
            "blocked_symbols": blackout_symbols,
        }

        return {"drift": drift, "beta": beta_panel, "earnings": earnings}

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

        # Validate types/ranges BEFORE this reaches the broker — a manual
        # trade with an inverted SL (e.g. SL above entry on a BUY) would
        # otherwise be placed live and stop out immediately.
        try:
            entry = float(body["entry_price"])
            target = float(body["target_price"])
            sl = float(body["stop_loss_price"])
        except (TypeError, ValueError):
            raise HTTPException(400, "entry/target/stop_loss prices must be numbers") from None
        if not (entry > 0 and target > 0 and sl > 0):
            raise HTTPException(400, "entry/target/stop_loss prices must be positive")
        side = str(body["signal_type"]).upper()
        if side not in ("BUY", "SELL"):
            raise HTTPException(400, "signal_type must be BUY or SELL")
        body["signal_type"] = side
        if side == "BUY" and not (sl < entry < target):
            raise HTTPException(400, "For BUY, require stop_loss < entry < target")
        if side == "SELL" and not (target < entry < sl):
            raise HTTPException(400, "For SELL, require target < entry < stop_loss")
        qty = body.get("position_size", body.get("quantity"))
        if qty is not None:
            try:
                if int(qty) <= 0:
                    raise HTTPException(400, "quantity must be a positive integer")
            except (TypeError, ValueError):
                raise HTTPException(400, "quantity must be an integer") from None

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
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
            )
        if new_password == DEFAULT_DASHBOARD_PASSWORD:
            raise HTTPException(
                status_code=400,
                detail="Choose a password other than the shipped default",
            )
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
            raise HTTPException(status_code=500, detail=f"Reload failed: {e}") from e
