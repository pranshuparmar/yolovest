"""News aggregator with concurrent fetching and deduplication."""

import asyncio
import logging

from yolovest.models.schemas import NewsArticle
from yolovest.news.base import NewsSource

logger = logging.getLogger(__name__)


class NewsAggregator:
    """Aggregates headlines from multiple NewsSource instances.

    Fetches all sources concurrently, catches per-source errors,
    and deduplicates by content_hash.
    """

    def __init__(self, sources: list[NewsSource]) -> None:
        self.sources = sources

    async def fetch_all(self, symbols: list[str]) -> list[NewsArticle]:
        """Fetch headlines from all sources concurrently, deduplicate results."""
        tasks = [self._safe_fetch(source, symbols) for source in self.sources]
        results = await asyncio.gather(*tasks)
        all_articles: list[NewsArticle] = []
        for articles in results:
            all_articles.extend(articles)
        return self.deduplicate(all_articles)

    async def _safe_fetch(
        self, source: NewsSource, symbols: list[str]
    ) -> list[NewsArticle]:
        """Fetch from a single source, catching and logging any errors."""
        source_name = type(source).__name__
        try:
            articles = await source.fetch_headlines(symbols)
            logger.info("%s returned %d articles", source_name, len(articles))
            return articles
        except Exception:
            logger.warning("%s failed during fetch", source_name, exc_info=True)
            return []

    def deduplicate(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """Deduplicate articles by content_hash.

        For duplicates, keeps the earliest published_at and merges symbol lists.
        """
        seen: dict[str, NewsArticle] = {}
        for article in articles:
            key = article.content_hash
            if key not in seen:
                seen[key] = article
            else:
                existing = seen[key]
                # Merge symbol lists (union, preserving order)
                merged_symbols = list(
                    dict.fromkeys(existing.symbols + article.symbols)
                )
                # Keep the earlier published_at
                earlier_time = existing.published_at
                if (
                    article.published_at is not None
                    and (earlier_time is None or article.published_at < earlier_time)
                ):
                    earlier_time = article.published_at
                seen[key] = existing.model_copy(
                    update={
                        "symbols": merged_symbols,
                        "published_at": earlier_time,
                    }
                )
        return list(seen.values())
