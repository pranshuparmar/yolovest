"""Per-(symbol, as-of) news sentiment features for the ML model.

Headlines + published_at live in the `news_articles` table. VADER scores
each headline's polarity; we aggregate over 24h / 7d windows ending at
`as_of_dt` to produce features that work identically at training time
(historical headlines filtered by published_at) and at inference time
(recent headlines for the live signal). The same window logic on both
sides is what keeps training and inference leakage-free and consistent.

Five features are emitted:
  news_count_24h           — article count in the trailing 24h
  news_count_7d            — article count in the trailing 7d
  news_sentiment_24h       — VADER compound mean over the 24h window
  news_sentiment_7d        — VADER compound mean over the 7d window
  news_sentiment_momentum  — 24h mean − 7d mean (positive = improving tone)
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

NEWS_FEATURE_KEYS: tuple[str, ...] = (
    "news_count_24h",
    "news_count_7d",
    "news_sentiment_24h",
    "news_sentiment_7d",
    "news_sentiment_momentum",
)


def _neutral() -> dict[str, float]:
    return {k: 0.0 for k in NEWS_FEATURE_KEYS}


_analyzer = None


def _get_analyzer() -> Any:
    global _analyzer
    if _analyzer is not None:
        return _analyzer
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError:
        return None
    _analyzer = SentimentIntensityAnalyzer()
    return _analyzer


def compute_news_features(
    headlines: Iterable[tuple[str, datetime]],
    as_of_dt: datetime,
) -> dict[str, float]:
    """Aggregate VADER polarity over 24h and 7d windows ending at as_of_dt.

    headlines: iterable of (headline_text, published_at). published_at
    must be timezone-aware and match as_of_dt's tzinfo (caller's job —
    callers typically pass IST throughout). Headlines outside the 7d
    window are silently dropped.

    Returns the five news features with neutral defaults (0.0 / 0.0) when
    no headlines fall in either window or VADER isn't installed.
    """
    analyzer = _get_analyzer()
    if analyzer is None:
        return _neutral()

    cutoff_24h = as_of_dt - timedelta(hours=24)
    cutoff_7d = as_of_dt - timedelta(days=7)

    scores_24h: list[float] = []
    scores_7d: list[float] = []
    for headline, published_at in headlines:
        if published_at > as_of_dt or published_at < cutoff_7d:
            continue
        text = (headline or "").strip()
        if not text:
            continue
        compound = float(analyzer.polarity_scores(text)["compound"])
        scores_7d.append(compound)
        if published_at >= cutoff_24h:
            scores_24h.append(compound)

    mean_24h = sum(scores_24h) / len(scores_24h) if scores_24h else 0.0
    mean_7d = sum(scores_7d) / len(scores_7d) if scores_7d else 0.0
    return {
        "news_count_24h": float(len(scores_24h)),
        "news_count_7d": float(len(scores_7d)),
        "news_sentiment_24h": mean_24h,
        "news_sentiment_7d": mean_7d,
        "news_sentiment_momentum": mean_24h - mean_7d,
    }
