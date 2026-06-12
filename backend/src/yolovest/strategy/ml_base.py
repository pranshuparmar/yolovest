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
    async def predict_intraday(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction:
        """Generate an intraday trading signal for a symbol."""
        ...

    @abstractmethod
    async def predict_swing(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction:
        """Generate a swing trading signal for a symbol."""
        ...

    def get_effective_thresholds(
        self, model_type: str,
    ) -> dict[str, float] | None:
        """Return the model's currently-applied (buy, sell) probability
        thresholds with any post-load adjustments (e.g. asymmetry cap).

        Default returns None — callers should treat that as "use the
        default argmax behaviour". Subclasses that ship a tuned-
        threshold model override this; balanced-mode signal generation
        uses the returned numbers to compare margins-above-threshold
        across the intraday and swing models instead of raw confidence
        (which is not comparable when the two models are trained on
        different class balances).
        """
        return None

    @abstractmethod
    async def train(
        self, model_type: str, X: Any, y: Any, params: dict[str, Any]  # noqa: N803
    ) -> dict[str, Any]:
        """Train a model and return metrics dict."""
        ...

    @abstractmethod
    async def save_model(self, model_type: str, metrics: dict[str, Any]) -> str:
        """Serialize trained model to disk. Returns version string."""
        ...

    @abstractmethod
    async def load_model(self, model_type: str, version: str | None = None) -> None:
        """Load a model from disk into the appropriate slot."""
        ...

    @abstractmethod
    async def get_production_metrics(self, model_type: str) -> dict[str, Any]:
        """Retrieve production performance metrics from DB."""
        ...

    @abstractmethod
    async def deploy_shadow(self, model_type: str, version: str, days: int) -> None:
        """Deploy a model version in shadow mode for validation."""
        ...

    # Shadow model methods (default implementations for backward compatibility)

    def has_shadow(self, model_type: str) -> bool:
        """Check if a shadow model is loaded."""
        return False

    def get_shadow_version(self, model_type: str) -> str | None:
        """Version string of the model in the shadow slot, if any."""
        return None

    def clear_model(self, model_type: str) -> None:
        """Empty the production slot for a lane (parked-lane safety)."""


    def clear_shadow(self, model_type: str) -> None:
        """Unload shadow model."""

    async def load_shadow_model(self, model_type: str, version: str | None = None) -> None:
        """Load a model into the shadow slot."""

    async def predict_shadow_intraday(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction | None:
        """Run shadow intraday model. Returns None if no shadow loaded."""
        return None

    async def predict_shadow_swing(
        self, symbol: str, features: dict[str, Any], *, current_price: float | None = None,
    ) -> MLPrediction | None:
        """Run shadow swing model. Returns None if no shadow loaded."""
        return None
