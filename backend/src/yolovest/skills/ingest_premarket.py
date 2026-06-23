"""Skill: ingest-premarket — Pre-market data and overnight global cues.

Trigger: CRON — daily at 8:30 AM IST (before market scan)
Pipeline position: Runs after auth-broker, before market-scan.

Flow (India-first priority):
1. Fetch GIFT Nifty / SGX Nifty futures for market direction signal
2. Fetch Asian market opens (Nikkei, Hang Seng, Shanghai — regional context)
3. Fetch global commodity prices (crude, gold, USD/INR) that impact Indian markets
4. Fetch overnight US market moves (S&P 500, NASDAQ — global sentiment context)
5. Use Gemini with web grounding to summarize overnight developments
6. Store pre-market context for use by market-scan and generate-signals
"""

import asyncio
import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class IngestPremarketSkill(SkillBase):
    name = "ingest-premarket"
    description = "Fetch pre-market global cues and overnight data"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        return self.ctx.config.heartbeat.ingest_premarket_cron

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_premarket_window())

    async def execute(self, **kwargs: Any) -> SkillResult:
        premarket: dict[str, Any] = {}
        errors: list[str] = []

        # Fetch all data concurrently (India-first priority order)
        results = await asyncio.gather(
            self._fetch_gift_nifty(),
            self._fetch_asian_markets(),
            self._fetch_commodities(),
            self._fetch_us_markets(),  # global sentiment context
            return_exceptions=True,
        )

        for key, result in zip(
            ["gift_nifty", "asian_markets", "commodities", "us_markets"],
            results,
            strict=False,
        ):
            if isinstance(result, Exception):
                logger.warning("Failed to fetch %s: %s", key, result)
                errors.append(f"{key}: {result}")
                premarket[key] = {}
            else:
                premarket[key] = result

        # Gemini web grounding summary (skip if LLM disabled)
        if not self.ctx.config.llm.enabled:
            premarket["llm_summary"] = {}
        else:
            try:
                premarket["llm_summary"] = await self.ctx.llm.summarize_with_web_grounding(
                    "Summarize key overnight market developments affecting "
                    "Indian stock markets today. "
                    "Include: US market close, Asian market opens, "
                    "GIFT Nifty, crude oil, any major "
                    "global news that could impact Nifty/Sensex direction."
                )
            except Exception as e:
                logger.warning("LLM web grounding failed: %s", e)
                errors.append(f"llm_summary: {e}")
                premarket["llm_summary"] = {}

        await self.ctx.db.upsert_premarket(premarket)

        return SkillResult(
            success=len(errors) == 0,
            skill_name=self.name,
            data={
                "gift_nifty_change_pct": premarket["gift_nifty"].get("change_pct"),
                "us_sp500_change_pct": premarket["us_markets"].get("sp500_change_pct"),
                "errors": errors,
            },
        )

    async def _fetch_gift_nifty(self) -> dict[str, Any]:
        """Fetch GIFT Nifty / Nifty futures for market direction."""
        return await asyncio.to_thread(self._yf_change, "^NSEI")

    async def _fetch_us_markets(self) -> dict[str, Any]:
        """Fetch overnight US market closes (global sentiment context for FII flows)."""
        sp500, nasdaq, dow = await asyncio.gather(
            asyncio.to_thread(self._yf_change, "^GSPC"),
            asyncio.to_thread(self._yf_change, "^IXIC"),
            asyncio.to_thread(self._yf_change, "^DJI"),
        )
        return {
            "sp500_change_pct": sp500.get("change_pct"),
            "nasdaq_change_pct": nasdaq.get("change_pct"),
            "dow_change_pct": dow.get("change_pct"),
        }

    async def _fetch_asian_markets(self) -> dict[str, Any]:
        """Fetch Asian market opens."""
        nikkei, hsi, shanghai = await asyncio.gather(
            asyncio.to_thread(self._yf_change, "^N225"),
            asyncio.to_thread(self._yf_change, "^HSI"),
            asyncio.to_thread(self._yf_change, "000001.SS"),
        )
        return {
            "nikkei_change_pct": nikkei.get("change_pct"),
            "hang_seng_change_pct": hsi.get("change_pct"),
            "shanghai_change_pct": shanghai.get("change_pct"),
        }

    async def _fetch_commodities(self) -> dict[str, Any]:
        """Fetch commodity prices relevant to Indian markets."""
        crude, gold, usdinr = await asyncio.gather(
            asyncio.to_thread(self._yf_change, "CL=F"),
            asyncio.to_thread(self._yf_change, "GC=F"),
            asyncio.to_thread(self._yf_change, "USDINR=X"),
        )
        return {
            "crude_oil_usd": crude.get("price"),
            "crude_oil_change_pct": crude.get("change_pct"),
            "gold_usd": gold.get("price"),
            "usdinr": usdinr.get("price"),
        }

    @staticmethod
    def _yf_change(ticker_symbol: str) -> dict[str, Any]:
        """Fetch price and % change for a yfinance ticker (blocking, run in thread)."""
        try:
            import yfinance as yf

            ticker = yf.Ticker(ticker_symbol)
            info = ticker.fast_info
            price = float(info.last_price) if hasattr(info, "last_price") else 0.0
            prev = float(info.previous_close) if hasattr(info, "previous_close") else 0.0
            change_pct = ((price - prev) / prev * 100) if prev else 0.0
            return {"price": price, "previous_close": prev, "change_pct": round(change_pct, 2)}
        except Exception as e:
            logger.warning("yfinance fetch failed for %s: %s", ticker_symbol, e)
            return {"price": 0.0, "previous_close": 0.0, "change_pct": 0.0}
