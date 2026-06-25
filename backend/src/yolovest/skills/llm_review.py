"""Skill: llm-review — Gemini-powered trade review gate.

Trigger: EVENT — called for each risk-approved signal
Pipeline position: After risk-check, before trade-execute.

Flow:
1. Check if LLM review is enabled (risk.llm_review_enabled)
2. If disabled or LLM unavailable → fall back to rules-only (auto-approve)
3. Build full context for Gemini:
   - ML signal details (entry, target, SL, confidence)
   - Technical indicator snapshot
   - News sentiment for the symbol
   - Current portfolio state
   - Market conditions (pre-market cues, sector rotation)
   - Today's trade history (wins/losses)
4. Send to Gemini for structured review
5. Gemini returns: APPROVE / REJECT / RESIZE with reasoning
6. If RESIZE: adjust position size per Gemini recommendation
7. Log the full LLM reasoning for audit trail
8. Track LLM review accuracy over time
"""

import logging
from typing import TYPE_CHECKING, Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

if TYPE_CHECKING:
    from yolovest.models.schemas import TradeContext

logger = logging.getLogger(__name__)


class LLMReviewSkill(SkillBase):
    name = "llm-review"
    description = "Gemini trade approval gate with full context"
    trigger = SkillTrigger.EVENT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.config.llm.enabled and self.ctx.config.risk.llm_review_enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        signal = kwargs["signal"]
        cfg = self.ctx.config.risk

        # If LLM disabled or unavailable, auto-approve
        if not cfg.llm_review_enabled or not self.ctx.config.llm.enabled:
            return self._auto_approve(signal, "LLM review disabled")

        try:
            # Build full context
            context = await self._build_review_context(signal)

            # Send to Gemini
            review = await self.ctx.llm.review_trade(context)

            # Log for audit and accuracy tracking
            await self.ctx.db.log_llm_review(
                signal=signal,
                decision=review.decision,
                reasoning=review.reasoning,
                adjusted_size=review.adjusted_size,
            )

            if review.decision == "APPROVE":
                logger.info(
                    "llm-review: APPROVE %s — %s",
                    signal["symbol"], review.reasoning[:100],
                )
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={
                        "approved": True,
                        "signal": signal,
                        "llm_reasoning": review.reasoning,
                    },
                )
            elif review.decision == "RESIZE":
                if not isinstance(review.adjusted_size, int) or review.adjusted_size <= 0:
                    logger.warning(
                        "llm-review: REJECT %s — LLM returned invalid adjusted_size=%s",
                        signal["symbol"], review.adjusted_size,
                    )
                    return SkillResult(
                        success=True,
                        skill_name=self.name,
                        data={
                            "approved": False,
                            "signal": signal,
                            "llm_reasoning": f"Invalid adjusted_size: {review.adjusted_size}",
                        },
                    )
                # The LLM may only TRIM size, never inflate it. risk-check
                # already sized this against every cap (risk budget,
                # single-stock exposure, margin) and llm-review runs after
                # it — an up-resize would bypass all of them. The LLM context
                # also embeds externally-scraped news sentiment, a
                # prompt-injection surface. Clamp to the risk-checked size.
                risk_checked_size = int(signal.get("position_size") or 0)
                adjusted_size = review.adjusted_size
                if risk_checked_size > 0 and adjusted_size > risk_checked_size:
                    logger.warning(
                        "llm-review: clamping LLM up-resize %d→%d for %s "
                        "(LLM may only trim, never exceed the risk-checked size)",
                        adjusted_size, risk_checked_size, signal["symbol"],
                    )
                    adjusted_size = risk_checked_size
                logger.info(
                    "llm-review: RESIZE %s %d→%d — %s",
                    signal["symbol"], signal["position_size"],
                    adjusted_size, review.reasoning[:100],
                )
                resized_signal = {**signal, "position_size": adjusted_size}
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={
                        "approved": True,
                        "resized": True,
                        "original_size": signal["position_size"],
                        "adjusted_size": adjusted_size,
                        "signal": resized_signal,
                        "llm_reasoning": review.reasoning,
                    },
                )
            else:  # REJECT
                logger.info(
                    "llm-review: REJECT %s — %s",
                    signal["symbol"], review.reasoning[:100],
                )
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={
                        "approved": False,
                        "signal": signal,
                        "llm_reasoning": review.reasoning,
                    },
                )

        except Exception as e:
            # Fallback to rules-only
            if cfg.llm_fallback_to_rules:
                logger.warning(
                    "llm-review: fallback to rules-only for %s — %s",
                    signal["symbol"], e,
                )
                return self._auto_approve(signal, "LLM unavailable, fallback to rules-only")
            raise

    async def _build_review_context(self, signal: dict[str, Any]) -> "TradeContext":
        """Assemble full context for Gemini review as a proper TradeContext model."""
        from yolovest.models.schemas import (
            PortfolioState,
            PremarketContext,
            SentimentResult,
            Signal,
            Trade,
            TradeContext,
        )

        symbol = signal["symbol"]

        # Build typed Signal from dict
        signal_model = Signal(**{
            k: signal[k] for k in Signal.model_fields if k in signal
        })

        # Build typed PortfolioState from DB dict
        portfolio_dict = await self.ctx.db.get_portfolio_state()
        portfolio_model = PortfolioState(**portfolio_dict)

        # Build typed SentimentResult (may be None)
        sentiment_model = None
        sentiment_dict = await self.ctx.db.get_latest_sentiment(symbol)
        if sentiment_dict:
            try:
                sentiment_model = SentimentResult(**sentiment_dict)
            except Exception:
                logger.debug("Failed to parse sentiment for %s", symbol, exc_info=True)

        # Build typed PremarketContext (may be None)
        premarket_model = None
        premarket_dict = await self.ctx.db.get_latest_premarket()
        if premarket_dict:
            try:
                premarket_model = PremarketContext(**premarket_dict)
            except Exception:
                logger.debug("Failed to parse premarket context", exc_info=True)

        sector_rotation = await self.ctx.db.get_sector_rotation()
        todays_trades_raw = await self.ctx.db.get_todays_trades()

        # Build typed Trade list (best-effort, skip malformed entries)
        todays_trades: list[Trade] = []
        for t in todays_trades_raw:
            try:
                todays_trades.append(Trade(**t))
            except Exception:
                logger.debug("Failed to parse trade record for LLM context", exc_info=True)

        return TradeContext(
            signal=signal_model,
            portfolio=portfolio_model,
            sentiment=sentiment_model,
            premarket=premarket_model,
            sector_rotation=sector_rotation,
            todays_trades=todays_trades,
        )

    def _auto_approve(self, signal: dict[str, Any], reason: str) -> SkillResult:
        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"approved": True, "signal": signal, "auto_approved": True, "reason": reason},
        )
