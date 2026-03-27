"""Abstract LLM interface (ABC).

All LLM implementations (Gemini, etc.) extend LLMBase.
"""

from abc import ABC, abstractmethod

from yolovest.models.schemas import (
    FailureAnalysis,
    MarketDaySummary,
    SentimentResult,
    TradeContext,
    TradeReview,
    WatchlistValidation,
    WebGroundingResult,
)


class LLMBase(ABC):
    """Abstract base for LLM integrations.

    Six core methods covering trade review, sentiment analysis,
    market intelligence, and self-learning analysis.
    """

    # --- Health ---

    @abstractmethod
    async def ping(self) -> bool:
        """Health check — returns True if the LLM API is reachable."""
        ...

    # --- Core trade pipeline ---

    @abstractmethod
    async def review_trade(self, context: TradeContext) -> TradeReview:
        """Review a trade signal with full context and return approval/rejection."""
        ...

    @abstractmethod
    async def analyze_sentiment(
        self, symbol: str, headlines: list[str]
    ) -> SentimentResult:
        """Analyze news headlines for a symbol and classify sentiment."""
        ...

    # --- Market intelligence ---

    @abstractmethod
    async def summarize_with_web_grounding(self, prompt: str) -> WebGroundingResult:
        """Search and summarize real-time market news with web grounding."""
        ...

    @abstractmethod
    async def validate_watchlist(
        self,
        shortlist: list[dict[str, object]],
        sector_analysis: dict[str, object],
        premarket_context: dict[str, object],
    ) -> WatchlistValidation:
        """Cross-validate top-ranked stocks against current market narrative."""
        ...

    # --- Reporting & analysis ---

    @abstractmethod
    async def summarize_market_day(self) -> MarketDaySummary:
        """Generate an end-of-day market summary."""
        ...

    @abstractmethod
    async def analyze_prediction_failures(
        self, failures: list[dict[str, object]]
    ) -> FailureAnalysis:
        """Analyze prediction failures to identify patterns and recommendations."""
        ...
