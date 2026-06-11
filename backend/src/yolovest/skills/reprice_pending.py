"""Skill: reprice-pending-trades — re-anchor still-pending levels to LTP.

For each row in `pending_trades` that the user has not overridden:

  * Expire when LTP has already crossed the queued target or SL, OR when
    drift from queued entry exceeds `execution.price_drift_max_pct`, OR
    when the reanchored geometry would fail the `risk.min_net_rr`
    cost-adjusted reward:risk gate.
  * Adjust entry / target / SL by `LTP − old_entry` so the same
    ATR-distance geometry sits around the new entry.
  * Skip when drift is below `_REPRICE_MIN_ADJUSTMENT_PCT` (noise),
    when `is_manual=1`, when the user set `user_entry_price`, or when
    LTP fetch failed.

Each batch of changes emits `pending_repriced` / `pending_expired`
WebSocket events plus a single batched Telegram alert.

Runs every heartbeat between the expire-pending sweep and health-check
(see `HeartbeatOrchestrator._execute_pipeline`); exposed as a manual
skill so the user can also reprice on demand from Telegram `/run
reprice-pending-trades` or the dashboard Skills page.
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


_REPRICE_MIN_ADJUSTMENT_PCT = 0.001


class RepricePendingSkill(SkillBase):
    name = "reprice-pending-trades"
    description = (
        "Re-anchor still-pending manual-approval trades to the latest LTP. "
        "Shifts entry/target/SL by the LTP-vs-entry delta, or expires the "
        "row when LTP has already crossed target/SL, drifted beyond "
        "`execution.price_drift_max_pct`, or the reanchored levels would "
        "fail `risk.min_net_rr`. Also runs every heartbeat."
    )
    trigger = SkillTrigger.MANUAL
    schedule = None

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        try:
            pending = await self.ctx.db.get_pending_trades()
        except Exception as e:
            return SkillResult(
                success=False, skill_name=self.name,
                error=f"get_pending_trades failed: {e}",
            )
        if not pending:
            return SkillResult(
                success=True, skill_name=self.name,
                data={"adjusted_count": 0, "expired_count": 0,
                      "pending_count": 0},
            )

        drift_max = float(self.ctx.config.execution.price_drift_max_pct)

        adjusted: list[dict[str, Any]] = []
        expired: list[dict[str, Any]] = []

        for row in pending:
            if row.get("is_manual") or row.get("user_entry_price"):
                continue

            symbol = row.get("symbol")
            sig = (row.get("signal_type") or "BUY").upper()
            old_entry = float(row.get("entry_price") or 0)
            old_target = float(row.get("target_price") or 0)
            old_sl = float(row.get("stop_loss_price") or 0)
            if not symbol or old_entry <= 0 or old_target <= 0 or old_sl <= 0:
                continue

            try:
                ltp = await self.ctx.market_data.get_ltp(symbol)
            except Exception:
                logger.debug(
                    "reprice: LTP fetch failed for %s", symbol, exc_info=True,
                )
                continue
            if not ltp or ltp <= 0:
                continue

            already_at_target = (
                (sig == "BUY" and ltp >= old_target)
                or (sig == "SELL" and ltp <= old_target)
            )
            already_at_sl = (
                (sig == "BUY" and ltp <= old_sl)
                or (sig == "SELL" and ltp >= old_sl)
            )
            drift_pct = (ltp - old_entry) / old_entry

            reason: str | None = None
            if already_at_target:
                reason = (
                    f"LTP ₹{ltp:.2f} reached target ₹{old_target:.2f} "
                    "before approval"
                )
            elif already_at_sl:
                reason = (
                    f"LTP ₹{ltp:.2f} hit SL ₹{old_sl:.2f} before approval"
                )
            elif abs(drift_pct) > drift_max:
                reason = (
                    f"LTP ₹{ltp:.2f} drifted {drift_pct * 100:+.2f}% from "
                    f"queued entry ₹{old_entry:.2f} (max {drift_max * 100:.2f}%)"
                )

            if reason:
                try:
                    if await self.ctx.db.expire_pending_trade(row["id"], reason):
                        expired.append({
                            "id": row["id"], "symbol": symbol, "signal_type": sig,
                            "entry_price": old_entry, "ltp": ltp, "reason": reason,
                        })
                except Exception:
                    logger.debug(
                        "reprice: expire_pending_trade failed for %s", symbol,
                        exc_info=True,
                    )
                continue

            if abs(drift_pct) < _REPRICE_MIN_ADJUSTMENT_PCT:
                continue
            delta = ltp - old_entry
            new_target = round(old_target + delta, 2)
            new_sl = round(old_sl + delta, 2)
            new_entry = round(ltp, 2)

            min_net_rr = float(getattr(self.ctx.config.risk, "min_net_rr", 0))
            if min_net_rr > 0:
                from yolovest.costs import evaluate_net_rr
                product = row.get("product", "MIS")
                qty = int(row.get("position_size") or 0)
                net_rr, _costs, fail_reason = evaluate_net_rr(
                    signal_type=sig,
                    entry_price=new_entry,
                    target_price=new_target,
                    stop_loss_price=new_sl,
                    quantity=qty,
                    product=product,
                    cost_config=getattr(self.ctx.config, "transaction_costs", None),
                )
                if fail_reason is not None or (
                    net_rr is not None and net_rr < min_net_rr
                ):
                    rr_str = f"{net_rr:.2f}" if net_rr is not None else "n/a"
                    reason = (
                        f"Reanchored R:R {rr_str} < {min_net_rr:.2f} "
                        f"({fail_reason})" if fail_reason
                        else f"Reanchored R:R {rr_str} < {min_net_rr:.2f}"
                    )
                    try:
                        if await self.ctx.db.expire_pending_trade(
                            row["id"], reason,
                        ):
                            expired.append({
                                "id": row["id"], "symbol": symbol,
                                "signal_type": sig,
                                "entry_price": old_entry, "ltp": ltp,
                                "reason": reason,
                            })
                    except Exception:
                        logger.debug(
                            "reprice: expire on R:R fail for %s", symbol,
                            exc_info=True,
                        )
                    continue

            try:
                ok = await self.ctx.db.update_pending_trade_levels(
                    row["id"],
                    entry_price=new_entry,
                    target_price=new_target,
                    stop_loss_price=new_sl,
                )
            except Exception:
                logger.debug(
                    "reprice: update_pending_trade_levels failed for %s",
                    symbol, exc_info=True,
                )
                continue
            if not ok:
                continue
            adjusted.append({
                "id": row["id"], "symbol": symbol, "signal_type": sig,
                "old_entry": old_entry, "new_entry": new_entry,
                "old_target": old_target, "new_target": new_target,
                "old_sl": old_sl, "new_sl": new_sl,
                "drift_pct": drift_pct,
            })

        if adjusted:
            await self.broadcast("pending_repriced", {"trades": adjusted})
            logger.info(
                "reprice-pending-trades: repriced %d row(s): %s",
                len(adjusted),
                ", ".join(
                    f"{a['symbol']}({a['old_entry']:.2f}→{a['new_entry']:.2f})"
                    for a in adjusted
                ),
            )
        if expired:
            await self.broadcast("pending_expired", {
                "count": len(expired), "trades": expired,
            })
            logger.info(
                "reprice-pending-trades: auto-expired %d row(s) on drift: %s",
                len(expired),
                ", ".join(f"{e['symbol']} ({e['reason']})" for e in expired),
            )

        if adjusted or expired:
            try:
                lines: list[str] = []
                if expired:
                    lines.append(
                        f"⏱ {len(expired)} pending trade(s) expired (price drift):"
                    )
                    for exp in expired:
                        lines.append(
                            f"  • {exp['signal_type']} {exp['symbol']}: {exp['reason']}"
                        )
                if adjusted:
                    if lines:
                        lines.append("")
                    lines.append(
                        f"✎ {len(adjusted)} pending trade(s) repriced to LTP:"
                    )
                    for a in adjusted:
                        lines.append(
                            f"  • {a['signal_type']} {a['symbol']}: "
                            f"entry ₹{a['old_entry']:.2f}→₹{a['new_entry']:.2f}, "
                            f"target ₹{a['old_target']:.2f}→₹{a['new_target']:.2f}, "
                            f"SL ₹{a['old_sl']:.2f}→₹{a['new_sl']:.2f}"
                        )
                if lines:
                    await self.ctx.notify.send("\n".join(lines))
            except Exception:
                logger.debug("reprice: notify failed", exc_info=True)

        return SkillResult(
            success=True, skill_name=self.name,
            data={
                "adjusted_count": len(adjusted),
                "expired_count": len(expired),
                "pending_count": len(pending),
            },
        )
