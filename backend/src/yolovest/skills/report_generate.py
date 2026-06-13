"""Skill: report-generate — Daily and weekly reports + Telegram delivery.

Trigger: CRON — daily at reports.daily_report_time, weekly at reports.weekly_report_cron
Pipeline position: Post-market, after square-off.

Flow:
Daily report:
1. Aggregate today's trades: entries, exits, PnL per trade
2. Compute daily PnL, win rate, avg slippage
3. Prediction accuracy for today's signals
4. Top signals (best confidence scores)
5. Market summary (via Gemini)
6. Store report in DB for dashboard access
7. Send to Telegram (if notifications.telegram.alerts.daily_summary is true)

Weekly report:
1. Cumulative PnL for the week
2. Model performance trends (accuracy over time)
3. Prediction accuracy trends
4. Gemini review analysis: did LLM approvals/rejections help?
5. Best/worst trades of the week
6. Risk metrics: max drawdown, Sharpe for the period
7. Store + send to Telegram (if weekly_summary alert enabled)
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class ReportGenerateSkill(SkillBase):
    name = "report-generate"
    description = "Generate daily/weekly reports and deliver via Telegram"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        # Daily report: convert HH:MM to cron (e.g. "16:00" -> "0 16 * * 1-5")
        daily_time = self.ctx.config.reports.daily_report_time
        try:
            h, m = daily_time.split(":")
            return f"{int(m)} {int(h)} * * 1-5"
        except (ValueError, AttributeError):
            return self.schedule

    def should_run(self) -> bool:
        return not self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        report_type = kwargs.get("type")

        if report_type is None:
            # Auto-detect: run weekly on Friday (last trading day of the week)
            # since the cron schedule only fires Mon-Fri
            from yolovest.timezone import now_ist

            today = now_ist().weekday()
            # Friday = 4 (last weekday the cron fires on)
            report_type = "weekly" if today == 4 else "daily"

        if report_type == "daily":
            return await self._generate_daily()
        else:
            return await self._generate_weekly()

    async def _generate_daily(self) -> SkillResult:
        """Daily report at market close."""
        mode = self.ctx.config.mode
        new_trades = await self.ctx.db.get_todays_trades(mode=mode)
        closed_trades = await self.ctx.db.get_todays_closed_trades(mode=mode)
        predictions_result = await self.ctx.db.get_todays_predictions(mode=mode)
        predictions = predictions_result.get("items", []) if isinstance(predictions_result, dict) else predictions_result

        realized_pnl = sum(t.get("pnl", 0) for t in closed_trades if t.get("pnl") is not None)
        wins = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in closed_trades if (t.get("pnl") or 0) < 0]
        win_rate = len(wins) / len(closed_trades) if closed_trades else 0
        avg_slippage = (
            sum(t.get("slippage", 0) for t in closed_trades) / len(closed_trades)
            if closed_trades else 0
        )

        # Prediction accuracy
        scored = [p for p in predictions if p.get("direction_correct") is not None]
        pred_accuracy = (
            sum(1 for p in scored if p["direction_correct"]) / len(scored)
            if scored
            else None
        )

        # Portfolio snapshot
        portfolio = await self.ctx.db.get_portfolio_state(mode=mode)
        signals_today = await self.ctx.db.get_todays_signals_count()

        # Gemini market summary (best effort, skip if LLM disabled)
        market_summary = None
        if self.ctx.config.llm.enabled:
            try:
                market_summary = await self.ctx.llm.summarize_market_day()
            except Exception as e:
                logger.warning("Market summary generation failed: %s", e)

        report = {
            "type": "daily",
            "new_entries": len(new_trades),
            "exits": len(closed_trades),
            "total_trades": len(new_trades) + len(closed_trades),
            "realized_pnl": realized_pnl,
            "total_pnl": realized_pnl,
            "win_rate": win_rate,
            "wins": len(wins),
            "losses": len(losses),
            "avg_slippage": avg_slippage,
            "prediction_accuracy": pred_accuracy,
            "predictions_scored": len(scored),
            "signals_generated": signals_today,
            "open_positions": portfolio.get("system_positions", 0),
            "adopted_positions": portfolio.get("adopted_positions", 0),
            "portfolio_value": portfolio.get("total_portfolio_value", 0),
            "holdings_current": portfolio.get("holdings_current", 0),
            "holdings_unrealized_pnl": portfolio.get("holdings_unrealized_pnl", 0),
            "available_funds": portfolio.get("available_funds", 0),
            "market_summary": str(market_summary) if market_summary else None,
        }

        await self.ctx.db.store_report(report)

        msg = self._format_daily_report(report)
        await self.ctx.notify.send(msg, alert_type="daily_summary")

        return SkillResult(success=True, skill_name=self.name, data=report)

    async def _generate_weekly(self) -> SkillResult:
        """Weekly cumulative report."""
        mode = self.ctx.config.mode
        trades = await self.ctx.db.get_weekly_trades(mode=mode)
        predictions = await self.ctx.db.get_weekly_predictions(mode=mode)
        llm_reviews = await self.ctx.db.get_weekly_llm_reviews()

        total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        win_rate = len(wins) / len(trades) if trades else 0

        # Best and worst trades
        closed_trades = [t for t in trades if t.get("pnl") is not None]
        best_trade = max(closed_trades, key=lambda t: t.get("pnl", 0)) if closed_trades else None
        worst_trade = min(closed_trades, key=lambda t: t.get("pnl", 0)) if closed_trades else None

        # LLM review accuracy
        llm_approved = [r for r in llm_reviews if r.get("decision") == "APPROVE"]
        llm_rejected = [r for r in llm_reviews if r.get("decision") == "REJECT"]
        llm_approve_pnl = sum(
            r.get("trade_pnl", 0) for r in llm_approved if r.get("trade_pnl") is not None
        )

        # Prediction accuracy
        scored = [p for p in predictions if p.get("direction_correct") is not None]
        pred_accuracy = (
            sum(1 for p in scored if p["direction_correct"]) / len(scored)
            if scored
            else None
        )

        # Prediction scoreboard snapshot
        scoreboard = await self.ctx.db.get_prediction_scoreboard("overall")

        # LLM review accuracy — compare decisions vs outcomes
        llm_accuracy = None
        try:
            llm_accuracy = await self.ctx.db.get_llm_review_accuracy(days=7)
        except Exception as e:
            logger.warning("LLM review accuracy fetch failed: %s", e)

        # Slippage analysis for the week
        slippage_stats = None
        try:
            slippage_stats = await self.ctx.db.get_slippage_stats(days=7)
        except Exception as e:
            logger.warning("Slippage stats fetch failed: %s", e)

        report = {
            "type": "weekly",
            "total_trades": len(trades),
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "best_trade": {
                "symbol": best_trade.get("symbol"),
                "pnl": best_trade.get("pnl"),
            } if best_trade else None,
            "worst_trade": {
                "symbol": worst_trade.get("symbol"),
                "pnl": worst_trade.get("pnl"),
            } if worst_trade else None,
            "llm_approvals": len(llm_approved),
            "llm_rejections": len(llm_rejected),
            "llm_approved_pnl": llm_approve_pnl,
            "llm_review_accuracy": llm_accuracy,
            "prediction_accuracy": pred_accuracy,
            "predictions_scored": len(scored),
            "scoreboard": scoreboard,
            "slippage_stats": slippage_stats,
        }

        await self.ctx.db.store_report(report)

        msg = self._format_weekly_report(report)
        await self.ctx.notify.send(msg, alert_type="weekly_summary")

        return SkillResult(success=True, skill_name=self.name, data=report)

    @staticmethod
    def _format_daily_report(report: dict[str, Any]) -> str:
        """Format daily report for Telegram/console."""
        pnl = report.get("realized_pnl", report.get("total_pnl", 0))
        pnl_sign = "+" if pnl >= 0 else ""
        lines = ["Daily Report", ""]

        # Portfolio snapshot
        portfolio_val = report.get("portfolio_value", 0)
        if portfolio_val > 0:
            lines.append(f"Portfolio: {portfolio_val:,.0f}")
        avail = report.get("available_funds", 0)
        if avail > 0:
            lines.append(f"Available: {avail:,.0f}")
        holdings = report.get("holdings_current", 0)
        h_pnl = report.get("holdings_unrealized_pnl", 0)
        if holdings > 0:
            h_sign = "+" if h_pnl >= 0 else ""
            lines.append(f"Holdings: {holdings:,.0f} ({h_sign}{h_pnl:,.0f})")
        lines.append("")

        # Trading activity
        new_entries = report.get("new_entries", 0)
        exits = report.get("exits", 0)
        open_pos = report.get("open_positions", 0)
        adopted = report.get("adopted_positions", 0)
        signals = report.get("signals_generated", 0)
        lines.append(f"Entries: {new_entries} | Exits: {exits}")
        pos_parts = [f"{open_pos} system"]
        if adopted > 0:
            pos_parts.append(f"{adopted} adopted")
        lines.append(f"Open: {' + '.join(pos_parts)}")
        if signals > 0:
            lines.append(f"Signals: {signals}")
        lines.append("")

        # PnL
        if exits > 0:
            lines.append(
                f"Realized PnL: {pnl_sign}{pnl:,.2f} "
                f"(W:{report.get('wins', 0)} L:{report.get('losses', 0)}, "
                f"{report.get('win_rate', 0):.0%})"
            )
            lines.append(f"Avg Slippage: {report.get('avg_slippage', 0):.4f}")
        else:
            lines.append("No exits today")

        if report.get("prediction_accuracy") is not None:
            lines.append(
                f"Prediction Accuracy: {report['prediction_accuracy']:.0%} "
                f"({report.get('predictions_scored', 0)} scored)"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_weekly_report(report: dict[str, Any]) -> str:
        """Format weekly report for Telegram/console."""
        pnl = report.get("total_pnl", 0)
        pnl_sign = "+" if pnl >= 0 else ""
        lines = [
            "Weekly Report",
            f"Trades: {report.get('total_trades', 0)}",
            f"PnL: {pnl_sign}{pnl:,.2f}",
            f"Win Rate: {report.get('win_rate', 0):.0%}",
            f"LLM Reviews: {report.get('llm_approvals', 0)} approved, "
            f"{report.get('llm_rejections', 0)} rejected",
            f"LLM Approved PnL: {report.get('llm_approved_pnl', 0):,.2f}",
        ]
        if report.get("prediction_accuracy") is not None:
            lines.append(f"Prediction Accuracy: {report['prediction_accuracy']:.0%}")
        best = report.get("best_trade")
        worst = report.get("worst_trade")
        if best:
            lines.append(f"Best Trade: {best['symbol']} +{best['pnl']:,.2f}")
        if worst:
            lines.append(f"Worst Trade: {worst['symbol']} {worst['pnl']:,.2f}")
        # LLM review accuracy
        llm_acc = report.get("llm_review_accuracy")
        if llm_acc and llm_acc.get("approval_accuracy") is not None:
            lines.append(
                f"LLM Approval Accuracy: {llm_acc['approval_accuracy']:.0%} "
                f"({llm_acc['profitable_approvals']}/{llm_acc['approved_with_outcomes']})"
            )
        # Slippage summary
        slip = report.get("slippage_stats")
        if slip and slip.get("total_trades", 0) > 0:
            lines.append(
                f"Avg Slippage: {slip['avg_slippage']:.2f} "
                f"({slip['avg_slippage_pct']:.3%} of entry)"
            )
        return "\n".join(lines)
