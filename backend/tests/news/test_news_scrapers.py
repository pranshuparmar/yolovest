"""Tests for news scrapers and aggregator.

All HTTP/RSS calls are mocked — no real network access.
"""

import time
from datetime import datetime
from unittest.mock import patch

from yolovest.models.schemas import NewsArticle
from yolovest.news.aggregator import NewsAggregator
from yolovest.news.base import NewsSource
from yolovest.news.et_markets import ETMarketsSource
from yolovest.news.livemint import LiveMintSource
from yolovest.news.moneycontrol import MoneyControlSource
from yolovest.news.nse_official import NSEOfficialSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article(
    headline: str,
    source: str = "test",
    symbols: list[str] | None = None,
    published_at: datetime | None = None,
) -> NewsArticle:
    return NewsArticle(
        headline=headline,
        source=source,
        symbols=symbols or [],
        published_at=published_at,
    )


def _make_feed_dict(entries: list[dict]) -> dict:
    """Build a minimal feedparser-like dict."""
    return {"entries": entries, "feed": {"title": "Test Feed"}}


def _make_entry(
    title: str,
    link: str = "https://example.com/article",
    published_parsed: tuple | None = None,
) -> dict:
    entry = {"title": title, "link": link}
    if published_parsed is not None:
        entry["published_parsed"] = published_parsed
    return entry


class FakeSource(NewsSource):
    """A controllable fake source for testing the aggregator."""

    def __init__(self, articles: list[NewsArticle] | Exception) -> None:
        self._articles = articles

    async def fetch_headlines(self, symbols: list[str]) -> list[NewsArticle]:
        if isinstance(self._articles, Exception):
            raise self._articles
        return self._articles

    async def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# NewsArticle.content_hash auto-computation
# ---------------------------------------------------------------------------


class TestNewsArticleHash:
    def test_hash_auto_computed(self):
        article = _make_article("RELIANCE shares surge 5%")
        assert article.content_hash
        assert len(article.content_hash) == 64  # SHA256 hex

    def test_hash_deterministic(self):
        a1 = _make_article("RELIANCE shares surge 5%")
        a2 = _make_article("RELIANCE shares surge 5%")
        assert a1.content_hash == a2.content_hash

    def test_hash_case_insensitive(self):
        a1 = _make_article("Reliance Shares Surge")
        a2 = _make_article("reliance shares surge")
        assert a1.content_hash == a2.content_hash

    def test_hash_strips_whitespace(self):
        a1 = _make_article("  Test Headline  ")
        a2 = _make_article("Test Headline")
        assert a1.content_hash == a2.content_hash

    def test_different_headlines_different_hash(self):
        a1 = _make_article("Headline A")
        a2 = _make_article("Headline B")
        assert a1.content_hash != a2.content_hash

    def test_explicit_hash_preserved(self):
        article = NewsArticle(
            headline="Test",
            source="test",
            content_hash="explicit_hash",
        )
        assert article.content_hash == "explicit_hash"


# ---------------------------------------------------------------------------
# NewsAggregator.deduplicate
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_no_duplicates(self):
        agg = NewsAggregator(sources=[])
        articles = [
            _make_article("Headline A"),
            _make_article("Headline B"),
        ]
        result = agg.deduplicate(articles)
        assert len(result) == 2

    def test_removes_exact_duplicates(self):
        agg = NewsAggregator(sources=[])
        articles = [
            _make_article("Same Headline", source="source_a"),
            _make_article("Same Headline", source="source_b"),
        ]
        result = agg.deduplicate(articles)
        assert len(result) == 1

    def test_case_insensitive_dedup(self):
        agg = NewsAggregator(sources=[])
        articles = [
            _make_article("RELIANCE Surges", source="a"),
            _make_article("reliance surges", source="b"),
        ]
        result = agg.deduplicate(articles)
        assert len(result) == 1

    def test_keeps_earliest_published_at(self):
        agg = NewsAggregator(sources=[])
        early = datetime(2026, 1, 1, 9, 0)
        late = datetime(2026, 1, 1, 12, 0)
        articles = [
            _make_article("Same News", published_at=late),
            _make_article("Same News", published_at=early),
        ]
        result = agg.deduplicate(articles)
        assert len(result) == 1
        assert result[0].published_at == early

    def test_merges_symbol_lists(self):
        agg = NewsAggregator(sources=[])
        articles = [
            _make_article("Market News", symbols=["RELIANCE"]),
            _make_article("Market News", symbols=["TCS", "RELIANCE"]),
        ]
        result = agg.deduplicate(articles)
        assert len(result) == 1
        assert set(result[0].symbols) == {"RELIANCE", "TCS"}

    def test_keeps_none_published_at_if_only_none(self):
        agg = NewsAggregator(sources=[])
        articles = [
            _make_article("No date", published_at=None),
            _make_article("No date", published_at=None),
        ]
        result = agg.deduplicate(articles)
        assert len(result) == 1
        assert result[0].published_at is None

    def test_prefers_date_over_none(self):
        agg = NewsAggregator(sources=[])
        dt = datetime(2026, 3, 15, 10, 0)
        articles = [
            _make_article("News", published_at=None),
            _make_article("News", published_at=dt),
        ]
        result = agg.deduplicate(articles)
        assert len(result) == 1
        assert result[0].published_at == dt


# ---------------------------------------------------------------------------
# NewsAggregator.fetch_all
# ---------------------------------------------------------------------------


class TestFetchAll:
    async def test_aggregates_from_multiple_sources(self):
        s1 = FakeSource([_make_article("A from s1")])
        s2 = FakeSource([_make_article("B from s2")])
        agg = NewsAggregator(sources=[s1, s2])
        result = await agg.fetch_all(["RELIANCE"])
        assert len(result) == 2

    async def test_failed_source_does_not_crash(self):
        s_ok = FakeSource([_make_article("Good article")])
        s_fail = FakeSource(RuntimeError("Connection timeout"))
        agg = NewsAggregator(sources=[s_ok, s_fail])
        result = await agg.fetch_all(["RELIANCE"])
        assert len(result) == 1
        assert result[0].headline == "Good article"

    async def test_all_sources_fail(self):
        s1 = FakeSource(RuntimeError("Fail 1"))
        s2 = FakeSource(RuntimeError("Fail 2"))
        agg = NewsAggregator(sources=[s1, s2])
        result = await agg.fetch_all(["RELIANCE"])
        assert result == []

    async def test_deduplicates_across_sources(self):
        s1 = FakeSource([_make_article("Same headline")])
        s2 = FakeSource([_make_article("Same headline")])
        agg = NewsAggregator(sources=[s1, s2])
        result = await agg.fetch_all(["RELIANCE"])
        assert len(result) == 1

    async def test_empty_symbols_list(self):
        s1 = FakeSource([_make_article("General news")])
        agg = NewsAggregator(sources=[s1])
        result = await agg.fetch_all([])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# MoneyControl RSS scraper — _parse_feed
# ---------------------------------------------------------------------------


class TestMoneyControlParseFeed:
    def test_parses_entries(self):
        source = MoneyControlSource()
        feed = _make_feed_dict([
            _make_entry("RELIANCE Q3 results beat estimates"),
            _make_entry("TCS wins mega deal"),
        ])
        articles = source._parse_feed(feed, ["RELIANCE", "TCS"])
        assert len(articles) == 2
        assert articles[0].source == "moneycontrol"
        assert articles[0].headline == "RELIANCE Q3 results beat estimates"
        assert articles[0].symbols == ["RELIANCE"]
        assert articles[1].symbols == ["TCS"]

    def test_symbol_matching_case_insensitive(self):
        source = MoneyControlSource()
        feed = _make_feed_dict([
            _make_entry("Reliance Industries posts strong results"),
        ])
        articles = source._parse_feed(feed, ["RELIANCE"])
        assert len(articles) == 1
        assert articles[0].symbols == ["RELIANCE"]

    def test_no_matching_symbols(self):
        source = MoneyControlSource()
        feed = _make_feed_dict([
            _make_entry("Market sentiment turns positive"),
        ])
        articles = source._parse_feed(feed, ["RELIANCE", "TCS"])
        assert len(articles) == 1
        assert articles[0].symbols == []

    def test_empty_feed(self):
        source = MoneyControlSource()
        feed = _make_feed_dict([])
        articles = source._parse_feed(feed, ["RELIANCE"])
        assert articles == []

    def test_entry_with_published_date(self):
        source = MoneyControlSource()
        feed = _make_feed_dict([
            _make_entry(
                "Test headline",
                published_parsed=time.struct_time((2026, 3, 15, 10, 30, 0, 0, 0, 0)),
            ),
        ])
        articles = source._parse_feed(feed, [])
        assert articles[0].published_at == datetime(2026, 3, 15, 10, 30, 0)

    def test_entry_without_title_skipped(self):
        source = MoneyControlSource()
        feed = _make_feed_dict([
            {"title": "", "link": "https://example.com"},
            _make_entry("Valid headline"),
        ])
        articles = source._parse_feed(feed, [])
        assert len(articles) == 1
        assert articles[0].headline == "Valid headline"

    def test_url_captured(self):
        source = MoneyControlSource()
        feed = _make_feed_dict([
            _make_entry("Test", link="https://moneycontrol.com/news/123"),
        ])
        articles = source._parse_feed(feed, [])
        assert articles[0].url == "https://moneycontrol.com/news/123"


# ---------------------------------------------------------------------------
# ET Markets RSS scraper — _parse_feed
# ---------------------------------------------------------------------------


class TestETMarketsParseFeed:
    def test_parses_entries(self):
        source = ETMarketsSource()
        feed = _make_feed_dict([
            _make_entry("Nifty hits all-time high"),
            _make_entry("INFY reports record revenue"),
        ])
        articles = source._parse_feed(feed, ["INFY"])
        assert len(articles) == 2
        assert articles[0].source == "et_markets"
        assert articles[1].symbols == ["INFY"]

    def test_empty_feed(self):
        source = ETMarketsSource()
        feed = _make_feed_dict([])
        assert source._parse_feed(feed, ["TCS"]) == []


# ---------------------------------------------------------------------------
# LiveMint RSS scraper — _parse_feed
# ---------------------------------------------------------------------------


class TestLiveMintParseFeed:
    def test_parses_entries(self):
        source = LiveMintSource()
        feed = _make_feed_dict([
            _make_entry("HDFC Bank merger update"),
        ])
        articles = source._parse_feed(feed, ["HDFC"])
        assert len(articles) == 1
        assert articles[0].source == "livemint"
        assert articles[0].symbols == ["HDFC"]

    def test_empty_feed(self):
        source = LiveMintSource()
        feed = _make_feed_dict([])
        assert source._parse_feed(feed, []) == []


# ---------------------------------------------------------------------------
# NSE Official — basic smoke tests (full tests in test_nse_official.py)
# ---------------------------------------------------------------------------


class TestNSEOfficialBasic:
    def test_nse_source_is_news_source(self):
        source = NSEOfficialSource()
        assert isinstance(source, NewsSource)

    def test_nse_source_default_session_is_none(self):
        source = NSEOfficialSource()
        assert source._session is None
        assert source._owns_session is True
        assert source._cookies_initialized is False


# ---------------------------------------------------------------------------
# fetch_headlines with mocked feedparser (integration-ish)
# ---------------------------------------------------------------------------


class TestFetchHeadlinesMocked:
    async def test_moneycontrol_fetch_headlines(self):
        source = MoneyControlSource()
        fake_feed = _make_feed_dict([
            _make_entry("RELIANCE Q3 beat"),
        ])
        with patch(
            "yolovest.news.moneycontrol._parse_rss",
            return_value=fake_feed,
        ):
            articles = await source.fetch_headlines(["RELIANCE"])
        assert len(articles) == 1
        assert articles[0].source == "moneycontrol"

    async def test_moneycontrol_fetch_handles_exception(self):
        source = MoneyControlSource()
        with patch(
            "yolovest.news.moneycontrol._parse_rss",
            side_effect=Exception("Network error"),
        ):
            articles = await source.fetch_headlines(["RELIANCE"])
        assert articles == []

    async def test_et_markets_fetch_headlines(self):
        source = ETMarketsSource()
        fake_feed = _make_feed_dict([
            _make_entry("TCS wins big contract"),
        ])
        with patch(
            "yolovest.news.et_markets._parse_rss",
            return_value=fake_feed,
        ):
            articles = await source.fetch_headlines(["TCS"])
        assert len(articles) == 1
        assert articles[0].source == "et_markets"

    async def test_livemint_fetch_headlines(self):
        source = LiveMintSource()
        fake_feed = _make_feed_dict([
            _make_entry("HDFC Bank reports earnings"),
        ])
        with patch(
            "yolovest.news.livemint._parse_rss",
            return_value=fake_feed,
        ):
            articles = await source.fetch_headlines(["HDFC"])
        assert len(articles) == 1
        assert articles[0].source == "livemint"
