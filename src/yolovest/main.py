"""YoloVest entry point.

Loads config, creates context, and starts the heartbeat orchestrator.
CLI args: --config path, --mode paper/live
"""

import argparse
import asyncio
import logging
import signal
import sys

from yolovest.config import AppConfig, load_config
from yolovest.context import AppContext, MarketHoursChecker
from yolovest.events import EventBus
from yolovest.notify import ConsoleNotifier
from yolovest.orchestrator import HeartbeatOrchestrator

logger = logging.getLogger("yolovest")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="yolovest",
        description="YoloVest — Autonomous AI-driven Indian stock trading platform",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["paper", "live"],
        default=None,
        help="Trading mode override (default: from config file)",
    )
    return parser.parse_args()


def setup_logging() -> None:
    """Configure logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


class _StubDB:
    """Minimal database stub for Phase 0 (no real DB yet)."""

    async def health_check(self) -> bool:
        return True

    async def is_kill_switch_active(self) -> bool:
        return False

    async def get_open_positions(self) -> list[object]:
        return []


class _StubBroker:
    """Minimal broker stub for Phase 0 (no real broker yet)."""

    async def place_order(self, *args: object, **kwargs: object) -> str:
        raise NotImplementedError("No broker configured")

    async def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("No broker configured")

    async def get_order_status(self, order_id: str) -> dict[str, object]:
        raise NotImplementedError("No broker configured")

    async def get_positions(self) -> list[dict[str, object]]:
        return []

    async def get_pending_orders(self) -> list[dict[str, object]]:
        return []

    async def is_authenticated(self) -> bool:
        return False

    async def get_margins(self) -> dict[str, object]:
        return {}


class _StubLLM:
    """Minimal LLM stub for Phase 0 (no real LLM yet)."""

    async def ping(self) -> bool:
        return False

    async def review_trade(self, context: object) -> object:
        raise NotImplementedError("No LLM configured")

    async def analyze_sentiment(self, symbol: str, headlines: list[str]) -> object:
        raise NotImplementedError("No LLM configured")

    async def summarize_with_web_grounding(self, prompt: str) -> object:
        raise NotImplementedError("No LLM configured")

    async def validate_watchlist(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError("No LLM configured")

    async def summarize_market_day(self) -> object:
        raise NotImplementedError("No LLM configured")

    async def analyze_prediction_failures(self, failures: list[dict[str, object]]) -> object:
        raise NotImplementedError("No LLM configured")


class _StubMarketData:
    """Minimal market data stub for Phase 0 (no real providers yet)."""

    async def get_ohlcv(self, symbol: str, interval: str, days: int = 30) -> list[object]:
        return []

    async def get_quote(self, symbol: str) -> dict[str, object]:
        raise NotImplementedError("No market data configured")

    async def health_check(self) -> bool:
        return False


def build_context(config: AppConfig) -> AppContext:
    """Build the application context with stubs for unimplemented backends.

    In Phase 0, all backends are stubs. As concrete implementations are built
    (Phase 1+), they replace these stubs.
    """
    return AppContext(
        config=config,
        db=_StubDB(),
        broker=_StubBroker(),
        llm=_StubLLM(),
        market_data=_StubMarketData(),
        notify=ConsoleNotifier(enabled=True),
        market_hours=MarketHoursChecker(config),
        event_bus=EventBus(),
    )


async def async_main(args: argparse.Namespace) -> None:
    """Async entry point: load config, build context, run orchestrator."""
    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)
    except Exception:
        logger.exception("Failed to load config from %s", args.config)
        sys.exit(1)

    # CLI mode override
    if args.mode is not None:
        config.mode = args.mode

    logger.info("YoloVest starting in %s mode", config.mode)

    # Build context
    ctx = build_context(config)

    # Build orchestrator
    orchestrator = HeartbeatOrchestrator(ctx)

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()

    def shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        orchestrator.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    # Start
    await ctx.notify.send(
        f"YoloVest started in {config.mode} mode. "
        f"Heartbeat interval: {config.heartbeat.market_hours_interval_min}min (market hours), "
        f"{config.heartbeat.off_hours_interval_min}min (off hours)."
    )

    await orchestrator.start()

    logger.info("YoloVest shutdown complete")


def main() -> None:
    """Synchronous entry point."""
    setup_logging()
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
