"""Pydantic v2 data models for inter-skill data contracts.

All data exchange between skills uses these typed models.
No raw dicts between skills.
"""

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from yolovest.timezone import now_ist

# ---------------------------------------------------------------------------
# Market Data
# ---------------------------------------------------------------------------


class OHLCVBar(BaseModel):
    """Single OHLCV candle bar."""

    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)


def is_valid_ohlc(*values: Any) -> bool:
    """True only when every O/H/L/C value is a real positive number.

    Data providers return junk bars — None, NaN, or 0.0 placeholders for
    pre-listing / no-trade / illiquid days (e.g. Kite's pre-IPO rows for a
    recently-listed symbol). OHLCVBar enforces gt=0, so a single junk bar
    would otherwise raise and fail the WHOLE symbol's fetch. Providers call
    this to skip such bars instead. Volume isn't checked here (0 volume is
    a legitimate bar)."""
    for v in values:
        if v is None:
            return False
        try:
            f = float(v)
        except (TypeError, ValueError):
            return False
        if f != f or f <= 0:  # NaN (f != f) or non-positive
            return False
    return True


class PremarketContext(BaseModel):
    """Pre-market global cues collected before market open."""

    gift_nifty_change_pct: float | None = None
    us_sp500_change_pct: float | None = None
    market_bias: Literal["bullish", "bearish", "neutral"] | None = None
    llm_summary: str | None = None


# ---------------------------------------------------------------------------
# Core Trading
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """ML-generated trade signal with full context."""

    symbol: str
    signal_type: Literal["BUY", "SELL", "HOLD"]
    entry_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    position_size: int = Field(gt=0)
    expected_holding_period: Literal["intraday", "3d", "1w"] = "intraday"
    product: Literal["MIS", "CNC"] = "MIS"
    confidence_score: float = Field(ge=0.0, le=1.0)
    model_version: str
    features_snapshot: dict[str, Any] = Field(default_factory=dict)


class Trade(BaseModel):
    """Executed (or pending) trade with full lifecycle tracking."""

    trade_id: str
    symbol: str
    signal_type: Literal["BUY", "SELL"]
    entry_price: float = Field(gt=0)
    fill_price: float = Field(ge=0)
    quantity: int = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    order_id: str | None = None  # None for paper trades
    sl_order_id: str | None = None
    product: Literal["MIS", "CNC"]
    mode: Literal["paper", "live"]
    status: Literal[
        "placed", "open", "partially_filled", "filled", "rejected", "cancelled"
    ]
    slippage: float = 0.0
    pnl: float | None = None  # None while open
    exit_price: float | None = None
    created_at: datetime = Field(default_factory=now_ist)
    closed_at: datetime | None = None


class Position(BaseModel):
    """An open position with real-time tracking."""

    position_id: str
    trade: Trade
    current_price: float = Field(gt=0)
    unrealized_pnl: float = 0.0
    trailing_sl_active: bool = False


class PortfolioState(BaseModel):
    """Current snapshot of the portfolio for risk checks."""

    total_capital: float = Field(ge=0)  # legacy: initial + realized PnL
    available_cash: float = Field(ge=0)  # legacy: total_capital - system_position_value
    exposure_pct: float = Field(ge=0.0, le=1.0)
    open_positions: int = Field(default=0, ge=0)
    stock_exposures: dict[str, float] = Field(default_factory=dict)  # symbol -> %
    sector_counts: dict[str, int] = Field(default_factory=dict)  # sector -> count
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    daily_pnl: float = 0.0  # absolute realized PnL today
    weekly_pnl: float = 0.0  # absolute realized PnL this week
    trades_today: int = Field(default=0, ge=0)
    minutes_since_last_loss: float = Field(default=0.0, ge=0)
    # Broker-synced capital breakdown
    available_funds: float = 0.0  # free cash from Kite available.cash
    utilised_margin: float = 0.0  # margin locked in open intraday MIS positions
    pending_trade_value: float = 0.0  # sum of pending-approval trade values
    locked_total: float = 0.0  # utilised_margin + pending_trade_value
    holdings_invested: float = 0.0  # total buy price of CNC delivery holdings
    holdings_current: float = 0.0  # current market value of CNC delivery holdings
    holdings_unrealized_pnl: float = 0.0  # holdings_current - holdings_invested
    holdings_unrealized_pnl_pct: float = 0.0
    total_portfolio_value: float = 0.0  # available_funds + utilised + holdings_current
    total_pnl: float = 0.0  # all-time realized + holdings unrealized
    all_time_realized_pnl: float = 0.0


# ---------------------------------------------------------------------------
# LLM I/O — Sentiment
# ---------------------------------------------------------------------------


class SentimentResult(BaseModel):
    """LLM-generated sentiment analysis for a symbol."""

    symbol: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    key_drivers: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM I/O — Trade Review
# ---------------------------------------------------------------------------


class TradeContext(BaseModel):
    """Full context sent to LLM for trade review."""

    signal: Signal
    portfolio: PortfolioState
    sentiment: SentimentResult | None = None
    premarket: PremarketContext | None = None
    sector_rotation: dict[str, Any] | None = None
    todays_trades: list[Trade] = Field(default_factory=list)


class TradeReview(BaseModel):
    """LLM trade review decision."""

    decision: Literal["APPROVE", "REJECT", "RESIZE"]
    reasoning: str
    adjusted_size: int | None = None  # only if RESIZE

    @model_validator(mode="after")
    def validate_resize_has_size(self) -> "TradeReview":
        if self.decision == "RESIZE" and self.adjusted_size is None:
            raise ValueError("adjusted_size is required when decision is RESIZE")
        return self


# ---------------------------------------------------------------------------
# LLM I/O — Additional output types
# ---------------------------------------------------------------------------


class WebGroundingResult(BaseModel):
    """Result from LLM web-grounded search and summarization."""

    query: str
    summary: str
    sources: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=now_ist)


class WatchlistValidation(BaseModel):
    """LLM cross-validation of watchlist against market narrative."""

    approved_symbols: list[str] = Field(default_factory=list)
    rejected_symbols: list[str] = Field(default_factory=list)
    reasoning: dict[str, str] = Field(default_factory=dict)  # symbol -> reason
    market_narrative: str = ""


class MarketDaySummary(BaseModel):
    """LLM-generated end-of-day market summary."""

    date: str  # YYYY-MM-DD
    market_sentiment: Literal["bullish", "bearish", "neutral"]
    key_events: list[str] = Field(default_factory=list)
    sector_highlights: dict[str, str] = Field(default_factory=dict)
    outlook: str = ""


class FailureAnalysis(BaseModel):
    """LLM analysis of prediction failures — patterns and recommendations."""

    patterns_identified: list[str] = Field(default_factory=list)
    common_failure_modes: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Prediction Tracking
# ---------------------------------------------------------------------------


def _parse_holding_period(period_str: str) -> timedelta:
    """Parse holding period string like 'intraday', '3d', '1w' into timedelta."""
    period_str = period_str.strip().lower()
    if period_str == "intraday":
        return timedelta(hours=6, minutes=15)  # market day length
    if period_str.endswith("d"):
        return timedelta(days=int(period_str[:-1]))
    if period_str.endswith("w"):
        return timedelta(weeks=int(period_str[:-1]))
    if period_str.endswith("h"):
        return timedelta(hours=int(period_str[:-1]))
    # Default: treat as days
    try:
        return timedelta(days=int(period_str))
    except ValueError:
        return timedelta(days=1)


# ---------------------------------------------------------------------------
# News & Intelligence
# ---------------------------------------------------------------------------


class EconomicEvent(BaseModel):
    """An economic calendar event: RBI/Fed policy, earnings, GDP."""

    event_date: str  # YYYY-MM-DD
    event_type: str  # "monetary_policy", "earnings", "gdp", "trade_data"
    title: str
    country: str  # "IN", "US"
    impact: Literal["high", "medium", "low"] = "medium"
    source: str  # "rbi_schedule", "fed_schedule", "nse_announcements"
    symbol: str | None = None  # stock symbol for earnings, None for macro events
    content_hash: str = ""

    @model_validator(mode="after")
    def compute_hash(self) -> "EconomicEvent":
        if not self.content_hash:
            import hashlib

            content = f"{self.event_date}:{self.event_type}:{self.title}:{self.country}"
            self.content_hash = hashlib.sha256(content.lower().encode()).hexdigest()
        return self


class NewsArticle(BaseModel):
    """A single news article from any source, with dedup hash."""

    headline: str
    source: str  # "moneycontrol", "et_markets", "livemint", "nse", "google"
    url: str | None = None
    symbols: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    content_hash: str = ""  # SHA256 of normalized headline

    @model_validator(mode="after")
    def compute_hash(self) -> "NewsArticle":
        if not self.content_hash:
            import hashlib

            normalized = self.headline.strip().lower()
            self.content_hash = hashlib.sha256(normalized.encode()).hexdigest()
        return self


class FeatureAttribution(BaseModel):
    """One feature's contribution to a prediction. Sign of contribution
    tells you whether the feature pushed the model toward (+) or away
    from (-) the predicted class. Magnitude is in log-odds space.
    """

    feature: str
    value: float  # actual feature value at inference
    contribution: float  # signed contribution to the predicted class


class MLPrediction(BaseModel):
    """Output from ML model inference."""

    signal_type: Literal["BUY", "SELL", "HOLD"]
    entry_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    position_size: int = Field(gt=0)
    holding_period: str  # "intraday", "3d", "1w"
    confidence: float = Field(ge=0.0, le=1.0)
    model_version: str
    # Probability mass per class (after calibration if calibration was
    # applied), keys are "BUY" / "SELL" / "HOLD". Lets generate-signals
    # log the runner-up class so we can tell whether a HOLD output is a
    # confident HOLD or a knife-edge case where BUY/SELL was close.
    class_probabilities: dict[str, float] | None = None
    # Top-N feature contributions (typically top-5 by absolute value).
    # Surfaced on TradeDetailPage so the user can see WHY the model
    # picked the setup. None when the booster isn't reachable through
    # the calibration wrapper (defensive).
    attribution: list[FeatureAttribution] | None = None


class BacktestResult(BaseModel):
    """Results from walk-forward backtesting."""

    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    total_trades: int
    total_return_pct: float
    trade_log: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prediction Tracking
# ---------------------------------------------------------------------------


class Prediction(BaseModel):
    """Logged prediction for self-learning scoreboard."""

    prediction_id: str
    signal: Signal
    trade_id: str | None = None
    created_at: datetime = Field(default_factory=now_ist)
    prediction_end_time: datetime | None = None  # computed from holding period
    actual_price: float | None = None
    direction_correct: bool | None = None
    target_hit: bool | None = None
    actual_pnl_pct: float | None = None

    @model_validator(mode="after")
    def compute_end_time(self) -> "Prediction":
        if self.prediction_end_time is None:
            delta = _parse_holding_period(self.signal.expected_holding_period)
            self.prediction_end_time = self.created_at + delta
        return self
