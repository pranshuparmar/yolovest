"""YoloVest entry point.

Loads config, creates context, and starts the heartbeat orchestrator.
CLI args: --config path, --mode paper/live
"""

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from yolovest.broker.zerodha import ZerodhaBroker
from yolovest.config import AppConfig, load_config
from yolovest.context import AppContext, MarketHoursChecker
from yolovest.cron_scheduler import CronScheduler
from yolovest.data.db import Database
from yolovest.data.ingester import MarketDataIngester
from yolovest.data.jugaad import JugaadDataProvider
from yolovest.data.tvfeed import TVDatafeedProvider
from yolovest.data.yfinance_provider import YFinanceProvider
from yolovest.events import EventBus
from yolovest.llm.gemini import GeminiLLM
from yolovest.notify import Notifier
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
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the web dashboard",
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

    async def authenticate(self, request_token: str) -> bool:
        return False

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

    async def modify_sl_order(self, order_id: str, new_trigger_price: float) -> bool:
        raise NotImplementedError("No broker configured")

    def get_login_url(self) -> str:
        return ""


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

    async def get_ltp(self, symbol: str) -> float:
        raise NotImplementedError("No market data configured")

    async def health_check(self) -> bool:
        return False


def _build_db(config: AppConfig) -> Database | _StubDB:
    """Build database — real if path configured, stub otherwise."""
    return Database(config.database.path)


def _build_broker(config: AppConfig) -> ZerodhaBroker | _StubBroker:
    """Build broker — real if API keys set, stub otherwise."""
    if config.broker.api_key and config.broker.api_key != "${KITE_API_KEY}":
        return ZerodhaBroker(
            api_key=config.broker.api_key,
            api_secret=config.broker.api_secret,
            mode=config.mode,
            paper_slippage_pct=config.execution.paper_slippage_pct,
            max_retries=config.execution.max_order_retries,
            retry_base_delay=float(config.execution.retry_base_delay_sec),
        )
    return _StubBroker()


def _build_llm(config: AppConfig) -> GeminiLLM | _StubLLM:
    """Build LLM — real if API key set, stub otherwise."""
    if config.llm.api_key and config.llm.api_key != "${GEMINI_API_KEY}":
        return GeminiLLM(api_key=config.llm.api_key, model=config.llm.model)
    return _StubLLM()


def _build_market_data(config: AppConfig) -> MarketDataIngester | _StubMarketData:
    """Build market data ingester with provider fallback chain.

    If kite_data_enabled is True and broker API keys are set, Kite Connect
    is added as the primary provider (FR-2.1e). Requires paid data plan.
    """
    from yolovest.data.base import MarketDataBase

    daily_providers: list[MarketDataBase] = []

    # FR-2.1e: Kite data plan as primary when enabled
    if config.market_data.kite_data_enabled:
        api_key = config.broker.api_key
        if api_key and api_key != "${KITE_API_KEY}":
            try:
                from yolovest.data.kite_data import KiteDataProvider

                kite_provider = KiteDataProvider(api_key=api_key)
                daily_providers.append(kite_provider)
                logger.info("Kite Connect data provider enabled as primary")
            except Exception as e:
                logger.warning("Failed to initialize Kite data provider: %s", e)

    if config.market_data.daily_provider == "jugaad":
        daily_providers.append(JugaadDataProvider())
    if config.market_data.daily_fallback == "yfinance":
        daily_providers.append(YFinanceProvider())

    if not daily_providers:
        return _StubMarketData()

    intraday = None
    # Kite handles intraday too, so skip tvDatafeed if Kite is primary
    if config.market_data.kite_data_enabled and daily_providers:
        from yolovest.data.kite_data import KiteDataProvider

        if isinstance(daily_providers[0], KiteDataProvider):
            intraday = daily_providers[0]  # Kite handles all intervals
    if intraday is None and config.market_data.intraday_provider == "tvdatafeed":
        intraday = TVDatafeedProvider()

    return MarketDataIngester(
        daily_providers=daily_providers,
        intraday_provider=intraday,
        stale_threshold_minutes=config.market_data.stale_threshold_minutes,
    )


def _build_memory(db: Any) -> Any:
    """Build agent memory persistence layer (FR-1.5)."""
    try:
        from yolovest.memory import AgentMemory

        return AgentMemory(db)
    except Exception:
        logger.warning("Failed to build agent memory")
        return None


def _build_ml(config: AppConfig, db: Any) -> Any:
    """Build ML provider (XGBoost signal model)."""
    try:
        from yolovest.strategy.ml_signal import XGBoostSignalModel

        model_dir = getattr(config.strategy, "model_dir", "./models")
        return XGBoostSignalModel(model_dir=model_dir, db=db)
    except Exception:
        logger.warning("Failed to build ML provider, signals will be unavailable")
        return None


def _build_news_aggregator() -> Any:
    """Build news aggregator with all available news sources."""
    try:
        from yolovest.news.aggregator import NewsAggregator
        from yolovest.news.et_markets import ETMarketsSource
        from yolovest.news.livemint import LiveMintSource
        from yolovest.news.moneycontrol import MoneyControlSource

        sources = [MoneyControlSource(), ETMarketsSource(), LiveMintSource()]
        return NewsAggregator(sources)
    except Exception:
        logger.warning("Failed to build news aggregator, news will be unavailable")
        return None


def build_context(config: AppConfig) -> AppContext:
    """Build the application context with real implementations where configured.

    Falls back to stubs when API keys or providers are not configured.
    """
    from typing import cast

    from yolovest.context import (
        BrokerProtocol,
        DatabaseProtocol,
        LLMProtocol,
        MarketDataProtocol,
        NotifierProtocol,
    )

    db = _build_db(config)
    return AppContext(
        config=config,
        db=cast(DatabaseProtocol, db),
        broker=cast(BrokerProtocol, _build_broker(config)),
        llm=cast(LLMProtocol, _build_llm(config)),
        market_data=cast(MarketDataProtocol, _build_market_data(config)),
        notify=cast(NotifierProtocol, Notifier(config)),
        market_hours=MarketHoursChecker(config),
        event_bus=EventBus(),
        ml=_build_ml(config, db),
        news_aggregator=_build_news_aggregator(),
        memory=_build_memory(db),
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

    # Initialize database if real (not stub)
    if isinstance(ctx.db, Database):
        await ctx.db.initialize()

    # Load production ML models from disk (if any exist)
    if ctx.ml is not None:
        for model_type in ("intraday", "swing"):
            try:
                await ctx.ml.load_model(model_type)
                logger.info("Loaded production %s model at startup", model_type)
            except FileNotFoundError:
                logger.info("No saved %s model found, will be available after model-retrain", model_type)
            except Exception as e:
                logger.warning("Failed to load %s model at startup: %s", model_type, e)

    # Build orchestrator (skills are instantiated internally)
    orchestrator = HeartbeatOrchestrator(ctx)

    # Wire WebSocket broadcasting for skill completion notifications
    # and event bus → WebSocket bridge for real-time dashboard updates
    try:
        from yolovest.dashboard.app import broadcast_ws
        from yolovest.events import Event

        orchestrator._on_skill_complete = broadcast_ws

        # Bridge: any event published on the bus gets broadcast to WebSocket clients
        async def _ws_bridge(event: Event) -> None:
            await broadcast_ws(event.event_type, event.data)

        for event_type in (
            "heartbeat_started", "heartbeat_completed",
            "signal_generated", "trade_executed", "trade_exit",
            "position_updated", "portfolio_pnl",
            "kill_switch_activated",
            "ingest_progress", "retrain_progress",
        ):
            ctx.event_bus.subscribe(event_type, _ws_bridge)
    except Exception:
        pass

    # Build CRON scheduler sharing the same skill instances
    cron_scheduler = CronScheduler(ctx, orchestrator._skills)

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()

    def shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        orchestrator.stop()
        cron_scheduler.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    # Start Telegram bot if enabled
    telegram_task = None
    telegram_bot = None
    if ctx.config.notifications.telegram.enabled:
        from yolovest.telegram_bot import TelegramBot

        telegram_bot = TelegramBot(ctx)
        # Wire bot into notifier for message sending
        if hasattr(ctx.notify, "set_telegram_bot"):
            ctx.notify.set_telegram_bot(telegram_bot)
        telegram_task = asyncio.create_task(_start_telegram(telegram_bot))

    # Start dashboard if enabled
    dashboard_task = None
    if not args.no_dashboard:
        dashboard_task = asyncio.create_task(_start_dashboard(ctx))

    # Start CRON scheduler as background task
    cron_task = asyncio.create_task(_start_cron_scheduler(cron_scheduler))

    # Start
    await ctx.notify.send(
        f"YoloVest started in {config.mode} mode. "
        f"Heartbeat interval: {config.heartbeat.market_hours_interval_min}min (market hours), "
        f"{config.heartbeat.off_hours_interval_min}min (off hours)."
        + (f"\nDashboard: http://{config.dashboard.host}:{config.dashboard.port}"
           if not args.no_dashboard else "")
    )

    try:
        await orchestrator.start()
    finally:
        # 1. Stop cron scheduler
        cron_scheduler.stop()
        cron_task.cancel()

        # 2. Cancel telegram task to interrupt the long-poll HTTP request,
        #    then call stop() to cleanly shut down the updater.
        if telegram_task:
            telegram_task.cancel()
            try:
                await telegram_task
            except (asyncio.CancelledError, Exception):
                pass
        if telegram_bot:
            try:
                await asyncio.wait_for(telegram_bot.stop(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Telegram bot stop timed out, forcing shutdown")

        # 3. Cancel dashboard
        if dashboard_task:
            dashboard_task.cancel()
            try:
                await dashboard_task
            except (asyncio.CancelledError, Exception):
                pass

        # 4. Close database
        if isinstance(ctx.db, Database):
            await ctx.db.close()

        # 5. Force-cancel any remaining tasks (e.g. orphaned updater polling)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        # Give cancelled tasks a chance to finish
        remaining = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)

    logger.info("YoloVest shutdown complete")


async def _start_telegram(bot: Any) -> None:
    """Start the Telegram bot in background."""
    try:
        await bot.start()
    except Exception:
        logger.exception("Telegram bot failed to start")


async def _start_cron_scheduler(scheduler: CronScheduler) -> None:
    """Start the CRON scheduler in background."""
    try:
        await scheduler.start()
    except Exception:
        logger.exception("CRON scheduler failed")


async def _start_dashboard(ctx: AppContext) -> None:
    """Start the FastAPI dashboard in background."""
    import uvicorn

    from yolovest.dashboard.app import create_app

    app = create_app(ctx)
    config = uvicorn.Config(
        app,
        host=ctx.config.dashboard.host,
        port=ctx.config.dashboard.port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    """Synchronous entry point."""
    setup_logging()
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
