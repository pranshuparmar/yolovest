"""YoloVest entry point.

Loads config, creates context, and starts the heartbeat orchestrator.
CLI args: --config path, --mode paper/live
"""

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from yolovest.watchdog import HeartbeatWatchdog

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
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

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

    # Drop asyncio's transport-layer "socket.send() raised exception"
    # warning. It fires when a WebSocket peer drops without a proper
    # close handshake — the underlying socket is in a half-closed
    # state, our broadcast_ws send hits BrokenPipe at the OS layer,
    # asyncio logs the generic warning, and our application-level
    # try/except discards the dead client one line later. The warning
    # is redundant noise; the prune is happening correctly. A single
    # dashboard tab walking off can produce hundreds of these per
    # heartbeat (one per broadcast event × one per dead client).
    class _DropAsyncioSocketSendWarning(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "socket.send() raised exception" not in record.getMessage()

    asyncio_logger = logging.getLogger("asyncio")
    if not any(
        isinstance(f, _DropAsyncioSocketSendWarning)
        for f in asyncio_logger.filters
    ):
        asyncio_logger.addFilter(_DropAsyncioSocketSendWarning())


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

    async def modify_order(
        self,
        order_id: str,
        *,
        price: float | None = None,
        quantity: int | None = None,
        trigger_price: float | None = None,
        order_type: str | None = None,
    ) -> bool:
        raise NotImplementedError("No broker configured")

    async def get_orders(self) -> list[dict[str, object]]:
        return []

    async def initiate_holdings_auth(
        self, holdings: list[dict[str, object]] | None = None,
    ) -> dict[str, object] | None:
        return None

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

    intraday: MarketDataBase | None = None
    intraday_fallback: MarketDataBase | None = None
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
        return XGBoostSignalModel(model_dir=model_dir, db=db, config=config)
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
        LLMProtocol,
        MarketDataProtocol,
        NotifierProtocol,
    )

    if db is None:
        db = _build_db(config)
    # Shared Kite rate limiter — broker and KiteDataProvider use the same
    # instance so all Kite calls draw from one combined budget.
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
        db=db,
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

async def _load_file_config(args: argparse.Namespace) -> AppConfig:
    """Load config.yaml, exiting the process on failure, and apply the
    CLI --mode override + config-based logging levels."""
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)
    except Exception:
        logger.exception("Failed to load config from %s", args.config)
        sys.exit(1)

    setup_logging(config)
    if args.mode is not None:
        config.mode = args.mode
    return config


async def _apply_persisted_config(db: Any, config: AppConfig) -> AppConfig:
    """Overlay DB-stored config onto the file config (or seed the DB with
    defaults on first boot). Must run BEFORE build_context — toggles like
    kite_data_enabled are frozen into the broker/ingester at build time."""
    if not isinstance(db, Database):
        return config
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
    return config


def _log_startup_summary(config: AppConfig) -> None:
    """Log effective toggles (after DB overrides) + retention sanity note."""
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

    # Config sanity: OHLCV retention shorter than the training window.
    # The nightly database-maintenance FLOORS the daily-OHLCV prune at
    # max(max_training_days, backfill_days), so training history (and
    # exited/delisted symbols) is never silently truncated. We still
    # surface the mismatch as INFO so the user knows their configured
    # `ohlcv_days` is being overridden upward in practice.
    ohlcv_retention = config.database.retention.ohlcv_days
    needed = max(config.retraining.max_training_days, config.market_data.backfill_days)
    if ohlcv_retention < needed:
        logger.info(
            "OHLCV retention (%dd) is shorter than the training/backfill "
            "window (%dd); the nightly maintenance will keep daily OHLCV for "
            "%dd anyway so the model trains on full history. Set "
            "database.retention.ohlcv_days >= %d to make this explicit.",
            ohlcv_retention, needed, needed, needed,
        )


def _resolve_ticker_provider(ctx: AppContext) -> Any:
    """Find (or build) a KiteDataProvider for the ticker's symbol→token
    lookups. The ticker only needs `kite.instruments("NSE")`, NOT the paid
    historical plan — so when kite_data_enabled is off but the websocket is
    on, a standalone provider is stood up just for token resolution."""
    ingester = getattr(ctx.market_data, "providers", None)
    if ingester:
        for p in ingester:
            if type(p).__name__ == "KiteDataProvider":
                return p
    try:
        from yolovest.broker.kite_rate_limiter import KiteRateLimiter
        from yolovest.data.kite_data import KiteDataProvider

        # Local rate limiter — token lookup is one-shot per symbol with a
        # process-wide cache, so dedicated limits are fine.
        provider = KiteDataProvider(
            api_key=ctx.config.broker.api_key.get_secret_value(),
            rate_limiter=KiteRateLimiter(calls_per_second=10.0, concurrency=4),
        )
        # Share the broker's access token so the standalone provider can
        # call /instruments without a separate login. _sync_kite_data_token
        # only walks the ingester chain, so we set this directly.
        provider.set_access_token(getattr(ctx.broker, "_access_token", ""))
        logger.info(
            "Built standalone KiteDataProvider for ticker (kite_data_enabled=False)",
        )
        return provider
    except Exception:
        logger.exception("Failed to build standalone KiteDataProvider for ticker")
        return None


async def _maybe_start_kite_ticker(ctx: AppContext) -> None:
    """Start the KiteTicker WebSocket when enabled + authenticated.

    Bridges on_order_update frames into the same business logic the HTTP
    postback handler runs — WebSocket is the primary push channel (Kite
    postbacks are explicitly best-effort with no retry); the postback
    handler stays as backup and heartbeat ghost-recovery is the
    last-resort reconciler. All three layers are idempotent.
    """
    if not (
        ctx.config.market_data.kite_websocket_enabled
        and getattr(ctx.broker, "_access_token", None)
    ):
        return
    try:
        from yolovest.broker.kite_ticker import KiteTickerClient
        from yolovest.dashboard.postback import _apply_order_postback
        from yolovest.dashboard.ws import broadcast_ws

        kite_provider = _resolve_ticker_provider(ctx)
        if kite_provider is None:
            logger.warning(
                "kite_websocket_enabled but no KiteDataProvider in ingester chain",
            )
            return

        async def _ticker_order_update(order: dict[str, Any]) -> None:
            order_id = str(order.get("order_id") or "")
            status = (order.get("status") or "").upper()
            if not order_id or status not in ("COMPLETE", "CANCELLED", "REJECTED"):
                return
            try:
                await _apply_order_postback(ctx, order_id, status, order)
            except Exception:
                logger.exception(
                    "ticker order_update handler failed for %s", order_id,
                )

        async def _ticker_tick_broadcast(tick: dict[str, Any]) -> None:
            # Throttled in KiteTickerClient itself — this is already at
            # most one call per symbol per second.
            try:
                await broadcast_ws("tick_update", tick)
            except Exception:
                logger.debug("tick broadcast failed", exc_info=True)

        ticker = KiteTickerClient(
            api_key=ctx.config.broker.api_key.get_secret_value(),
            access_token=getattr(ctx.broker, "_access_token", ""),
            kite_data_provider=kite_provider,
            order_update_callback=_ticker_order_update,
            tick_broadcast_callback=_ticker_tick_broadcast,
        )
        await ticker.start()
        ctx.ticker = ticker
        logger.info(
            "KiteTicker started — sub-second LTP cache + real-time order updates active",
        )
    except Exception:
        logger.exception("KiteTicker startup failed; continuing without it")


async def _restore_broker_session(ctx: AppContext) -> None:
    """Restore the persisted Zerodha session and everything gated on it:
    token sync to the data provider, tick-size cache warmup, the optional
    KiteTicker, and the first-boot capital seed."""
    if not isinstance(ctx.broker, ZerodhaBroker):
        return
    restored = await ctx.broker.restore_session()
    _sync_kite_data_token(ctx)

    if not restored:
        return

    # Warm the tick-size cache eagerly so signal_evaluator can snap
    # target / SL to the per-symbol grid on the very first heartbeat.
    # Without this the cache only warms on the first order placement,
    # and signals generated before then would use the 0.05 fallback
    # even for stocks with 0.01 tick. Idempotent + skips when kite is
    # unauthenticated.
    try:
        await ctx.broker._ensure_tick_size_cache()
    except Exception:
        logger.debug("tick-size cache warmup failed (non-fatal)", exc_info=True)

    await _maybe_start_kite_ticker(ctx)

    # First-time bootstrap: if no baseline exists yet, seed it from the
    # broker's current funds (cash + utilised). Subsequent restarts must
    # not overwrite it — the baseline is what the user deposited, and
    # total_capital = initial_capital + all_time_realized_pnl depends on
    # it staying constant. Use /api/capital or /api/capital/sync to reset.
    existing = await ctx.db.get_system_state("initial_capital")
    if not existing:
        try:
            margins = await ctx.broker.get_margins()
            if margins:
                from yolovest.dashboard.helpers import _extract_broker_capital
                broker_capital = _extract_broker_capital(margins)
                if broker_capital > 0:
                    await ctx.db.set_system_state("initial_capital", str(broker_capital))
                    logger.info("Seeded initial capital from Zerodha: %.2f", broker_capital)
        except Exception as e:
            logger.info("Could not seed initial capital from Zerodha: %s", e)


async def _seed_initial_capital_fallback(ctx: AppContext) -> None:
    """If neither broker nor any prior run set a baseline, take it from
    the config's capital.initial_amount."""
    if not isinstance(ctx.db, Database):
        return
    existing = await ctx.db.get_system_state("initial_capital")
    if not existing:
        await ctx.db.set_system_state(
            "initial_capital", str(ctx.config.capital.initial_amount)
        )
        logger.info(
            "Set initial capital to %.0f from config", ctx.config.capital.initial_amount,
        )


def _wire_event_bridge(orchestrator: HeartbeatOrchestrator, ctx: AppContext) -> None:
    """Bridge skill completions + bus events to dashboard WebSocket clients."""
    try:
        from yolovest.dashboard.ws import broadcast_ws
        from yolovest.events import Event

        orchestrator._on_skill_complete = broadcast_ws

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


def _make_config_reloader(ctx: AppContext, config_path: str) -> Callable[[], dict[str, Any]]:
    """Build the reload-config.yaml-at-runtime callable (SIGHUP + API).

    Only reloads settings that are safe to change at runtime; structural
    changes (broker, DB, LLM provider) require restart.
    """

    def reload_config_from_file() -> dict[str, Any]:
        new_config = load_config(config_path)
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

    return reload_config_from_file


def _install_signal_handlers(
    orchestrator: HeartbeatOrchestrator,
    cron_scheduler: CronScheduler,
    reload_config: Callable[[], dict[str, Any]],
    config_path: str,
) -> None:
    """SIGINT/SIGTERM stop the loops; SIGHUP hot-reloads config.yaml."""
    loop = asyncio.get_running_loop()

    def shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        orchestrator.stop()
        cron_scheduler.stop()

    def reload_handler() -> None:
        logger.info("SIGHUP received — reloading config from %s", config_path)
        try:
            reload_config()
        except Exception:
            logger.exception("Config reload failed — keeping previous config")

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)
    # SIGHUP for config reload (Unix only)
    try:
        loop.add_signal_handler(signal.SIGHUP, reload_handler)
    except (ValueError, OSError):
        pass  # SIGHUP not available on Windows


async def async_main(args: argparse.Namespace) -> None:
    """Async entry point: load config, build context, run orchestrator."""
    # Pre-create jugaad-data cache dir to avoid race condition in library
    import os
    os.makedirs(os.path.expanduser("~/.cache/nsehistory-stock"), exist_ok=True)

    config = await _load_file_config(args)

    # Initialize DB and load persisted config BEFORE building broker /
    # market_data — the ingester/broker chain is frozen at build time.
    db = _build_db(config)
    if isinstance(db, Database):
        await db.initialize()
        config = await _apply_persisted_config(db, config)

    logger.info("YoloVest starting in %s mode", config.mode)
    ctx = build_context(config, db=db)
    _log_startup_summary(config)

    await _restore_broker_session(ctx)
    await _seed_initial_capital_fallback(ctx)

    # ML model loading happens in a background task started AFTER the
    # dashboard is up — see `_load_ml_models_background`. Loading pickled
    # XGBoost models can take 30-60s when shadow models have accumulated,
    # and under memory pressure the deserialization stalls long
    # enough that the docker healthcheck times out before /api/health
    # binds. The orchestrator's first heartbeat is gated by a 2s sleep so
    # models almost always finish loading before the first inference.

    # Build orchestrator (skills are instantiated internally) and expose
    # it on ctx so the heartbeat-pipeline skill can invoke run_heartbeat
    # on demand.
    orchestrator = HeartbeatOrchestrator(ctx)
    ctx.orchestrator = orchestrator

    from yolovest.watchdog import HeartbeatWatchdog
    watchdog = HeartbeatWatchdog(ctx)
    orchestrator.set_watchdog(watchdog)

    _wire_event_bridge(orchestrator, ctx)

    # CRON scheduler shares the same skill instances as the heartbeat.
    cron_scheduler = CronScheduler(ctx, orchestrator._skills)

    reload_config = _make_config_reloader(ctx, args.config)
    # Store reload function on app state so the dashboard can call it
    ctx._reload_config = reload_config  # type: ignore[attr-defined]
    _install_signal_handlers(orchestrator, cron_scheduler, reload_config, args.config)

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

    dashboard_task = asyncio.create_task(_start_dashboard(ctx))

    # Keep a reference so the loader task isn't garbage-collected mid-run
    # (asyncio only holds weak refs to tasks); cancelled on shutdown below.
    ml_load_task: asyncio.Task[None] | None = None
    if ctx.ml is not None:
        ml_load_task = asyncio.create_task(_load_ml_models_background(ctx))

    cron_task = asyncio.create_task(_start_cron_scheduler(cron_scheduler))
    watchdog_task = asyncio.create_task(_start_watchdog(watchdog))

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
        if ml_load_task is not None:
            ml_load_task.cancel()
        if ctx.ticker is not None:
            try:
                await ctx.ticker.stop()
            except Exception:
                logger.debug("ticker stop failed", exc_info=True)

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
            except (TimeoutError, Exception):
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


async def _load_ml_models_background(ctx: AppContext) -> None:
    """Load production + shadow ML models in a background task so the
    /api/health endpoint binds before pickle deserialization stalls
    the event loop. Loading is sequential to avoid memory spikes from
    multiple XGBoost models being unpickled in parallel.

    The production slot loads the REGISTRY's production version — the
    model_versions row promotion (shadow gate or manual) marked
    `status='production'`. The registry is binding: every retrain saves
    its candidate as a newer artifact with `status='shadow'`, so
    "newest file on disk" would put an un-vetted candidate on the live
    account at every restart. The latest artifact is only a bootstrap
    fallback when the registry has no production row (fresh install) or
    the production artifact is missing from disk (keep trading, loudly).

    Models that aren't loaded yet when generate-signals runs will
    cause the skill to fall back to "no model available"; the
    orchestrator's first heartbeat is gated by a 2-second sleep so
    this is unlikely in practice, but if a shadow has dozens of MB
    of trees and a host is under memory pressure, it's possible.
    """
    if ctx.ml is None:
        return
    for model_type in ("intraday", "swing"):
        prod_version: str | None = None
        try:
            prod = await ctx.db.get_production_model(model_type)
            if prod and prod.get("version"):
                prod_version = str(prod["version"])
        except Exception:
            logger.warning(
                "Registry lookup failed for %s; falling back to the "
                "latest artifact on disk",
                model_type, exc_info=True,
            )
        try:
            await ctx.ml.load_model(model_type, prod_version)
            logger.info(
                "Loaded %s model at startup: %s", model_type,
                prod_version or "latest artifact (no production row in registry)",
            )
            continue
        except FileNotFoundError:
            if prod_version is None:
                logger.info(
                    "No saved %s model found, will be available after model-retrain",
                    model_type,
                )
                continue
            logger.warning(
                "Registry production %s model %s has no artifact on disk — "
                "falling back to the latest artifact so trading continues. "
                "Re-promote a model (or retrain) to restore registry state.",
                model_type, prod_version,
            )
        except Exception as e:
            logger.warning("Failed to load %s model at startup: %s", model_type, e)
            continue
        # Fallback: registry pointed at a missing artifact.
        try:
            await ctx.ml.load_model(model_type)
        except FileNotFoundError:
            logger.info(
                "No saved %s model found, will be available after model-retrain",
                model_type,
            )
        except Exception as e:
            logger.warning("Failed to load %s model at startup: %s", model_type, e)
    try:
        shadow_models = await ctx.db.get_all_shadow_models()
        for shadow in shadow_models:
            try:
                await ctx.ml.load_shadow_model(
                    shadow["model_type"], shadow["version"],
                )
            except FileNotFoundError:
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
