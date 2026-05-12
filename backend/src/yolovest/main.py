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
from yolovest.config import AppConfig, apply_db_config, get_db_editable_defaults, load_config
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
    return parser.parse_args()


def setup_logging(config: "AppConfig | None" = None) -> None:
    """Configure logging for the application.

    Called twice: once at startup with defaults (before config loads),
    then again after config loads to apply configured levels.
    """
    from pathlib import Path
    from logging.handlers import RotatingFileHandler
    from yolovest.config import LoggingConfig

    cfg = config.log if config else LoggingConfig()
    level = getattr(logging, cfg.level.upper(), logging.INFO)
    file_level = getattr(logging, cfg.file_level.upper(), logging.INFO)

    log_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Only add handlers on first call (avoid duplicates on reconfigure)
    if not root.handlers:
        # Console handler
        console = logging.StreamHandler()
        console.setFormatter(log_fmt)
        console.setLevel(level)
        root.addHandler(console)

        # File handler
        log_dir = Path(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "yolovest.log",
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
        )
        file_handler.setFormatter(log_fmt)
        file_handler.setLevel(file_level)
        root.addHandler(file_handler)

        # In-memory ring buffer for live log viewing from dashboard
        from yolovest.log_buffer import LogBuffer
        buffer_handler = LogBuffer(maxlen=500)
        buffer_handler.setFormatter(log_fmt)
        root.addHandler(buffer_handler)
    else:
        # Reconfigure: update levels on existing handlers
        for handler in root.handlers:
            if isinstance(handler, RotatingFileHandler):
                handler.setLevel(file_level)
            elif isinstance(handler, logging.StreamHandler):
                handler.setLevel(level)

    # Suppress verbose third-party logs (leak tokens and API keys)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


class _StubDB:
    """Minimal database stub when no real DB yet)."""

    async def health_check(self) -> bool:
        return True

    async def is_kill_switch_active(self) -> bool:
        return False

    async def get_open_positions(self) -> list[object]:
        return []


class _StubBroker:
    """Minimal broker stub when no real broker yet)."""

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
    """Minimal LLM stub when no real LLM configured.

    Returns safe no-op defaults instead of raising, so callers without
    try/except won't crash the pipeline.
    """

    async def ping(self) -> bool:
        return False

    async def review_trade(self, context: object) -> object:
        from yolovest.models.schemas import TradeReview
        return TradeReview(decision="APPROVE", reasoning="LLM not configured — auto-approved")

    async def analyze_sentiment(self, symbol: str, headlines: list[str]) -> object:
        from yolovest.models.schemas import SentimentResult
        return SentimentResult(symbol=symbol, sentiment="neutral", confidence=0.0)

    async def summarize_with_web_grounding(self, prompt: str) -> object:
        from yolovest.models.schemas import WebGroundingResult
        return WebGroundingResult(query=prompt, summary="LLM not configured")

    async def validate_watchlist(self, *args: object, **kwargs: object) -> object:
        from yolovest.models.schemas import WatchlistValidation
        return WatchlistValidation()

    async def summarize_market_day(self) -> object:
        from yolovest.models.schemas import MarketDaySummary
        from yolovest.timezone import now_ist
        return MarketDaySummary(
            date=now_ist().strftime("%Y-%m-%d"),
            market_sentiment="neutral",
        )

    async def analyze_prediction_failures(self, failures: list[dict[str, object]]) -> object:
        from yolovest.models.schemas import FailureAnalysis
        return FailureAnalysis(summary="LLM not configured — no analysis")


class _StubMarketData:
    """Minimal market data stub when no real providers yet)."""

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


def _build_broker(
    config: AppConfig, rate_limiter: Any = None,
) -> ZerodhaBroker | _StubBroker:
    """Build broker — real if API keys set, stub otherwise."""
    api_key = config.broker.api_key.get_secret_value()
    api_secret = config.broker.api_secret.get_secret_value()
    if api_key and api_key != "${KITE_API_KEY}":
        return ZerodhaBroker(
            api_key=api_key,
            api_secret=api_secret,
            mode=config.mode,
            paper_slippage_pct=config.execution.paper_slippage_pct,
            max_retries=config.execution.max_order_retries,
            retry_base_delay=float(config.execution.retry_base_delay_sec),
            kite_data_enabled=config.market_data.kite_data_enabled,
            rate_limiter=rate_limiter,
        )
    return _StubBroker()


def _build_llm(config: AppConfig) -> GeminiLLM | _StubLLM:
    """Build LLM — real if enabled + API key set, stub otherwise."""
    llm_key = config.llm.api_key.get_secret_value()
    if (config.llm.enabled and llm_key and llm_key != "${GEMINI_API_KEY}"):
        return GeminiLLM(api_key=llm_key, model=config.llm.model)
    if not config.llm.enabled:
        logger.info("LLM disabled via config (llm.enabled=false)")
    return _StubLLM()


def _build_market_data(
    config: AppConfig, rate_limiter: Any = None,
) -> MarketDataIngester | _StubMarketData:
    """Build market data ingester with provider fallback chain.

    If kite_data_enabled is True and broker API keys are set, Kite Connect
    is added as the primary provider. Requires paid data plan.
    """
    from yolovest.data.base import MarketDataBase

    daily_providers: list[MarketDataBase] = []

    # Kite data plan as primary when enabled
    if config.market_data.kite_data_enabled:
        kite_key = config.broker.api_key.get_secret_value()
        if kite_key and kite_key != "${KITE_API_KEY}":
            try:
                from yolovest.data.kite_data import KiteDataProvider

                kite_provider = KiteDataProvider(
                    api_key=kite_key, rate_limiter=rate_limiter,
                )
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
    intraday_fallback = None
    # Kite handles intraday too — use it as primary with tvDatafeed as fallback
    if config.market_data.kite_data_enabled and daily_providers:
        from yolovest.data.kite_data import KiteDataProvider

        if isinstance(daily_providers[0], KiteDataProvider):
            intraday = daily_providers[0]
            intraday_fallback = TVDatafeedProvider()
    if intraday is None and config.market_data.intraday_provider == "tvdatafeed":
        intraday = TVDatafeedProvider()

    return MarketDataIngester(
        daily_providers=daily_providers,
        intraday_provider=intraday,
        intraday_fallback=intraday_fallback,
        stale_threshold_minutes=config.market_data.stale_threshold_minutes,
    )


def _build_memory(db: Any) -> Any:
    """Build agent memory persistence layer."""
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


def _build_news_aggregator(config: AppConfig) -> Any:
    """Build news aggregator with all available news sources."""
    if not config.market_data.news_enabled:
        logger.info("News sources disabled via config (market_data.news_enabled=false)")
        return None
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


def build_context(config: AppConfig, db: Any = None) -> AppContext:
    """Build the application context with real implementations where configured.

    Falls back to stubs when API keys or providers are not configured.
    If `db` is provided, it's used directly (allowing the caller to load DB
    config values before broker/market_data are constructed). Otherwise a
    fresh DB instance is built from config.
    """
    from typing import cast

    from yolovest.context import (
        BrokerProtocol,
        DatabaseProtocol,
        LLMProtocol,
        MarketDataProtocol,
        NotifierProtocol,
    )

    if db is None:
        db = _build_db(config)
    # One shared rate limiter for all Kite calls — broker (orders, profile,
    # holdings) and KiteDataProvider (quote, historical) draw from the same
    # 10 req/s + 8 concurrent budget so they can't combine to exceed quota.
    from yolovest.broker.kite_rate_limiter import KiteRateLimiter
    kite_rate_limiter = KiteRateLimiter(calls_per_second=10.0, concurrency=8)
    broker = _build_broker(config, rate_limiter=kite_rate_limiter)
    # Pass DB to broker for token persistence (if real broker)
    market_data = _build_market_data(config, rate_limiter=kite_rate_limiter)
    if isinstance(broker, ZerodhaBroker):
        broker._db = db
        broker._market_data = market_data
    return AppContext(
        config=config,
        db=cast(DatabaseProtocol, db),
        broker=cast(BrokerProtocol, broker),
        llm=cast(LLMProtocol, _build_llm(config)),
        market_data=cast(MarketDataProtocol, market_data),
        notify=cast(NotifierProtocol, Notifier(config)),
        market_hours=MarketHoursChecker(config),
        event_bus=EventBus(),
        ml=_build_ml(config, db),
        news_aggregator=_build_news_aggregator(config),
        memory=_build_memory(db),
    )


def _sync_kite_data_token(ctx: "AppContext") -> None:
    """Sync broker's access token to KiteDataProvider if enabled.

    Called after broker auth/restore so the data provider can make API calls.
    """
    from yolovest.broker.zerodha import ZerodhaBroker

    if not isinstance(ctx.broker, ZerodhaBroker):
        return
    token = getattr(ctx.broker, "_access_token", None)
    if not token or token == "paper_token":
        return

    # Find KiteDataProvider in the ingester's provider chain
    ingester = ctx.market_data
    if not hasattr(ingester, "_daily_providers"):
        return
    for provider in ingester._daily_providers:
        try:
            from yolovest.data.kite_data import KiteDataProvider
            if isinstance(provider, KiteDataProvider):
                provider.set_access_token(token)
                logger.info("Synced broker access token to Kite data provider")
                break
        except ImportError:
            break
    # Also sync to intraday provider if it's the same Kite instance
    if hasattr(ingester, "_intraday_provider") and ingester._intraday_provider is not None:
        try:
            from yolovest.data.kite_data import KiteDataProvider
            if isinstance(ingester._intraday_provider, KiteDataProvider):
                ingester._intraday_provider.set_access_token(token)
        except ImportError:
            pass


async def async_main(args: argparse.Namespace) -> None:
    """Async entry point: load config, build context, run orchestrator."""
    # Pre-create jugaad-data cache dir to avoid race condition in library
    import os
    os.makedirs(os.path.expanduser("~/.cache/nsehistory-stock"), exist_ok=True)

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)
    except Exception:
        logger.exception("Failed to load config from %s", args.config)
        sys.exit(1)

    # Reconfigure logging with config-based levels
    setup_logging(config)

    # CLI mode override
    if args.mode is not None:
        config.mode = args.mode

    # Initialize DB and load persisted config BEFORE building broker/market_data.
    # Without this, toggles like kite_data_enabled stored in the DB would not
    # take effect until restart (the ingester/broker chain is frozen at build).
    db = _build_db(config)
    if isinstance(db, Database):
        await db.initialize()
        try:
            if await db.is_config_empty():
                defaults = get_db_editable_defaults()
                await db.set_config_bulk(defaults)
                logger.info("Populated %d config defaults into DB", len(defaults))
            else:
                db_values = await db.get_all_config()
                config = apply_db_config(config, db_values)
                logger.info("Loaded %d config values from DB", len(db_values))
        except Exception:
            logger.warning("Failed to load config from DB, using file defaults", exc_info=True)

    # Log effective mode AFTER DB config has been applied
    logger.info("YoloVest starting in %s mode", config.mode)

    # Build context with the now-effective config and the pre-built DB
    ctx = build_context(config, db=db)

    # Log effective config (after DB overrides are applied)
    logger.info(
        "Config toggles: mode=%s, llm.enabled=%s, telegram.enabled=%s, "
        "news_enabled=%s, scrapers_enabled=%s, kite_data_enabled=%s, "
        "llm_review_enabled=%s, transaction_mode=%s",
        config.mode,
        config.llm.enabled,
        config.notifications.telegram.enabled,
        config.market_data.news_enabled,
        config.market_data.scrapers_enabled,
        config.market_data.kite_data_enabled,
        config.risk.llm_review_enabled,
        config.execution.transaction_mode,
    )

    # Restore Zerodha session from persisted access token
    if isinstance(ctx.broker, ZerodhaBroker):
        restored = await ctx.broker.restore_session()

        # Sync Kite data provider with broker's access token
        _sync_kite_data_token(ctx)

        # Sync capital from Zerodha if session was restored
        if restored:
            try:
                margins = await ctx.broker.get_margins()
                if margins:
                    from yolovest.dashboard.app import _extract_broker_capital
                    broker_capital = _extract_broker_capital(margins)
                    if broker_capital > 0:
                        await ctx.db.set_system_state("initial_capital", str(broker_capital))
                        logger.info("Synced capital from Zerodha: %.2f", broker_capital)
            except Exception as e:
                logger.info("Could not sync capital from Zerodha: %s", e)

    # Sync initial capital from config → DB (so portfolio reads the configured value)
    if isinstance(ctx.db, Database):
        existing = await ctx.db.get_system_state("initial_capital")
        if not existing:
            await ctx.db.set_system_state(
                "initial_capital", str(ctx.config.capital.initial_amount)
            )
            logger.info("Set initial capital to %.0f from config", ctx.config.capital.initial_amount)

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

        # Load shadow models (if any are in shadow status in DB)
        try:
            shadow_models = await ctx.db.get_all_shadow_models()
            for shadow in shadow_models:
                try:
                    await ctx.ml.load_shadow_model(
                        shadow["model_type"], shadow["version"],
                    )
                except FileNotFoundError:
                    # .pkl file missing — revert to retired
                    logger.warning(
                        "Shadow %s model %s has no .pkl file — reverting to retired",
                        shadow["model_type"], shadow["version"],
                    )
                    await ctx.db.retire_model(shadow["model_type"], shadow["version"])
                except Exception as e:
                    logger.warning(
                        "Failed to load shadow %s model %s: %s",
                        shadow["model_type"], shadow["version"], e,
                    )
        except Exception:
            logger.warning("Failed to load shadow models", exc_info=True)

    # Build orchestrator (skills are instantiated internally)
    orchestrator = HeartbeatOrchestrator(ctx)

    # Build heartbeat watchdog
    from yolovest.watchdog import HeartbeatWatchdog
    watchdog = HeartbeatWatchdog(ctx)
    orchestrator.set_watchdog(watchdog)

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
        logger.warning("Failed to set up WebSocket event bridge", exc_info=True)

    # Build CRON scheduler sharing the same skill instances
    cron_scheduler = CronScheduler(ctx, orchestrator._skills)

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()

    def reload_config_from_file() -> dict[str, Any]:
        """Reload config.yaml and apply safe runtime changes.

        Only reloads settings that are safe to change at runtime.
        Structural changes (broker, DB, LLM provider) require restart.
        Returns dict with status and reloaded sections.
        """
        new_config = load_config(args.config)
        # Safe to hot-reload: risk params, scanning weights, heartbeat timing,
        # market hours, execution params, transaction costs, alert toggles
        ctx.config.risk = new_config.risk
        ctx.config.scanning = new_config.scanning
        ctx.config.heartbeat = new_config.heartbeat
        ctx.config.market_hours = new_config.market_hours
        ctx.config.execution = new_config.execution
        ctx.config.transaction_costs = new_config.transaction_costs
        ctx.config.strategy = new_config.strategy
        ctx.config.notifications = new_config.notifications
        ctx.config.reports = new_config.reports
        ctx.config.retraining = new_config.retraining
        ctx.config.market_data = new_config.market_data
        ctx.config.dashboard = new_config.dashboard
        ctx.config.news_digest = new_config.news_digest
        # Update market hours checker with new config
        ctx.market_hours = MarketHoursChecker(ctx.config)
        # Sync mode to broker
        if new_config.mode != ctx.config.mode:
            ctx.config.mode = new_config.mode
            if hasattr(ctx.broker, "_mode"):
                ctx.broker._mode = new_config.mode
                logger.info("Broker mode synced to: %s", new_config.mode)
        reloaded = [
            "risk", "scanning", "heartbeat", "market_hours", "execution",
            "transaction_costs", "strategy", "notifications", "reports",
            "retraining", "market_data", "dashboard",
        ]
        logger.info("Config reloaded: %s", ", ".join(reloaded))
        return {"status": "ok", "reloaded": reloaded}

    # Store reload function on app state so the dashboard can call it
    ctx._reload_config = reload_config_from_file  # type: ignore[attr-defined]

    def shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        orchestrator.stop()
        cron_scheduler.stop()

    def reload_handler() -> None:
        """SIGHUP handler: reload config.yaml without restart."""
        logger.info("SIGHUP received — reloading config from %s", args.config)
        try:
            reload_config_from_file()
        except Exception:
            logger.exception("Config reload failed — keeping previous config")

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)
    # SIGHUP for config reload (Unix only)
    try:
        loop.add_signal_handler(signal.SIGHUP, reload_handler)
    except (ValueError, OSError):
        pass  # SIGHUP not available on Windows

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

    # Start dashboard
    dashboard_task = asyncio.create_task(_start_dashboard(ctx))

    # Start CRON scheduler as background task
    cron_task = asyncio.create_task(_start_cron_scheduler(cron_scheduler))

    # Start heartbeat watchdog
    watchdog_task = asyncio.create_task(_start_watchdog(watchdog))

    # Start
    import os
    domain = os.environ.get("DOMAIN")
    dashboard_url = (
        f"https://{domain}" if domain
        else f"http://{config.dashboard.host}:{config.dashboard.port}"
    )

    await ctx.notify.send(
        f"YoloVest started in {ctx.config.mode} mode. "
        f"Heartbeat interval: {ctx.config.heartbeat.market_hours_interval_min}min (market hours), "
        f"{ctx.config.heartbeat.off_hours_interval_min}min (off hours)."
        + f"\nDashboard: {dashboard_url}"
    )

    try:
        await orchestrator.start()
    finally:
        # 1. Stop cron scheduler and watchdog
        cron_scheduler.stop()
        cron_task.cancel()
        watchdog.stop()
        watchdog_task.cancel()

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


async def _start_watchdog(watchdog: "HeartbeatWatchdog") -> None:
    """Start the heartbeat watchdog in background."""
    try:
        await watchdog.start()
    except Exception:
        logger.exception("Heartbeat watchdog failed")


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
