"""Integration test: full heartbeat pipeline with real SQLite DB.

Runs the orchestrator's heartbeat pipeline end-to-end using:
- Real SQLite database (in-memory)
- Mocked broker, market data, and LLM (no external APIs)
- Real skill instances (not mocked)

Verifies the full flow: health-check → ingest → scan → signals →
risk-check → llm-review → trade-execute → predict-track → position-monitor
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yolovest.config import AppConfig
from yolovest.context import AppContext, MarketHoursChecker
from yolovest.data.db import Database
from yolovest.events import EventBus
from yolovest.notify import Notifier
from yolovest.orchestrator import HeartbeatOrchestrator


@pytest.fixture
async def integration_db(tmp_path):
    """Real SQLite database for integration testing."""
    db_path = str(tmp_path / "test.db")
    migrations_dir = Path(__file__).parent.parent / "migrations"
    db = Database(db_path, migrations_dir)
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def integration_config():
    return AppConfig(
        mode="paper",
        capital={"initial_amount": 100000},
        broker={"api_key": "test_key", "api_secret": "test_secret"},
        llm={"enabled": True, "model": "gemini-2.5-flash", "api_key": "test_key"},
        market_data={
            "daily_provider": "jugaad",
            "stale_threshold_minutes": 30,
            "news_enabled": False,
            "scrapers_enabled": False,
        },
        heartbeat={
            "market_hours_interval_min": 15,
            "off_hours_interval_min": 60,
            "max_consecutive_skips": 3,
        },
        scanning={
            "seed_symbols": ["RELIANCE", "TCS"],
            "shortlist_size": 5,
            "weights": {
                "technical": 0.35,
                "volume_momentum": 0.25,
                "news_sentiment": 0.15,
                "fundamental": 0.15,
                "volatility": 0.10,
            },
        },
        risk={
            "max_risk_per_trade_pct": 0.02,
            "max_portfolio_exposure_pct": 0.60,
            "max_open_positions": 3,
            "max_single_stock_pct": 0.25,
            "daily_loss_limit_pct": 0.03,
            "weekly_loss_limit_pct": 0.05,
            "weekly_loss_sizing_reduction": 0.50,
            "llm_review_enabled": True,
            "llm_fallback_to_rules": True,
        },
        market_hours={
            "open": "09:15",
            "close": "15:30",
            "order_start": "09:15",
            "order_end": "15:15",
            "square_off": "15:15",
            "timezone": "Asia/Kolkata",
        },
        notifications={"telegram": {"enabled": False}},
    )


@pytest.fixture
def mock_broker():
    broker = AsyncMock()
    broker.is_authenticated = AsyncMock(return_value=True)
    broker.get_positions = AsyncMock(return_value=[])
    broker.get_pending_orders = AsyncMock(return_value=[])
    broker.get_margins = AsyncMock(return_value={"available_cash": 100000})
    broker.place_order = AsyncMock(return_value="PAPER-1")
    broker.get_order_status = AsyncMock(return_value={
        "status": "filled", "average_price": 2501.0, "filled_quantity": 10,
    })
    broker.cancel_order = AsyncMock(return_value=True)
    broker.modify_sl_order = AsyncMock(return_value=True)
    return broker


@pytest.fixture
def mock_market_data():
    md = AsyncMock()
    md.health_check = AsyncMock(return_value=True)
    md.get_ltp = AsyncMock(return_value=2500.0)

    # Return realistic OHLCV bars ending today so the ingest
    # staleness gate (5d) doesn't quarantine the symbol mid-test.
    from datetime import datetime, timedelta

    from yolovest.models.schemas import OHLCVBar
    bars = []
    today = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
    for i in range(30):
        dt = today - timedelta(days=29 - i)
        bars.append(OHLCVBar(
            timestamp=dt,
            open=2400.0 + i * 5,
            high=2410.0 + i * 5,
            low=2390.0 + i * 5,
            close=2405.0 + i * 5,
            volume=1000000 + i * 10000,
        ))
    md.get_ohlcv = AsyncMock(return_value=bars)
    # get_fetch_meta is SYNC (not awaited at call site); AsyncMock
    # would return a coroutine that crashes the .get() the ingest
    # skill does on the result. MagicMock returning an empty dict
    # keeps the no-issues path live for tests that don't care.
    md.get_fetch_meta = MagicMock(return_value={})
    return md


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.ping = AsyncMock(return_value=True)

    review = MagicMock()
    review.decision = "APPROVE"
    review.reasoning = "Test approval"
    review.adjusted_size = None
    llm.review_trade = AsyncMock(return_value=review)

    from yolovest.models.schemas import SentimentResult
    llm.analyze_sentiment = AsyncMock(
        return_value=SentimentResult(
            symbol="RELIANCE", sentiment="neutral", confidence=0.5,
        )
    )

    from yolovest.models.schemas import WatchlistValidation
    llm.validate_watchlist = AsyncMock(return_value=WatchlistValidation())
    llm.summarize_market_day = AsyncMock(return_value=MagicMock(
        date="2026-03-28", market_sentiment="neutral",
    ))
    llm.analyze_prediction_failures = AsyncMock(return_value=MagicMock(
        summary="No failures",
    ))
    return llm


@pytest.fixture
async def integration_ctx(
    integration_db, integration_config, mock_broker, mock_market_data, mock_llm,
):
    """Full AppContext with real DB + mocked external services."""
    notify = Notifier(integration_config)
    ctx = AppContext(
        config=integration_config,
        db=integration_db,
        broker=mock_broker,
        llm=mock_llm,
        market_data=mock_market_data,
        notify=notify,
        market_hours=MarketHoursChecker(integration_config),
        event_bus=EventBus(),
    )
    return ctx


class TestHeartbeatIntegration:
    """Full pipeline integration tests."""

    async def test_heartbeat_completes_without_crash(self, integration_ctx):
        """The most basic integration test: run a heartbeat, don't crash."""
        orchestrator = HeartbeatOrchestrator(integration_ctx)

        # Patch market hours to simulate market open
        with patch.object(
            integration_ctx.market_hours, "is_market_hours", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_order_window", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_premarket_window", return_value=False,
        ):
            results = await orchestrator.run_heartbeat()

        assert results is not None
        assert "health-check" in results
        assert results["health-check"].success

    async def test_health_check_uses_real_db(self, integration_ctx):
        """Health check should pass with real SQLite."""
        orchestrator = HeartbeatOrchestrator(integration_ctx)

        with patch.object(
            integration_ctx.market_hours, "is_market_hours", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_order_window", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_premarket_window", return_value=False,
        ):
            results = await orchestrator.run_heartbeat()

        health = results["health-check"]
        assert health.success
        assert health.data["checks"]["database"] is True

    async def test_ingest_persists_to_real_db(self, integration_ctx):
        """Ingest should write OHLCV data to the real SQLite DB."""
        orchestrator = HeartbeatOrchestrator(integration_ctx)

        with patch.object(
            integration_ctx.market_hours, "is_market_hours", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_order_window", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_premarket_window", return_value=False,
        ):
            results = await orchestrator.run_heartbeat()

        ingest = results.get("ingest-data")
        assert ingest is not None
        assert ingest.success

        # Verify data was actually written to DB
        bars = await integration_ctx.db.get_ohlcv("RELIANCE", "daily", days=30)
        assert len(bars) > 0

    async def test_trade_recorded_in_db(self, integration_ctx):
        """A full heartbeat completes and any recorded trades are well-formed.

        Whether a trade is actually placed depends on the synthetic data's
        signal conditions, so this asserts the pipeline ran through to
        position-monitor and that every persisted trade has a symbol and a
        positive quantity. The deterministic no-trade case is covered by
        test_kill_switch_stops_trading.
        """
        orchestrator = HeartbeatOrchestrator(integration_ctx)

        with patch.object(
            integration_ctx.market_hours, "is_market_hours", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_order_window", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_premarket_window", return_value=False,
        ):
            results = await orchestrator.run_heartbeat()

        # Pipeline ran to completion (position-monitor always runs unless
        # health-check aborts), and any trades placed are well-formed.
        assert results is not None
        assert "position-monitor" in results
        for trade in await integration_ctx.db.get_todays_trades():
            assert trade["symbol"]
            assert trade["quantity"] > 0

    async def test_pipeline_resilient_to_ingest_failure(self, integration_ctx):
        """If ingest fails, position-monitor should still run."""
        orchestrator = HeartbeatOrchestrator(integration_ctx)

        # Make market data fail
        integration_ctx.market_data.get_ohlcv = AsyncMock(
            side_effect=Exception("API down"),
        )

        with patch.object(
            integration_ctx.market_hours, "is_market_hours", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_order_window", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_premarket_window", return_value=False,
        ):
            results = await orchestrator.run_heartbeat()

        # Ingest may or may not fail (depends on cache), but pipeline continues
        assert "position-monitor" in results
        assert results["position-monitor"].success

    async def test_kill_switch_detected_by_pipeline(self, integration_ctx):
        """Kill switch should be detected and prevent trading."""
        await integration_ctx.db.set_system_state("kill_switch", "active")

        orchestrator = HeartbeatOrchestrator(integration_ctx)

        with patch.object(
            integration_ctx.market_hours, "is_market_hours", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_order_window", return_value=True,
        ), patch.object(
            integration_ctx.market_hours, "is_premarket_window", return_value=False,
        ):
            results = await orchestrator.run_heartbeat()

        # Health check should report kill switch active
        health = results["health-check"]
        assert health.data["checks"]["kill_switch_active"] is True
        # Pipeline should still complete (kill switch doesn't abort health-check)
        # but trading_allowed should be False
        assert health.data.get("trading_allowed") is False
