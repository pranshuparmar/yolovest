"""Tests for the per-(symbol, as-of) news sentiment features."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from yolovest.data.news_features import NEWS_FEATURE_KEYS, compute_news_features
from yolovest.timezone import IST


@pytest.fixture
def now() -> datetime:
    return datetime(2025, 6, 15, 10, 0, 0, tzinfo=IST)


def test_empty_headlines_returns_neutral(now: datetime) -> None:
    feats = compute_news_features([], now)
    assert feats == {k: 0.0 for k in NEWS_FEATURE_KEYS}


def test_keys_always_present(now: datetime) -> None:
    feats = compute_news_features([], now)
    assert set(feats.keys()) == set(NEWS_FEATURE_KEYS)


def test_positive_headline_in_24h(now: datetime) -> None:
    headlines = [
        ("Company beats earnings estimates, reports record profit growth", now - timedelta(hours=2)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_count_24h"] == 1.0
    assert feats["news_count_7d"] == 1.0
    assert feats["news_sentiment_24h"] > 0.0
    assert feats["news_sentiment_7d"] == pytest.approx(feats["news_sentiment_24h"])
    assert feats["news_sentiment_momentum"] == pytest.approx(0.0)


def test_negative_headline_in_24h(now: datetime) -> None:
    headlines = [
        ("Company reports terrible loss, fraud allegations damage outlook", now - timedelta(hours=3)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_count_24h"] == 1.0
    assert feats["news_sentiment_24h"] < 0.0


def test_window_exclusion_outside_7d(now: datetime) -> None:
    headlines = [
        ("Great wonderful excellent profit growth", now - timedelta(days=10)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_count_7d"] == 0.0
    assert feats["news_count_24h"] == 0.0
    assert feats["news_sentiment_7d"] == 0.0


def test_future_headlines_excluded(now: datetime) -> None:
    """Headlines published after as_of_dt are leakage; must be filtered out."""
    headlines = [
        ("Strong positive results announced", now + timedelta(hours=2)),
        ("Past positive news", now - timedelta(hours=5)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_count_24h"] == 1.0
    assert feats["news_count_7d"] == 1.0


def test_momentum_improving(now: datetime) -> None:
    """Recent positive + older negative → positive momentum."""
    headlines = [
        ("Company crushed by terrible loss and scandal", now - timedelta(days=5)),
        ("Excellent quarterly profit growth, beat estimates", now - timedelta(hours=4)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_count_24h"] == 1.0
    assert feats["news_count_7d"] == 2.0
    assert feats["news_sentiment_momentum"] > 0.0


def test_momentum_deteriorating(now: datetime) -> None:
    headlines = [
        ("Excellent quarterly profit growth", now - timedelta(days=5)),
        ("Terrible loss reported, fraud allegations", now - timedelta(hours=4)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_sentiment_momentum"] < 0.0


def test_empty_headline_text_skipped(now: datetime) -> None:
    headlines = [
        ("", now - timedelta(hours=2)),
        ("   ", now - timedelta(hours=3)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_count_24h"] == 0.0
    assert feats["news_count_7d"] == 0.0


def test_24h_window_boundary(now: datetime) -> None:
    """Headline exactly at the 24h boundary is included in both buckets."""
    headlines = [
        ("Strong positive earnings results", now - timedelta(hours=24)),
    ]
    feats = compute_news_features(headlines, now)
    assert feats["news_count_24h"] == 1.0
    assert feats["news_count_7d"] == 1.0
