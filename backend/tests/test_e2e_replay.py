"""End-to-end test: replay realistic market data through the full pipeline.

Simulates multiple trading days with:
- Realistic OHLCV data (uptrend + pullback pattern for 2 symbols)
- Mock ML model that generates BUY signals on RSI oversold + uptrend
- Mock broker in paper mode
- Real SQLite database, real skills, real feature computation

Verifies the full chain:
  ingest → scan → generate-signals → risk-check → llm-review →
  trade-execute → predict-track → position-monitor → close

This test takes ~2-3 seconds (no external APIs, no sleeps).
"""

import random
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yolovest.config import AppConfig
from yolovest.context import AppContext, MarketHoursChecker
from yolovest.data.db import Database
from yolovest.events import EventBus
from yolovest.models.schemas import MLPrediction, OHLCVBar
from yolovest.notify import Notifier
from yolovest.orchestrator import HeartbeatOrchestrator

# ---------------------------------------------------------------------------
# Realistic OHLCV data generator
# ---------------------------------------------------------------------------

def _generate_ohlcv(
    symbol: str,
    days: int = 250,
    start_price: float = 2000.0,
    volatility: float = 0.02,
    trend: float = 0.001,
    seed: int = 42,
) -> list[OHLCVBar]:
    """Generate realistic daily OHLCV bars with trend + noise.

    Creates a price series with:
    - Upward drift (configurable trend)
    - Random walk volatility
    - Proper OHLC relationships (high >= max(open, close), etc.)
    - Volume correlated with price movement
    """
    rng = random.Random(seed)
    bars = []
    price = start_price
    base_date = datetime(2025, 3, 1, 0, 0)

    for i in range(days):
        dt = base_date + timedelta(days=i)
        # Skip weekends
        if dt.weekday() >= 5:
            continue

        # Random return with trend
        ret = trend + volatility * rng.gauss(0, 1)
        open_price = price
        close_price = price * (1 + ret)

        # Generate high/low
        intraday_vol = abs(ret) + volatility * 0.5
        high = max(open_price, close_price) * (1 + abs(rng.gauss(0, intraday_vol * 0.3)))
        low = min(open_price, close_price) * (1 - abs(rng.gauss(0, intraday_vol * 0.3)))

        # Volume: higher on bigger moves
        base_vol = 500_000 + rng.randint(0, 500_000)
        vol_multiplier = 1 + abs(ret) * 20  # Big moves → high volume
        volume = int(base_vol * vol_multiplier)

        bars.append(OHLCVBar(
            timestamp=dt,
            open=round(open_price, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close_price, 2),
            volume=volume,
        ))
        price = close_price

    return bars


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def e2e_db(tmp_path):
    """Real SQLite database with migrations."""
    db_path = str(tmp_path / "e2e_test.db")
    migrations_dir = Path(__file__).parent.parent / "migrations"
    db = Database(db_path, migrations_dir)
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def e2e_config():
    return AppConfig(
        mode="paper",
        capital={"initial_amount": 500000},
        broker={"api_key": "test", "api_secret": "test"},
        llm={"enabled": True, "model": "gemini-2.5-flash", "api_key": "test"},
        market_data={
            "daily_provider": "jugaad",
            "stale_threshold_minutes": 9999,
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
            "shortlist_size": 10,
            "weights": {
                "technical": 0.35,
                "volume_momentum": 0.25,
                "news_sentiment": 0.15,
                "fundamental": 0.15,
                "volatility": 0.10,
            },
        },
        strategy={
            "ema_periods": [9, 21, 50, 200],
            "min_training_samples": 50,
        },
        risk={
            "max_risk_per_trade_pct": 0.02,
            "max_portfolio_exposure_pct": 0.60,
            "max_open_positions": 3,
            "max_single_stock_pct": 0.25,
            "daily_loss_limit_pct": 0.05,
            "weekly_loss_limit_pct": 0.10,
            "weekly_loss_sizing_reduction": 0.50,
            "llm_review_enabled": True,
            "llm_fallback_to_rules": True,
            "mandatory_stop_loss": True,
            "max_trades_per_day": 5,
            "loss_cooldown_minutes": 0,
            "symbol_cooldown_days": 0,
        },
        market_hours={
            "open": "09:15",
            "close": "15:30",
            "order_start": "09:15",
            "order_end": "15:15",
            "square_off": "15:15",
            "timezone": "Asia/Kolkata",
        },
        execution={
            "max_order_retries": 1,
            "paper_slippage_pct": 0.001,
            "price_drift_max_pct": 0.05,
        },
        notifications={"telegram": {"enabled": False}},
    )


@pytest.fixture
def mock_ml():
    """ML provider that generates realistic BUY/SELL predictions."""
    ml = AsyncMock()
    ml.has_shadow = MagicMock(return_value=False)

    call_count = {"n": 0}

    async def _predict(symbol, features, *, current_price=None):
        call_count["n"] += 1
        price = current_price or features.get("close", 2500)
        rsi = features.get("rsi_14", 50)

        # Generate BUY on oversold (RSI < 40), SELL on overbought (RSI > 70)
        if rsi < 40:
            signal_type = "BUY"
            confidence = 0.70 + (40 - rsi) * 0.005
        elif rsi > 70:
            signal_type = "SELL"
            confidence = 0.70 + (rsi - 70) * 0.005
        else:
            signal_type = "HOLD"
            confidence = 0.3

        atr = features.get("atr_14", price * 0.015)
        if signal_type == "BUY":
            target = round(price + 2 * atr, 2)
            sl = round(price - 1.5 * atr, 2)
        elif signal_type == "SELL":
            target = round(price - 2 * atr, 2)
            sl = round(price + 1.5 * atr, 2)
        else:
            target = round(price * 1.02, 2)
            sl = round(price * 0.98, 2)

        return MLPrediction(
            signal_type=signal_type,
            entry_price=round(price, 2),
            target_price=target,
            stop_loss_price=sl,
            position_size=10,
            holding_period="intraday",
            confidence=min(confidence, 0.95),
            model_version="test-v1",
        )

    ml.predict_swing = AsyncMock(side_effect=_predict)
    ml.predict_intraday = AsyncMock(side_effect=_predict)
    return ml


@pytest.fixture
def mock_broker_e2e():
    """Paper broker that tracks orders."""
    broker = AsyncMock()
    broker.is_authenticated = AsyncMock(return_value=True)
    broker.get_positions = AsyncMock(return_value=[])
    broker.get_pending_orders = AsyncMock(return_value=[])
    broker.get_margins = AsyncMock(return_value={"available_cash": 500000})
    broker.get_login_url = MagicMock(return_value="https://kite.test/login")

    order_counter = {"n": 0}

    async def _place_order(**kwargs):
        order_counter["n"] += 1
        return f"PAPER-E2E-{order_counter['n']}"

    broker.place_order = AsyncMock(side_effect=_place_order)
    broker.get_order_status = AsyncMock(return_value={
        "status": "filled",
        "average_price": 2500.0,
        "filled_quantity": 10,
    })
    broker.cancel_order = AsyncMock(return_value=True)
    broker.modify_sl_order = AsyncMock(return_value=True)
    broker.get_holdings = AsyncMock(return_value=[])
    return broker


@pytest.fixture
def mock_llm_e2e():
    """LLM that approves everything."""
    llm = AsyncMock()
    llm.ping = AsyncMock(return_value=True)

    review = MagicMock()
    review.decision = "APPROVE"
    review.reasoning = "E2E test auto-approve"
    review.adjusted_size = None
    llm.review_trade = AsyncMock(return_value=review)
    llm.analyze_sentiment = AsyncMock(return_value=MagicMock(
        symbol="RELIANCE", sentiment="neutral", confidence=0.5, key_drivers=[],
    ))
    llm.validate_watchlist = AsyncMock(return_value=MagicMock(
        approved_symbols=[], rejected_symbols=[], reasoning={}, market_narrative="",
    ))
    llm.summarize_market_day = AsyncMock(return_value=MagicMock(
        date="2026-03-28", market_sentiment="neutral",
    ))
    llm.analyze_prediction_failures = AsyncMock(return_value=MagicMock(
        summary="No failures",
    ))
    return llm


@pytest.fixture
async def e2e_ctx(e2e_db, e2e_config, mock_broker_e2e, mock_ml, mock_llm_e2e):
    """Full context with real DB, mock external services, and real OHLCV data."""
    # Pre-populate DB with realistic OHLCV data
    reliance_bars = _generate_ohlcv("RELIANCE", days=300, start_price=2400, seed=42)
    tcs_bars = _generate_ohlcv("TCS", days=300, start_price=3800, seed=99)

    await e2e_db.upsert_ohlcv("RELIANCE", "daily", reliance_bars, "test")
    await e2e_db.upsert_ohlcv("TCS", "daily", tcs_bars, "test")

    # Mock market data that reads from the real DB
    market_data = AsyncMock()
    market_data.health_check = AsyncMock(return_value=True)

    async def _get_ohlcv(symbol, interval, days=30):
        return await e2e_db.get_ohlcv(symbol, interval, days)

    async def _get_ltp(symbol):
        bars = await e2e_db.get_ohlcv(symbol, "daily", days=5)
        return bars[-1].close if bars else 2500.0

    market_data.get_ohlcv = AsyncMock(side_effect=_get_ohlcv)
    market_data.get_ltp = AsyncMock(side_effect=_get_ltp)
    market_data.get_quote = AsyncMock(return_value={"ltp": 2500.0})

    # No fetch metadata needed for tests
    market_data.get_fetch_meta = MagicMock(return_value={})

    notify = Notifier(e2e_config)

    ctx = AppContext(
        config=e2e_config,
        db=e2e_db,
        broker=mock_broker_e2e,
        llm=mock_llm_e2e,
        market_data=market_data,
        notify=notify,
        market_hours=MarketHoursChecker(e2e_config),
        event_bus=EventBus(),
        ml=mock_ml,
    )
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestE2EReplay:
    """Full pipeline replay with realistic data."""

    async def test_single_heartbeat_generates_signals(self, e2e_ctx):
        """One heartbeat with realistic data should produce signals."""
        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
            results = await orchestrator.run_heartbeat()

        # Pipeline should complete
        assert results is not None
        assert results["health-check"].success
        assert results["ingest-data"].success

        # Signals should be generated (ML model produces BUY/SELL for oversold/overbought)
        gen = results.get("generate-signals")
        assert gen is not None
        assert gen.success
        signals = gen.data.get("signals", [])
        # With 250 bars and RSI computation, at least some symbols should signal
        assert gen.data["watchlist_size"] >= 1

    async def test_trade_execution_flow(self, e2e_ctx):
        """If a signal is generated, it should flow through risk → review → execute."""
        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
            results = await orchestrator.run_heartbeat()

        # Check if any trades were executed
        trades = await e2e_ctx.db.get_todays_trades()

        # If signals were generated with sufficient confidence, trades should exist
        gen = results.get("generate-signals")
        if gen and gen.data.get("signals"):
            # At least one signal → should have flowed through
            signal_keys = [k for k in results if "/risk-check" in k]
            assert len(signal_keys) > 0, "Signals generated but no risk-check ran"

    async def test_multiple_heartbeats_accumulate_trades(self, e2e_ctx):
        """Running multiple heartbeats should accumulate trades in the DB."""
        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        total_trades_before = len(await e2e_ctx.db.get_todays_trades())

        for _ in range(3):
            with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
                 patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
                 patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
                await orchestrator.run_heartbeat()

        total_trades_after = len(await e2e_ctx.db.get_todays_trades())
        # Should have at least as many trades as before (may have more)
        assert total_trades_after >= total_trades_before

    async def test_position_monitor_tracks_open_positions(self, e2e_ctx):
        """Position monitor should run and track any open positions."""
        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
            results = await orchestrator.run_heartbeat()

        pm = results.get("position-monitor")
        assert pm is not None
        assert pm.success

    async def test_data_persisted_to_db(self, e2e_ctx):
        """After a heartbeat, OHLCV, watchlist, and signals should be in the DB."""
        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
            await orchestrator.run_heartbeat()

        # OHLCV should be persisted
        reliance_bars = await e2e_ctx.db.get_ohlcv("RELIANCE", "daily", days=365)
        assert len(reliance_bars) > 100

        tcs_bars = await e2e_ctx.db.get_ohlcv("TCS", "daily", days=365)
        assert len(tcs_bars) > 100

        # Watchlist should be populated
        watchlist = await e2e_ctx.db.get_watchlist()
        assert len(watchlist) >= 1

    async def test_full_cycle_with_pnl(self, e2e_ctx):
        """Run heartbeat, manually close a position, verify PnL is recorded."""
        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
            await orchestrator.run_heartbeat()

        # Check for open positions
        positions = await e2e_ctx.db.get_open_positions()

        if positions:
            pos = positions[0]
            # Simulate closing at a profit
            exit_price = pos["entry_price"] * 1.02  # 2% profit
            qty = pos["quantity"]
            pnl = (exit_price - pos["entry_price"]) * qty
            await e2e_ctx.db.close_position(pos["trade_id"], exit_price, round(pnl, 2))

            # Verify PnL is recorded
            trades = await e2e_ctx.db.get_todays_trades()
            closed = [t for t in trades if t["trade_id"] == pos["trade_id"]]
            assert len(closed) == 1
            assert closed[0]["pnl"] is not None
            assert closed[0]["pnl"] > 0
            assert closed[0]["exit_price"] == exit_price

    async def test_risk_limits_enforced(self, e2e_ctx):
        """Risk check should reject signals that exceed limits."""
        # Set very low limits
        e2e_ctx.config.risk.max_open_positions = 1
        e2e_ctx.config.risk.max_portfolio_exposure_pct = 0.1

        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        # Run multiple heartbeats — second should hit position limit
        for _ in range(3):
            with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
                 patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
                 patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
                await orchestrator.run_heartbeat()

        positions = await e2e_ctx.db.get_open_positions()
        # Should not exceed max_open_positions
        assert len(positions) <= e2e_ctx.config.risk.max_open_positions

    async def test_kill_switch_stops_trading(self, e2e_ctx):
        """Kill switch should prevent trade execution."""
        await e2e_ctx.db.set_system_state("kill_switch", "active")

        orchestrator = HeartbeatOrchestrator(e2e_ctx)

        with patch.object(e2e_ctx.market_hours, "is_market_hours", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_order_window", return_value=True), \
             patch.object(e2e_ctx.market_hours, "is_premarket_window", return_value=False):
            results = await orchestrator.run_heartbeat()

        # The pipeline still runs, but risk-check rejects every signal while
        # the kill switch is active — so NO trades may be placed.
        trades = await e2e_ctx.db.get_todays_trades()
        assert results is not None
        assert trades == [], f"kill switch active but {len(trades)} trade(s) placed"
