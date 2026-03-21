"""Skill: report-generate — Daily and weekly reports + Telegram delivery.

Covers: FR-8.4, FR-8.5, FR-8.6
Trigger: CRON — daily at reports.daily_report_time, weekly at reports.weekly_report_cron
Pipeline position: Post-market, after square-off.

Flow:
Daily report (FR-8.4):
1. Aggregate today's trades: entries, exits, PnL per trade
2. Compute daily PnL, win rate, avg slippage
3. Prediction accuracy for today's signals
4. Top signals (best confidence scores)
5. Market summary (via Gemini)
6. Store report in DB for dashboard access
7. Send to Telegram (if notifications.telegram.alerts.daily_summary is true)

Weekly report (FR-8.5):
1. Cumulative PnL for the week
2. Model performance trends (accuracy over time)
3. Prediction accuracy trends
4. Gemini review analysis: did LLM approvals/rejections help? (FR-7.8)
5. Best/worst trades of the week
6. Risk metrics: max drawdown, Sharpe for the period
7. Store + send to Telegram (if weekly_summary alert enabled)
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class ReportGenerateSkill(SkillBase):
    name = "report-generate"
    description = "Generate daily/weekly reports and deliver via Telegram"
    trigger = SkillTrigger.CRON
    schedule = None  # set dynamically from reports.daily_report_time / weekly_report_cron

    def should_run(self) -> bool:
        return not self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        report_type = kwargs.get("type", "daily")

        if report_type == "daily":
            return await self._generate_daily()
        else:
            return await self._generate_weekly()

    async def _generate_daily(self) -> SkillResult:
        """FR-8.4: Daily report at market close."""
        trades = await self.ctx.db.get_todays_trades()
        predictions = await self.ctx.db.get_todays_predictions()

        total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        win_rate = len(wins) / len(trades) if trades else 0
        avg_slippage = (
            sum(t.get("slippage", 0) for t in trades) / len(trades) if trades else 0
        )

        # Prediction accuracy
        scored = [p for p in predictions if p.get("direction_correct") is not None]
        pred_accuracy = (
            sum(1 for p in scored if p["direction_correct"]) / len(scored)
            if scored
            else None
        )

        # Gemini market summary
        market_summary = await self.ctx.llm.summarize_market_day()

        report = {
            "type": "daily",
            "total_trades": len(trades),
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "avg_slippage": avg_slippage,
            "prediction_accuracy": pred_accuracy,
            "market_summary": market_summary,
            "trades": trades,
        }

        await self.ctx.db.store_report(report)

        # Telegram delivery
        alerts_cfg = self.ctx.config.notifications.telegram.alerts
        if alerts_cfg.daily_summary:
            await self.ctx.notify.send_daily_report(report)

        return SkillResult(success=True, skill_name=self.name, data=report)

    async def _generate_weekly(self) -> SkillResult:
        """FR-8.5: Weekly cumulative report."""
        trades = await self.ctx.db.get_weekly_trades()
        predictions = await self.ctx.db.get_weekly_predictions()
        llm_reviews = await self.ctx.db.get_weekly_llm_reviews()

        total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)

        # FR-7.8: LLM review accuracy
        llm_approved = [r for r in llm_reviews if r["decision"] == "APPROVE"]
        llm_rejected = [r for r in llm_reviews if r["decision"] == "REJECT"]
        llm_approve_pnl = sum(
            r.get("trade_pnl", 0) for r in llm_approved if r.get("trade_pnl") is not None
        )

        report = {
            "type": "weekly",
            "total_trades": len(trades),
            "total_pnl": total_pnl,
            "llm_approvals": len(llm_approved),
            "llm_rejections": len(llm_rejected),
            "llm_approved_pnl": llm_approve_pnl,
            "predictions_scored": len(predictions),
        }

        await self.ctx.db.store_report(report)

        alerts_cfg = self.ctx.config.notifications.telegram.alerts
        if alerts_cfg.weekly_summary:
            await self.ctx.notify.send_weekly_report(report)

        return SkillResult(success=True, skill_name=self.name, data=report)
