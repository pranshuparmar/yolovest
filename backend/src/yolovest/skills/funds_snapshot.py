"""Skill: funds-snapshot — daily snapshot of broker funds/margins.

Trigger: CRON — 16:05 IST (after market close, before report-generate
at 16:00 — actually right alongside so daily summary email can pick
up the snapshot if needed).

Why this exists: the user can already look at live funds via the
Funds page, but there's no history. Without a daily snapshot,
reconstructing "did my available cash drop today and where did it
go?" requires pulling Kite's contract notes. This skill captures the
broker.get_margins() payload + a few derived holdings totals once a
day, so the Funds page can render a multi-week trail of cash and
margin movement.

Mode-scoped (paper / live snapshots stored separately). Manually
runnable from the Skills page or `/run funds-snapshot`.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)


def _f(d: dict[str, Any], key: str) -> float:
    try:
        return float(d.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


class FundsSnapshotSkill(SkillBase):
    name = "funds-snapshot"
    description = (
        "Capture today's broker funds/margins snapshot for the funds "
        "movement trail. Runs once daily after market close."
    )
    trigger = SkillTrigger.CRON
    # 16:05 IST weekdays — market closes 15:30, position-monitor /
    # ghost-recovery have settled by 16:00, and report-generate
    # fires at 16:00 too. Five-minute offset avoids racing report.
    schedule = "5 16 * * 1-5"

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        broker = self.ctx.broker
        if not await broker.is_authenticated():
            logger.info("funds-snapshot: broker not authenticated — skipping")
            return SkillResult(
                success=True, skill_name=self.name,
                data={"skipped": True, "reason": "broker_not_authenticated"},
            )

        try:
            raw = await broker.get_margins()
        except Exception as e:
            logger.exception("funds-snapshot: broker.get_margins failed")
            return SkillResult(
                success=False, skill_name=self.name,
                error=f"get_margins failed: {e}",
            )

        equity: dict[str, Any] = (raw or {}).get("equity", {}) or {}
        avail: dict[str, Any] = equity.get("available", {}) or {}
        util: dict[str, Any] = equity.get("utilised", {}) or {}

        summary = {
            "available_cash": _f(avail, "cash"),
            "live_balance": _f(avail, "live_balance"),
            "opening_balance": _f(avail, "opening_balance"),
            "collateral": _f(avail, "collateral"),
            "utilised_margin": _f(util, "debits"),
            "m2m_unrealised": _f(util, "m2m_unrealised"),
            "m2m_realised": _f(util, "m2m_realised"),
            "payout": _f(util, "payout"),
            "exposure": _f(util, "exposure"),
            "span": _f(util, "span"),
            "delivery": _f(util, "delivery"),
            "net": _f(equity, "net"),
        }

        # Holdings totals so the snapshot tells the whole story
        # without a second JOIN at read time.
        holdings_invested = 0.0
        holdings_current = 0.0
        try:
            holdings = await broker.get_holdings() or []
            for h in holdings:
                qty = float(h.get("quantity") or 0)
                avg = float(h.get("average_price") or 0)
                ltp = float(h.get("last_price") or avg or 0)
                holdings_invested += qty * avg
                holdings_current += qty * ltp
        except Exception:
            logger.debug(
                "funds-snapshot: get_holdings failed — recording with 0s",
                exc_info=True,
            )

        snapshot_date = now_ist().date().isoformat()
        mode = self.ctx.config.mode
        try:
            await self.ctx.db.upsert_funds_snapshot(
                snapshot_date=snapshot_date,
                mode=mode,
                summary=summary,
                raw_json=_json.dumps(raw) if raw else None,
                holdings_invested=holdings_invested,
                holdings_current=holdings_current,
            )
        except Exception as e:
            logger.exception("funds-snapshot: db upsert failed")
            return SkillResult(
                success=False, skill_name=self.name,
                error=f"db upsert failed: {e}",
            )

        logger.info(
            "funds-snapshot: %s mode=%s cash=%.2f used=%.2f holdings=%.2f net=%.2f",
            snapshot_date, mode,
            summary["available_cash"], summary["utilised_margin"],
            holdings_current, summary["net"],
        )
        return SkillResult(
            success=True, skill_name=self.name,
            data={
                "snapshot_date": snapshot_date, "mode": mode,
                "summary": summary,
                "holdings_invested": holdings_invested,
                "holdings_current": holdings_current,
            },
        )
