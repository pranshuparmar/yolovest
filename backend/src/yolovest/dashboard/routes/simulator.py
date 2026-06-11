"""Risk-parameter replay simulator.

Moved verbatim out of app.py's create_app; endpoints close over
(app, ctx, deps) supplied by register().
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
)

if TYPE_CHECKING:
    from yolovest.context import AppContext
    from yolovest.dashboard.deps import Deps

logger = logging.getLogger(__name__)


def register(app: "FastAPI", ctx: "AppContext", deps: "Deps") -> None:
    verify_credentials = deps.verify_credentials

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

