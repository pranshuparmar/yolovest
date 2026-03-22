"""Abstract base class for ML model inference and lifecycle.

Defines the interface for ML signal models used by the generate-signals skill.
Implementations handle training, prediction, serialization, and shadow deployment.
"""

from abc import ABC, abstractmethod
from typing import Any

from yolovest.models.schemas import MLPrediction


class MLBase(ABC):
    """Abstract ML model interface for trading signal generation."""

    @abstractmethod
    async def predict_intraday(self, symbol: str, features: dict) -> MLPrediction:
        """Generate an intraday trading signal for a symbol."""
        ...

    @abstractmethod
    async def predict_swing(self, symbol: str, features: dict) -> MLPrediction:
        """Generate a swing trading signal for a symbol."""
        ...

    @abstractmethod
    async def train(self, model_type: str, X: Any, y: Any, params: dict) -> dict:
        """Train a model and return metrics dict."""
        ...

    @abstractmethod
    async def save_model(self, model_type: str, metrics: dict) -> str:
        """Serialize trained model to disk. Returns version string."""
        ...

    @abstractmethod
    async def load_model(self, model_type: str, version: str | None = None) -> None:
        """Load a model from disk into the appropriate slot."""
        ...

    @abstractmethod
    async def get_production_metrics(self, model_type: str) -> dict:
        """Retrieve production performance metrics from DB."""
        ...

    @abstractmethod
    async def deploy_shadow(self, model_type: str, version: str, days: int) -> None:
        """Deploy a model version in shadow mode for validation."""
        ...
