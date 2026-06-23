"""Skill: news-digest — Send today's news headlines to Telegram.

Trigger: CRON — daily at configured time (default 9:00 AM IST, weekdays)
Pipeline position: Independent, runs alongside other CRON skills.

Flow:
1. Check if news digest is enabled in config
2. Fetch today's news articles from DB (latest N headlines)
3. Format as a Telegram message with headlines + sources
4. Include link to dashboard news page if there are more articles
5. If no news available, send a "no news ingested" message
6. Deliver via ctx.notify.send()
"""

import logging
import os
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)


class NewsDigestSkill(SkillBase):
    name = "news-digest"
    description = "Send daily news headlines digest to Telegram"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        return self.ctx.config.news_digest.schedule_cron

    def should_run(self) -> bool:
        return bool(self.ctx.config.news_digest.enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        cfg = self.ctx.config.news_digest
        max_headlines = cfg.max_headlines
        telegram_limit = 5  # Telegram messages have a 4096 char limit
        today = now_ist().strftime("%Y-%m-%d")

        # Fetch today's articles (all of them for the digest result)
        articles = await self.ctx.db.get_news_articles(
            date_from=today, limit=max_headlines + 1,
        )

        total_today = len(articles)
        has_more = total_today > max_headlines
        headlines = articles[:max_headlines]

        # Build dashboard URL
        domain = os.environ.get("DOMAIN")
        if domain:
            news_url = f"https://{domain}/news"
        else:
            port = self.ctx.config.dashboard.port
            news_url = f"http://localhost:{port}/news"

        # Telegram message — only top N to stay within message size limit
        tg_headlines = articles[:telegram_limit]
        if not tg_headlines:
            msg = (
                f"News Digest — {today}\n\n"
                "No news articles ingested today.\n"
                "Check if news sources are enabled and reachable."
            )
        else:
            lines = [f"News Digest — {today}\n"]
            for i, a in enumerate(tg_headlines, 1):
                source = a.get("source", "")
                headline = a.get("headline", "")
                url = a.get("url", "")
                source_tag = f" [{source}]" if source else ""
                if url:
                    lines.append(f"{i}. {headline}{source_tag}\n   {url}")
                else:
                    lines.append(f"{i}. {headline}{source_tag}")
            remaining = total_today - telegram_limit
            if remaining > 0:
                lines.append(f"\n+{remaining} more — {news_url}")
            msg = "\n".join(lines)

        await self.ctx.notify.send(msg, alert_type="daily_summary")

        logger.info(
            "news-digest: sent %d headlines for %s (total=%d, has_more=%s)",
            len(headlines), today, total_today, has_more,
        )

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "date": today,
                "headlines_sent": len(headlines),
                "total_available": total_today,
                "has_more": has_more,
            },
        )
