"""Skill: llm-review — Gemini-powered trade review gate.

Covers: FR-4.4, FR-5.11, FR-5.12
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
7. Log the full LLM reasoning for audit trail (FR-8.8)
8. Track LLM review accuracy over time (FR-7.8)
"""

from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class LLMReviewSkill(SkillBase):
    name = "llm-review"
    description = "Gemini trade approval gate with full context"
    trigger = SkillTrigger.EVENT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.config.risk.llm_review_enabled)

    async def execute(self, **kwargs: Any) -> SkillResult:
        signal = kwargs["signal"]
        cfg = self.ctx.config.risk

        # FR-5.12: If LLM disabled or unavailable, auto-approve
        if not cfg.llm_review_enabled:
            return self._auto_approve(signal, "LLM review disabled")

        try:
            # Build full context
            context = await self._build_review_context(signal)

            # Send to Gemini
            review = await self.ctx.llm.review_trade(context)

            # Log for audit (FR-8.8) and accuracy tracking (FR-7.8)
            await self.ctx.db.log_llm_review(
                signal=signal,
                decision=review.decision,
                reasoning=review.reasoning,
                adjusted_size=review.adjusted_size,
            )

            if review.decision == "APPROVE":
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
                resized_signal = {**signal, "position_size": review.adjusted_size}
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={
                        "approved": True,
                        "resized": True,
                        "original_size": signal["position_size"],
                        "adjusted_size": review.adjusted_size,
                        "signal": resized_signal,
                        "llm_reasoning": review.reasoning,
                    },
                )
            else:  # REJECT
                return SkillResult(
                    success=True,
                    skill_name=self.name,
                    data={
                        "approved": False,
                        "signal": signal,
                        "llm_reasoning": review.reasoning,
                    },
                )

        except Exception:
            # FR-5.12: Fallback to rules-only
            if cfg.llm_fallback_to_rules:
                return self._auto_approve(signal, "LLM unavailable, fallback to rules-only")
            raise

    async def _build_review_context(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Assemble full context for Gemini review."""
        symbol = signal["symbol"]
        return {
            "signal": signal,
            "sentiment": await self.ctx.db.get_latest_sentiment(symbol),
            "portfolio": await self.ctx.db.get_portfolio_state(),
            "premarket": await self.ctx.db.get_latest_premarket(),
            "sector_rotation": await self.ctx.db.get_sector_rotation(),
            "todays_trades": await self.ctx.db.get_todays_trades(),
        }

    def _auto_approve(self, signal: dict[str, Any], reason: str) -> SkillResult:
        return SkillResult(
            success=True,
            skill_name=self.name,
            data={"approved": True, "signal": signal, "auto_approved": True, "reason": reason},
        )
