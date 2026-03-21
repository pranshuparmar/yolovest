"""Skill: ingest-premarket — Pre-market data and overnight global cues.

Covers: FR-2.10, FR-2.11
Trigger: CRON — daily at 8:30 AM IST (before market scan)
Pipeline position: Runs after auth-broker, before market-scan.

Flow:
1. Fetch GIFT Nifty / SGX Nifty futures for market direction signal
2. Fetch overnight US market moves (S&P 500, NASDAQ, Dow)
3. Fetch Asian market opens (Nikkei, Hang Seng, Shanghai)
4. Fetch global commodity prices (crude, gold) that impact Indian markets
5. Use Gemini with web grounding to summarize overnight developments
6. Store pre-market context for use by market-scan and generate-signals
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class IngestPremarketSkill(SkillBase):
    name = "ingest-premarket"
    description = "Fetch pre-market global cues and overnight data"
    trigger = SkillTrigger.CRON
    schedule = "30 8 * * 1-5"  # 8:30 AM IST, weekdays

    def should_run(self) -> bool:
        return self.ctx.market_hours.is_premarket_window()

    async def execute(self, **kwargs: Any) -> SkillResult:
        premarket: dict[str, Any] = {}

        # GIFT Nifty / SGX Nifty for market direction
        premarket["gift_nifty"] = await self._fetch_gift_nifty()

        # Overnight US markets
        premarket["us_markets"] = await self._fetch_us_markets()

        # Asian market opens
        premarket["asian_markets"] = await self._fetch_asian_markets()

        # Commodities (crude, gold)
        premarket["commodities"] = await self._fetch_commodities()

        # Gemini web grounding summary (FR-2.11)
        premarket["llm_summary"] = await self.ctx.llm.summarize_with_web_grounding(
            "Summarize key overnight market developments affecting Indian stock markets today. "
            "Include: US market close, Asian market opens, GIFT Nifty, crude oil, any major "
            "global news that could impact Nifty/Sensex direction."
        )

        await self.ctx.db.upsert_premarket(premarket)

        return SkillResult(
            success=True,
            skill_name=self.name,
            data={
                "gift_nifty_change_pct": premarket["gift_nifty"].get("change_pct"),
                "us_sp500_change_pct": premarket["us_markets"].get("sp500_change_pct"),
                "market_bias": premarket["llm_summary"].get("bias"),  # bullish/bearish/neutral
            },
        )

    async def _fetch_gift_nifty(self) -> dict:
        raise NotImplementedError

    async def _fetch_us_markets(self) -> dict:
        raise NotImplementedError

    async def _fetch_asian_markets(self) -> dict:
        raise NotImplementedError

    async def _fetch_commodities(self) -> dict:
        raise NotImplementedError
