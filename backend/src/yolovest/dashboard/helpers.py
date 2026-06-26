"""Pure dashboard helpers: broker-margin extraction, capital math,
CDSL TPIN handling, market-scan scoring. No FastAPI dependencies
beyond response shapes."""

import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _safe_path_in(base_dir: str | Path, *segments: str) -> Path:
    """Join user-supplied ``segments`` under ``base_dir`` and confirm the
    result stays inside it — rejecting ``../`` traversal and absolute-path
    injection. Returns the resolved ``Path``; raises ``HTTPException(400)``
    on any escape.

    Containment is checked with ``os.path.realpath`` + ``os.path.commonpath``
    (the form CodeQL models as a path-injection *barrier*). A pathlib
    ``resolve()`` / ``relative_to()`` guard is equivalent at runtime but isn't
    recognised as a sanitizer, so sinks fed through it stay flagged.
    """
    root = os.path.realpath(str(base_dir))
    target = os.path.realpath(os.path.join(root, *segments))
    if root != target and os.path.commonpath((root, target)) != root:
        raise HTTPException(status_code=400, detail="Invalid path")
    return Path(target)


def _to_base_date(raw: Any) -> date | None:
    """Best-effort parse of a signal's base date.

    Accepts a date, a 'YYYY-MM-DD' string, or a full ISO/SQLite UTC
    timestamp (which is converted to its IST calendar date). Returns
    None when nothing parseable is supplied.
    """
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    s = str(raw).strip()
    if not s:
        return None
    # Plain date prefix.
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        # If there's a time component, treat the stamp as UTC and convert
        # to the IST calendar date (signals store UTC via datetime('now')).
        if len(s) > 10:
            from yolovest.timezone import IST
            try:
                ts = datetime.fromisoformat(s.replace(" ", "T"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                return ts.astimezone(IST).date()
            except ValueError:
                pass
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def compute_signal_economics(
    ctx: Any,
    *,
    signal_type: Any,
    entry_price: Any,
    target_price: Any,
    stop_loss_price: Any,
    position_size: Any,
    product: Any,
    base_date: Any,
    expected_holding_days: Any,
) -> dict[str, Any]:
    """Derive display economics shared by the recommendations + dry-run views.

    Returns a dict with:
      - target_date:    base_date + expected_holding_days trading days
                        ('YYYY-MM-DD'), or None when undeterminable.
      - estimated_costs: round-trip transaction costs at the target.
      - est_net_gain:    net P&L if the target hits, after all costs.
      - est_net_loss:    net P&L if the SL hits, after all costs (<= 0).

    All fields are best-effort; any that can't be computed come back None
    instead of raising, so a malformed row never breaks the endpoint.
    """
    from yolovest.costs import compute_transaction_costs

    out: dict[str, Any] = {
        "target_date": None,
        "estimated_costs": None,
        "est_net_gain": None,
        "est_net_loss": None,
    }

    # --- Target / predicted-exit date ---
    bd = _to_base_date(base_date)
    try:
        horizon = int(expected_holding_days) if expected_holding_days is not None else None
    except (TypeError, ValueError):
        horizon = None
    if bd is not None and horizon is not None:
        try:
            out["target_date"] = ctx.market_hours.add_trading_days(
                bd, max(0, horizon),
            ).isoformat()
        except Exception:
            logger.debug("target-date computation failed", exc_info=True)

    # --- Net gain / loss after costs ---
    try:
        entry = float(entry_price)
        target = float(target_price)
        sl = float(stop_loss_price)
        qty = int(position_size) if position_size is not None else 0
    except (TypeError, ValueError):
        return out
    if entry <= 0 or target <= 0 or sl <= 0 or qty <= 0:
        return out

    prod = product if product in ("MIS", "CNC") else "MIS"
    direction = 1 if str(signal_type).upper() == "BUY" else -1
    gross_win = (target - entry) * direction * qty
    gross_loss = (entry - sl) * direction * qty  # positive = rupees at risk
    # STT lands on the sell leg — flip entry/exit for SELL so the cost model
    # taxes the correct side.
    if direction > 0:
        costs = compute_transaction_costs(
            entry, target, qty, product=prod,
            cost_config=ctx.config.transaction_costs,
        )
    else:
        costs = compute_transaction_costs(
            target, entry, qty, product=prod,
            cost_config=ctx.config.transaction_costs,
        )
    out["estimated_costs"] = round(costs, 2)
    out["est_net_gain"] = round(gross_win - costs, 2)
    out["est_net_loss"] = round(-(gross_loss + costs), 2)
    return out


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


def _compute_cdsl_status(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """Inspect Kite holdings to figure out CDSL TPIN auth state.

    Per Kite's holdings schema:
      - quantity            = total qty held
      - t1_quantity         = subset still in T+1 (can't be sold today)
      - authorised_quantity = qty already authorised for sale today
                              (via DDPI or daily CDSL TPIN)
      - authorised_date     = "0001-01-01 ..." when never authorised

    Deliverable today  = quantity - t1_quantity
    Needs CDSL auth    = deliverable - authorised_quantity > 0

    DDPI users see authorised_quantity == quantity for every row at
    every check, so this returns needs_auth=False without ever
    nagging them.

    Empty holdings → needs_auth=False (nothing to sell).

    NOTE: `needs_auth` here is "are there unauthorised holdings?",
    not "should we alert the user?". The alert gate is computed
    separately by `_compute_cdsl_alert_gate` so users who hold
    long-term shares with no system exits aren't pinged daily.
    """
    total_holdings = 0
    total_deliverable = 0
    total_authorised = 0
    pending_symbols: list[dict[str, Any]] = []

    for h in holdings or []:
        qty = int(h.get("quantity") or h.get("opening_quantity") or 0)
        if qty <= 0:
            continue
        t1 = int(h.get("t1_quantity") or 0)
        deliverable = max(0, qty - t1)
        if deliverable <= 0:
            continue
        authorised = int(h.get("authorised_quantity") or 0)
        unauth = max(0, deliverable - authorised)
        total_holdings += qty
        total_deliverable += deliverable
        total_authorised += min(authorised, deliverable)
        if unauth > 0:
            pending_symbols.append({
                "symbol": h.get("tradingsymbol"),
                "isin": h.get("isin"),
                "deliverable_qty": deliverable,
                "authorised_qty": authorised,
                "pending_qty": unauth,
            })

    needs_auth = total_authorised < total_deliverable
    return {
        "needs_auth": needs_auth,
        "total_holdings": total_holdings,
        "deliverable_qty": total_deliverable,
        "authorised_qty": total_authorised,
        "pending_qty": max(0, total_deliverable - total_authorised),
        "pending_count": len(pending_symbols),
        "pending_symbols": pending_symbols,
        # When the user has holdings but nothing is pending auth, the
        # most likely explanation is DDPI is set up. We can't be 100%
        # certain (could also mean they auth'd earlier today) but this
        # is the right hint to suppress the banner once a day after
        # they auth.
        "ddpi_likely_enabled": (
            total_deliverable > 0 and total_authorised >= total_deliverable
        ),
    }


async def _compute_cdsl_alert_gate(ctx: Any) -> dict[str, Any]:
    """Decide whether a CDSL alert should actually fire.

    Just having unauthorised holdings isn't enough — a long-term
    investor who never sells shouldn't be nagged daily. We only
    alert when something the system manages could try to place a
    delivery (CNC) sell today:

      - Open system-managed CNC position (could exit on target/SL).
      - Active GTT at the broker (could fire on price crossing).
      - Pending CNC SELL in the manual-approval queue.

    Returns a dict the caller merges into the cdsl_status snapshot:
        {
            "has_active_cnc_exits": bool,
            "active_cnc_positions": int,
            "active_gtts": int,
            "pending_cnc_sells": int,
        }
    """
    active_positions = 0
    active_gtts = 0
    pending_cnc_sells = 0

    try:
        positions = await ctx.db.get_open_positions(mode=ctx.config.mode)
        active_positions = sum(
            1 for p in positions
            if (p.get("product") or "").upper() == "CNC"
            and (p.get("origin") or "system") == "system"
        )
    except Exception:
        logger.debug("cdsl-gate: get_open_positions failed", exc_info=True)

    try:
        if hasattr(ctx.broker, "get_gtts") and await ctx.broker.is_authenticated():
            gtts = await ctx.broker.get_gtts() or []
            # Only count GTTs that aren't already terminal — Kite
            # keeps cancelled/triggered ones in the list for a while.
            active_gtts = sum(
                1 for g in gtts
                if str(g.get("status") or "").lower() == "active"
            )
    except Exception:
        logger.debug("cdsl-gate: get_gtts failed", exc_info=True)

    try:
        pending = await ctx.db.get_pending_trades()
        pending_cnc_sells = sum(
            1 for p in pending
            if (p.get("product") or "").upper() == "CNC"
            and (p.get("signal_type") or "").upper() == "SELL"
        )
    except Exception:
        logger.debug("cdsl-gate: get_pending_trades failed", exc_info=True)

    return {
        "has_active_cnc_exits": (
            active_positions > 0 or active_gtts > 0 or pending_cnc_sells > 0
        ),
        "active_cnc_positions": active_positions,
        "active_gtts": active_gtts,
        "pending_cnc_sells": pending_cnc_sells,
    }


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


async def _compute_total_capital(broker: Any) -> float:
    """Backward-compat wrapper. Returns the total of the breakdown."""
    bd = await _compute_capital_breakdown(broker)
    return bd["total"]


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



# CDSL TPIN authorisation is a Zerodha-side daily requirement for
# selling delivery (CNC) holdings unless the user has DDPI set up.
# The first sell of the day gets rejected with a message like
# "X shares need to be authorised at CDSL". We intercept that
# specific error and either programmatically kick off the auth
# flow (newer kiteconnect clients) or surface a static help URL.
_CDSL_HELP_URL = "https://kite.zerodha.com/#holdings"
_CDSL_DDPI_URL = "https://zerodha.com/cdsl-tpin/"

def _is_cdsl_tpin_error(msg: str) -> bool:
    msg_lower = (msg or "").lower()
    return (
        "cdsl" in msg_lower
        or "authoris" in msg_lower  # matches both "authorise" + "authorisation"
        or "tpin" in msg_lower
    )

async def _build_cdsl_response(
    ctx: Any,
    error_msg: str,
    *,
    triggered_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured error response for the CDSL TPIN case.

    Tries to call broker.initiate_holdings_auth so the UI can open
    a Kite session URL directly into the authorisation flow.
    Falls back to the static Kite holdings page when the
    kiteconnect library version doesn't expose the method.

    When `triggered_by` is provided (= a real sell order just
    failed, not a proactive button click), also pings Telegram so
    the user sees the same alert outside the UI. Per-symbol-per-day
    dedup via system_state keeps repeated clicks on the same
    failing order from spamming the channel.
    """
    auth_url: str | None = None
    request_id: str | None = None
    try:
        kite_holdings = await ctx.broker.get_holdings() or []
        # Kite's initiate_holdings_auth wants [{isin, quantity}].
        payload = [
            {
                "isin": h.get("isin"),
                "quantity": int(h.get("quantity") or 0),
            }
            for h in kite_holdings
            if h.get("isin") and (h.get("quantity") or 0) > 0
        ]
        auth = await ctx.broker.initiate_holdings_auth(
            holdings=payload or None,
        )
        if isinstance(auth, dict):
            auth_url = auth.get("redirect_url") or None
            request_id = auth.get("request_id") or None
    except Exception:
        logger.debug(
            "initiate_holdings_auth failed — using static URL",
            exc_info=True,
        )

    resolved_url = auth_url or _CDSL_HELP_URL

    # Fire Telegram alert only on reactive failures (real sell got
    # rejected). The proactive endpoint doesn't pass triggered_by
    # because the user is already engaged with the UI.
    if triggered_by:
        try:
            from yolovest.timezone import now_ist as _now_ist
            sym = (triggered_by.get("symbol") or "?").upper()
            today = _now_ist().date().isoformat()
            dedup_key = f"cdsl_alert_sent:{today}:{sym}"
            already = await ctx.db.get_system_state(dedup_key)
            if not already:
                qty = triggered_by.get("quantity")
                side = (triggered_by.get("side") or "").upper()
                src = triggered_by.get("source") or "order"
                qty_blurb = f" ({side} x{qty})" if qty else ""
                msg = (
                    f"⚠ CDSL TPIN required — {sym}{qty_blurb} sell rejected\n\n"
                    f"Source: {src}\n"
                    f"{error_msg}\n\n"
                    f"Authorise: {resolved_url}\n"
                    f"Skip daily TPIN (DDPI): {_CDSL_DDPI_URL}"
                )
                await ctx.notify.send(msg, alert_type="errors")
                await ctx.db.set_system_state(dedup_key, "1")
        except Exception:
            logger.debug(
                "CDSL reactive Telegram alert failed", exc_info=True,
            )

    return {
        "success": False,
        # Fixed, user-facing message — the UI keys off error_type / auth_url to
        # render the CDSL authorise button, so the raw broker string isn't
        # needed in the HTTP response (it still rides the Telegram alert above).
        "error": "CDSL TPIN authorisation required before this holding can be sold.",
        "error_type": "cdsl_tpin_required",
        "auth_url": resolved_url,
        "auth_url_static": auth_url is None,
        "request_id": request_id,
        "ddpi_help_url": _CDSL_DDPI_URL,
        "hint": (
            "CDSL TPIN authorisation is required to sell delivery (CNC) "
            "holdings. Open the auth URL, complete TPIN, then retry. "
            "For a permanent fix (no daily TPIN), set up DDPI via "
            "the DDPI link."
        ),
    }


def _model_dir(ctx: Any) -> str:
    """Model artifact directory from config (shared by models + backup routes)."""
    return getattr(ctx.config.strategy, "model_dir", "./models")
