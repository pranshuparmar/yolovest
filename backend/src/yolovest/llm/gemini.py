"""Google Gemini LLM implementation.

Implements all 7 LLMBase methods using the google-genai SDK.
Uses structured JSON output with Pydantic schema parsing.
"""

import asyncio
import json
import logging
from typing import Any

from yolovest.llm.base import LLMBase
from yolovest.models.schemas import (
    FailureAnalysis,
    MarketDaySummary,
    SentimentResult,
    TradeContext,
    TradeReview,
    WatchlistValidation,
    WebGroundingResult,
)

logger = logging.getLogger(__name__)


class GeminiLLM(LLMBase):
    """Concrete LLM using Google Gemini API.

    Uses Pro model for complex analysis, Flash for routine checks.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-pro",
        flash_model: str = "gemini-2.5-flash",
        max_retries: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._flash_model = flash_model
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._client: Any = None
        self._rate_limited_until: float = 0  # monotonic time when rate limit expires

    def _get_client(self) -> Any:
        """Lazy-init Gemini client."""
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def _generate(
        self, prompt: str, model: str | None = None, json_mode: bool = True
    ) -> str:
        """Generate text with retry and optional JSON mode."""
        client = self._get_client()
        use_model = model or self._model

        config: dict[str, Any] = {}
        if json_mode:
            config["response_mime_type"] = "application/json"

        import time as _time

        # Skip immediately if we know we're rate limited
        if _time.monotonic() < self._rate_limited_until:
            wait = self._rate_limited_until - _time.monotonic()
            logger.info("Gemini rate limited, skipping (%.0fs remaining)", wait)
            raise RuntimeError(f"Gemini rate limited for {wait:.0f}s more")

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=use_model,
                    contents=prompt,
                    config=config if config else None,
                )
                return str(response.text)
            except Exception as e:
                last_error = e
                err_str = str(e)
                # Detect 429 rate limit and extract retry delay
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    # Parse retryDelay from error message if available
                    import re
                    match = re.search(r"retryDelay.*?(\d+)", err_str)
                    cooldown = int(match.group(1)) if match else 60
                    self._rate_limited_until = _time.monotonic() + cooldown
                    logger.warning(
                        "Gemini 429 rate limited, cooling off for %ds", cooldown,
                    )
                    raise  # Don't retry 429s — waste of quota
                delay = self._retry_base_delay * (2 ** attempt)
                logger.warning(
                    "Gemini call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, self._max_retries, delay, e,
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

    async def _generate_json(
        self, prompt: str, model: str | None = None
    ) -> dict[str, Any]:
        """Generate and parse JSON with retry on malformed responses."""
        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                text = await self._generate(prompt, model=model, json_mode=True)
                result: dict[str, Any] = json.loads(text)
                return result
            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    "Gemini returned invalid JSON (attempt %d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
                # Re-prompt asking for valid JSON on retry
                prompt = (
                    f"{prompt}\n\n"
                    "IMPORTANT: Your previous response was not valid JSON. "
                    "Please respond with ONLY valid JSON, no markdown or extra text."
                )
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Quick health check using Flash model."""
        try:
            result = await self._generate(
                "Respond with exactly: {\"status\": \"ok\"}",
                model=self._flash_model,
            )
            return "ok" in result.lower()
        except Exception as e:
            # Use warning not exception to avoid noisy tracebacks on 429
            logger.warning("Gemini ping failed: %s", type(e).__name__)
            return False

    # ------------------------------------------------------------------
    # Core Trade Pipeline
    # ------------------------------------------------------------------

    async def review_trade(self, context: TradeContext) -> TradeReview:
        """Review a trade signal with full context (Pro model)."""
        prompt = (
            "You are a professional Indian stock market trader and risk manager.\n"
            "Review the following trade signal and decide whether to "
            "APPROVE, REJECT, or RESIZE.\n\n"
            f"Signal: {context.signal.model_dump_json()}\n"
            f"Portfolio: {context.portfolio.model_dump_json()}\n"
            f"Sentiment: {context.sentiment.model_dump_json() if context.sentiment else 'N/A'}\n"
            f"Today's trades: {len(context.todays_trades)}\n\n"
            "Respond with JSON matching this schema:\n"
            '{"decision": "APPROVE"|"REJECT"|"RESIZE", '
            '"reasoning": "...", "adjusted_size": null|int}\n'
            "If RESIZE, provide adjusted_size. Otherwise set it to null."
        )
        data = await self._generate_json(prompt, model=self._model)
        return TradeReview(**data)

    async def analyze_sentiment(
        self, symbol: str, headlines: list[str]
    ) -> SentimentResult:
        """Classify sentiment from headlines (Flash model for routine)."""
        headlines_text = "\n".join(f"- {h}" for h in headlines[:20])
        prompt = (
            f"Analyze the sentiment of these news headlines for {symbol} stock.\n\n"
            f"Headlines:\n{headlines_text}\n\n"
            "Respond with JSON:\n"
            '{"symbol": "...", "sentiment": "bullish"|"bearish"|"neutral", '
            '"confidence": 0.0-1.0, "key_drivers": ["driver1", "driver2"]}'
        )
        data = await self._generate_json(prompt, model=self._flash_model)
        return SentimentResult(**data)

    # ------------------------------------------------------------------
    # Market Intelligence
    # ------------------------------------------------------------------

    async def summarize_with_web_grounding(self, prompt: str) -> WebGroundingResult:
        """Summarize with web grounding (Pro model for depth)."""
        full_prompt = (
            "Search the web and provide a comprehensive summary.\n\n"
            f"Query: {prompt}\n\n"
            "Respond with JSON:\n"
            '{"query": "...", "summary": "...", "sources": ["url1", "url2"]}'
        )
        data = await self._generate_json(full_prompt, model=self._model)
        return WebGroundingResult(**data)

    async def validate_watchlist(
        self,
        shortlist: list[dict[str, object]],
        sector_analysis: dict[str, object],
        premarket_context: dict[str, object],
    ) -> WatchlistValidation:
        """Cross-validate watchlist against market narrative (Pro model)."""
        prompt = (
            "You are validating a trading watchlist for the Indian stock market.\n\n"
            f"Shortlisted stocks: {json.dumps(shortlist[:10], default=str)}\n"
            f"Sector analysis: {json.dumps(sector_analysis, default=str)}\n"
            f"Pre-market context: {json.dumps(premarket_context, default=str)}\n\n"
            "Respond with JSON:\n"
            '{"approved_symbols": [...], "rejected_symbols": [...], '
            '"reasoning": {"SYMBOL": "reason"}, "market_narrative": "..."}'
        )
        data = await self._generate_json(prompt, model=self._model)
        return WatchlistValidation(**data)

    # ------------------------------------------------------------------
    # Reporting & Analysis
    # ------------------------------------------------------------------

    async def summarize_market_day(self) -> MarketDaySummary:
        """Generate end-of-day summary (Pro model)."""
        from datetime import date

        prompt = (
            "Provide a concise end-of-day summary for today's Indian stock market.\n\n"
            "Respond with JSON:\n"
            '{"date": "YYYY-MM-DD", "market_sentiment": "bullish"|"bearish"|"neutral", '
            '"key_events": ["event1"], "sector_highlights": '
            '{"sector": "summary"}, "outlook": "..."}'
        )
        data = await self._generate_json(prompt, model=self._model)
        if "date" not in data:
            data["date"] = date.today().isoformat()
        return MarketDaySummary(**data)

    async def analyze_prediction_failures(
        self, failures: list[dict[str, object]]
    ) -> FailureAnalysis:
        """Analyze prediction failures for patterns (Pro model)."""
        prompt = (
            "Analyze these prediction failures from an ML trading model.\n\n"
            f"Failures: {json.dumps(failures[:20], default=str)}\n\n"
            "Identify patterns, common failure modes, and recommendations.\n"
            "Respond with JSON:\n"
            '{"patterns_identified": [...], "common_failure_modes": [...], '
            '"recommendations": [...], "summary": "..."}'
        )
        data = await self._generate_json(prompt, model=self._model)
        return FailureAnalysis(**data)
