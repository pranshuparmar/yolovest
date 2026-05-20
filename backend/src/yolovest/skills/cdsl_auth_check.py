"""Skill: cdsl-auth-check — proactive CDSL TPIN status alert.

Trigger: CRON — 09:20 IST weekdays (matches the default order_start
so the warning lands at the same time signal-generation could fire
a CNC SELL).

Why this exists: without CDSL TPIN (or DDPI), the first delivery
sell of the day at the broker fails with the
"X shares need to be authorised at CDSL" error. The OrderForm
already catches that error and renders an action panel — but the
user only sees it AFTER trying to sell. This skill flips it to
"warn before".

What it does:
  1. Read holdings via broker.get_holdings().
  2. Sum the deliverable qty vs the authorised_quantity.
  3. When deliverable > authorised (= needs_auth), push a Telegram
     alert with the count + a clickable auth URL.
  4. Persist the snapshot to system_state.cdsl_auth_status so the
     dashboard banner can render without an extra broker call.

DDPI users (authorised_quantity == quantity for all holdings) never
get pinged — the check returns needs_auth=False and the skill exits
silently.

Mode-aware: skipped in paper mode and when the broker isn't
authenticated. The persisted system_state is global; the dashboard
only renders the banner when needs_auth=True.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_utc

logger = logging.getLogger(__name__)


# Helpers duplicated from dashboard.app to keep this skill standalone.
# Tiny enough that splitting into a shared module would be premature.
def _compute_cdsl_status(holdings: list[dict[str, Any]]) -> dict[str, Any]:
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
    return {
        "needs_auth": total_authorised < total_deliverable,
        "total_holdings": total_holdings,
        "deliverable_qty": total_deliverable,
        "authorised_qty": total_authorised,
        "pending_qty": max(0, total_deliverable - total_authorised),
        "pending_count": len(pending_symbols),
        "pending_symbols": pending_symbols,
        "ddpi_likely_enabled": (
            total_deliverable > 0 and total_authorised >= total_deliverable
        ),
    }


class CdslAuthCheckSkill(SkillBase):
    name = "cdsl-auth-check"
    description = (
        "Pre-market CDSL TPIN authorisation check. Alerts Telegram + "
        "dashboard banner when delivery holdings still need daily "
        "authorisation before they can be sold. Skipped for DDPI users."
    )
    trigger = SkillTrigger.CRON
    # 09:20 IST weekdays — the default order_start. Banner lands the
    # moment trading can actually begin so the user has the whole
    # day to authorise.
    schedule = "20 9 * * 1-5"

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        broker = self.ctx.broker
        if self.ctx.config.mode == "paper":
            return SkillResult(
                success=True, skill_name=self.name,
                data={"skipped": True, "reason": "paper_mode"},
            )
        if not await broker.is_authenticated():
            return SkillResult(
                success=True, skill_name=self.name,
                data={"skipped": True, "reason": "broker_not_authenticated"},
            )

        try:
            holdings = await broker.get_holdings()
        except Exception as e:
            logger.exception("cdsl-auth-check: get_holdings failed")
            return SkillResult(
                success=False, skill_name=self.name,
                error=f"get_holdings failed: {e}",
            )

        status = _compute_cdsl_status(holdings or [])

        # Alert gate: only ping when something the system manages could
        # actually try to sell delivery today. A user who's holding
        # long-term shares with no system-managed exits and no GTTs
        # shouldn't be nagged daily about TPIN auth they don't need.
        active_positions = 0
        active_gtts = 0
        pending_cnc_sells = 0
        try:
            positions = await self.ctx.db.get_open_positions(
                mode=self.ctx.config.mode,
            )
            active_positions = sum(
                1 for p in positions
                if (p.get("product") or "").upper() == "CNC"
                and (p.get("origin") or "system") == "system"
            )
        except Exception:
            logger.debug("cdsl-auth-check: positions lookup failed", exc_info=True)
        try:
            if hasattr(broker, "get_gtts"):
                gtts = await broker.get_gtts() or []
                active_gtts = sum(
                    1 for g in gtts
                    if str(g.get("status") or "").lower() == "active"
                )
        except Exception:
            logger.debug("cdsl-auth-check: gtts lookup failed", exc_info=True)
        try:
            pending = await self.ctx.db.get_pending_trades()
            pending_cnc_sells = sum(
                1 for p in pending
                if (p.get("product") or "").upper() == "CNC"
                and (p.get("signal_type") or "").upper() == "SELL"
            )
        except Exception:
            logger.debug("cdsl-auth-check: pending lookup failed", exc_info=True)

        has_active_cnc_exits = (
            active_positions > 0 or active_gtts > 0 or pending_cnc_sells > 0
        )
        alert_needed = status["needs_auth"] and has_active_cnc_exits

        result = {
            "authenticated": True,
            **status,
            "has_active_cnc_exits": has_active_cnc_exits,
            "active_cnc_positions": active_positions,
            "active_gtts": active_gtts,
            "pending_cnc_sells": pending_cnc_sells,
            "alert_needed": alert_needed,
            "checked_at": now_utc().isoformat(),
        }

        # Persist snapshot for the dashboard banner regardless of
        # needs_auth — that way the UI knows when the most recent
        # check ran and can show a "Checked X minutes ago" hint.
        try:
            await self.ctx.db.set_system_state(
                "cdsl_auth_status", _json.dumps(result),
            )
        except Exception:
            logger.debug("cdsl-auth-check: cache write failed", exc_info=True)

        if not alert_needed:
            # Either nothing to authorise, or nothing the system
            # manages might sell today — quiet path.
            reason = (
                "nothing pending auth" if not status["needs_auth"]
                else "no active CNC exits (long-term holding, no GTTs / open trades / pending CNC sells)"
            )
            logger.info(
                "cdsl-auth-check: no alert — %s "
                "(deliverable=%d, authorised=%d, positions=%d, gtts=%d, pending_sells=%d)",
                reason,
                status["deliverable_qty"], status["authorised_qty"],
                active_positions, active_gtts, pending_cnc_sells,
            )
            return SkillResult(
                success=True, skill_name=self.name, data=result,
            )

        # Alert Telegram. Mirror the structure of the auth-broker
        # session-expired alert so users instantly recognise it.
        symbols_blurb = ", ".join(
            f"{s['symbol']}({s['pending_qty']})"
            for s in status["pending_symbols"][:6]
        )
        if status["pending_count"] > 6:
            symbols_blurb += f", +{status['pending_count'] - 6} more"
        # Build a one-line "why we're alerting" so the user understands
        # this isn't a daily blanket nag.
        trigger_bits: list[str] = []
        if active_positions:
            trigger_bits.append(f"{active_positions} open CNC trade(s)")
        if active_gtts:
            trigger_bits.append(f"{active_gtts} active GTT(s)")
        if pending_cnc_sells:
            trigger_bits.append(f"{pending_cnc_sells} pending CNC sell(s)")
        trigger_blurb = " + ".join(trigger_bits) or "scheduled exit activity"

        msg = (
            f"⚠ CDSL TPIN required\n"
            f"{status['pending_qty']} share(s) across {status['pending_count']} "
            f"symbol(s) need authorising before delivery sells can be placed today.\n\n"
            f"Triggered by: {trigger_blurb}\n"
            f"Pending: {symbols_blurb}\n\n"
            f"Open the dashboard banner or Kite Holdings to authorise. "
            f"For a one-time setup that skips daily TPIN, set up DDPI: "
            f"https://zerodha.com/cdsl-tpin/"
        )
        try:
            await self.ctx.notify.send(msg, alert_type="errors")
        except Exception:
            logger.debug("cdsl-auth-check: telegram alert failed", exc_info=True)

        logger.warning(
            "cdsl-auth-check: %d share(s) across %d symbol(s) need TPIN auth",
            status["pending_qty"], status["pending_count"],
        )
        return SkillResult(
            success=True, skill_name=self.name, data=result,
        )
