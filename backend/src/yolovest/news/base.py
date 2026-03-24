"""Abstract base class for news sources."""

from abc import ABC, abstractmethod

from yolovest.models.schemas import NewsArticle


class NewsSource(ABC):
    """Base class for all news/data scrapers."""

    @abstractmethod
    async def fetch_headlines(self, symbols: list[str]) -> list[NewsArticle]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...
