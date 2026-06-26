"""Shared test fixtures for YoloVest test suite.

Provides mock objects for all abstraction layers and sample data
for schemas. No external services required.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from yolovest.config import AppConfig
from yolovest.context import AppContext, MarketHoursChecker
from yolovest.events import EventBus
from yolovest.models.schemas import (
    PortfolioState,
    Signal,
    Trade,
)


@pytest.fixture
def sample_config() -> AppConfig:
    """Return a valid AppConfig with test values."""
    return AppConfig(
        mode="paper",
        capital={"initial_amount": 100000},
        broker={"api_key": "test_key", "api_secret": "test_secret"},
        llm={"enabled": True, "model": "gemini-2.5-flash", "api_key": "test_key"},
        market_data={"daily_provider": "jugaad", "stale_threshold_minutes": 30},
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
            "holidays": ["2026-01-26", "2026-08-15"],
        },
        notifications={"telegram": {"enabled": False, "alerts": {"errors": True}}},
    )


@pytest.fixture
def sample_signal() -> Signal:
    """Return a valid Signal instance."""
    return Signal(
        symbol="RELIANCE",
        signal_type="BUY",
        entry_price=2500.0,
        target_price=2600.0,
        stop_loss_price=2450.0,
        position_size=10,
        expected_holding_period="intraday",
        confidence_score=0.85,
        model_version="xgb-v1.0",
        features_snapshot={"rsi": 55.0, "macd": 1.2},
    )


@pytest.fixture
def sample_trade() -> Trade:
    """Return a valid Trade instance."""
    return Trade(
        trade_id="T-001",
        symbol="RELIANCE",
        signal_type="BUY",
        entry_price=2500.0,
        fill_price=2501.0,
        quantity=10,
        stop_loss_price=2450.0,
        target_price=2600.0,
        order_id="ORD-123",
        sl_order_id="SL-456",
        product="MIS",
        mode="paper",
        status="filled",
        slippage=1.0,
        created_at=datetime.now(),
    )


@pytest.fixture
def sample_portfolio_state() -> PortfolioState:
    """Return a valid PortfolioState instance."""
    return PortfolioState(
        total_capital=100000.0,
        available_cash=80000.0,
        exposure_pct=0.20,
        open_positions=1,
        stock_exposures={"RELIANCE": 0.20},
        sector_counts={"Energy": 1},
        daily_pnl_pct=-0.005,
        weekly_pnl_pct=-0.01,
        trades_today=2,
        minutes_since_last_loss=30.0,
    )


@pytest.fixture
def mock_broker() -> AsyncMock:
    """AsyncMock implementing BrokerBase interface."""
    broker = AsyncMock()
    broker.is_authenticated = AsyncMock(return_value=True)
    broker.get_positions = AsyncMock(return_value=[])
    broker.place_order = AsyncMock(return_value="ORD-TEST")
    broker.cancel_order = AsyncMock(return_value=True)
    broker.get_order_status = AsyncMock(return_value={"status": "filled"})
    broker.get_pending_orders = AsyncMock(return_value=[])
    broker.get_margins = AsyncMock(return_value={})
    broker.modify_sl_order = AsyncMock(return_value=True)
    broker.get_executed_trades = AsyncMock(return_value=[])
    broker.compute_charges = AsyncMock(return_value=None)
    # tick_for / round_to_tick are SYNC on BrokerBase. AsyncMock would
    # return coroutines that signal_evaluator stores as the target/SL
    # price. Use MagicMock with identity rounding (0.05 tick is the
    # NSE default; tests don't assert exact tick snapping).
    broker.tick_for = MagicMock(return_value=0.05)
    broker.round_to_tick = MagicMock(side_effect=lambda _sym, price: round(price, 2))
    return broker


@pytest.fixture
def mock_llm() -> AsyncMock:
    """AsyncMock implementing LLMBase interface."""
    llm = AsyncMock()
    llm.ping = AsyncMock(return_value=True)
    llm.review_trade = AsyncMock(return_value={"decision": "APPROVE", "reasoning": "Looks good"})
    llm.analyze_sentiment = AsyncMock(
        return_value={
            "symbol": "RELIANCE",
            "sentiment": "bullish",
            "confidence": 0.8,
            "key_drivers": ["strong earnings"],
        }
    )
    llm.summarize_with_web_grounding = AsyncMock(return_value={"summary": "Market is bullish"})
    llm.validate_watchlist = AsyncMock(return_value={"adjusted_shortlist": []})
    llm.summarize_market_day = AsyncMock(return_value={"summary": "Good day"})
    llm.analyze_prediction_failures = AsyncMock(return_value={"patterns": []})
    return llm


@pytest.fixture
def mock_db() -> AsyncMock:
    """AsyncMock for database operations."""
    db = AsyncMock()
    db.health_check = AsyncMock(return_value=True)
    db.get_open_positions = AsyncMock(return_value=[])
    db.get_locked_symbols = AsyncMock(return_value=set())
    db.get_feedback_data = AsyncMock(return_value={})
    db.is_kill_switch_active = AsyncMock(return_value=False)
    db.set_kill_switch = AsyncMock(return_value=True)
    db.get_portfolio_state = AsyncMock(return_value={
        "total_capital": 100000,
        "available_cash": 80000,
        "exposure_pct": 0.20,
        "open_positions": 1,
        "stock_exposures": {},
        "sector_counts": {},
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "trades_today": 0,
        "mis_trades_today": 0,
        "cnc_trades_today": 0,
        "minutes_since_last_loss": 60,
    })
    # Per-symbol loss cooldown + the gates risk-check added later. Sane
    # numeric / empty defaults so tests that construct RiskCheckSkill
    # against the shared mock_db don't trip on AsyncMock-vs-int
    # comparisons or empty-gate iteration.
    db.minutes_since_last_loss_for_symbol = AsyncMock(return_value=1e9)
    db.get_pending_trades = AsyncMock(return_value=[])
    db.get_earnings_events = AsyncMock(return_value=[])
    db.compute_symbol_beta = AsyncMock(return_value=None)
    db.upsert_ohlcv = AsyncMock()
    db.upsert_sentiment = AsyncMock()
    db.upsert_watchlist = AsyncMock()
    db.get_nse_universe = AsyncMock(return_value=[])
    db.get_latest_premarket = AsyncMock(return_value={})
    db.get_stock_sector = AsyncMock(return_value="Technology")
    db.update_position_sl = AsyncMock()
    db.update_unrealized_pnl = AsyncMock()
    db.upsert_market_data = AsyncMock()
    db.log_llm_review = AsyncMock()
    db.get_sector_rotation = AsyncMock(return_value={"strong": [], "weak": [], "sectors": {}})
    db.get_todays_trades = AsyncMock(return_value=[])
    db.get_latest_sentiment = AsyncMock(return_value=None)
    db.insert_trade = AsyncMock(return_value="T-test001")
    db.close_position = AsyncMock()
    db.insert_prediction = AsyncMock(return_value="P-test001")
    db.get_unscored_predictions = AsyncMock(return_value=[])
    db.score_prediction = AsyncMock()
    db.refresh_prediction_scoreboard = AsyncMock()
    db.get_prediction_scoreboard = AsyncMock(return_value=[])
    db.get_todays_predictions = AsyncMock(return_value=[])
    db.get_weekly_trades = AsyncMock(return_value=[])
    db.get_weekly_predictions = AsyncMock(return_value=[])
    db.get_weekly_llm_reviews = AsyncMock(return_value=[])
    db.store_report = AsyncMock()
    db.get_todays_closed_trades = AsyncMock(return_value=[])
    db.get_todays_signals_count = AsyncMock(return_value=0)
    db.update_signal_disposition = AsyncMock()
    db.get_todays_recommendations = AsyncMock(return_value=[])
    db.get_shadow_models_ready = AsyncMock(return_value=[])
    db.retire_model = AsyncMock()
    db.get_trades_history = AsyncMock(return_value=[])
    db.get_equity_curve = AsyncMock(return_value=[])
    db.get_trade_detail = AsyncMock(return_value=None)
    db.get_reports_history = AsyncMock(return_value=[])
    db.get_audit_log = AsyncMock(return_value=[])
    db.get_prediction_outcomes = AsyncMock(return_value=[])
    db.store_failure_analysis = AsyncMock()
    db.get_slippage_stats = AsyncMock(return_value={
        "total_trades": 0,
        "avg_slippage": 0,
        "max_slippage": 0,
        "avg_slippage_pct": 0,
        "by_symbol": {},
    })
    db.get_llm_review_accuracy = AsyncMock(return_value={
        "total_reviews": 0,
        "approved_count": 0,
        "rejected_count": 0,
        "approved_with_outcomes": 0,
        "profitable_approvals": 0,
        "losing_approvals": 0,
        "approval_accuracy": None,
        "approved_total_pnl": 0,
        "approved_avg_pnl": 0,
    })
    db.get_todays_signaled_symbols = AsyncMock(return_value=set())
    db.get_recently_traded_symbols = AsyncMock(return_value={})
    db.get_all_quarantined_symbol_set = AsyncMock(return_value=set())
    db.get_quarantined_symbols = AsyncMock(return_value=[])
    db.record_fetch_failure = AsyncMock(return_value=False)
    db.record_fetch_success = AsyncMock()
    # Quarantine resolver — default to identity so ingest paths that
    # route their symbol list through it (ingest-data, ingest-universe,
    # backfill) get the list back unchanged unless a test overrides.
    db.resolve_symbols_with_replacements = AsyncMock(
        side_effect=lambda syms: list(syms),
    )
    db.unquarantine_symbol = AsyncMock(return_value=True)
    db.is_quarantined = AsyncMock(return_value=False)
    # generate-signals pre-loop reads: empty stubs so the per-symbol
    # loop doesn't crash on AsyncMock-returns-coroutine for missing
    # methods. Override per-test for behaviour-specific scenarios.
    db.get_combined_watchlist = AsyncMock(return_value=[])
    db.get_quarantine_replacements = AsyncMock(return_value={})
    db.get_news_articles = AsyncMock(return_value=[])
    db.get_vix_timeline = AsyncMock(return_value=[])
    db.get_fno_timeline = AsyncMock(return_value={})
    # model-retrain per-feature timelines — empty defaults so the
    # training-data builder gets real (empty) dicts/lists instead of
    # AsyncMock coroutines that crash on .items() / iteration.
    db.get_news_timeline = AsyncMock(return_value={})
    db.get_bulk_deals_timeline = AsyncMock(return_value={})
    db.get_symbol_sectors_map = AsyncMock(return_value={})
    # Live inference-feature context (load_inference_feature_context +
    # enrich_features). compute_live_regime's result is consumed UNGUARDED
    # in enrich_features, so a bare AsyncMock (returns a mock, not a dict)
    # poisons every per-symbol eval and silently zeroes the signal list.
    # Neutral defaults mirror the production fail-open values.
    db.compute_live_regime = AsyncMock(
        return_value={"breadth": 0.5, "avg_return": 0.0, "sample_size": 0},
    )
    db.compute_live_sector_regime = AsyncMock(return_value=({}, {}))
    db.count_recent_bulk_deals = AsyncMock(
        return_value={"buy_count": 0, "sell_count": 0},
    )
    db.get_recent_delivery_pct = AsyncMock(return_value=None)
    db.get_live_metrics_for_model = AsyncMock(return_value={
        "total": 0, "scored": 0, "direction_accuracy": 0.0,
        "target_hit_rate": 0.0, "avg_pnl_pct": 0.0,
    })
    db.get_ohlcv = AsyncMock(return_value=[])
    db.get_system_state = AsyncMock(return_value=None)
    db.record_signal_outcome = AsyncMock()
    db.insert_signal = AsyncMock(return_value="S-test001")
    db.insert_shadow_prediction = AsyncMock()
    return db


@pytest.fixture
def mock_market_data() -> AsyncMock:
    """AsyncMock implementing MarketDataBase interface."""
    md = AsyncMock()
    md.health_check = AsyncMock(return_value=True)
    md.get_ohlcv = AsyncMock(return_value=[])
    md.get_quote = AsyncMock(return_value={"ltp": 2500.0})
    md.get_ltp = AsyncMock(return_value=2500.0)
    # get_fetch_meta is a synchronous provenance lookup; keep it a sync Mock so
    # callers (e.g. SkillBase._ingest_source) don't get an unawaited coroutine.
    md.get_fetch_meta = MagicMock(return_value=None)
    return md


@pytest.fixture
def mock_notify() -> AsyncMock:
    """AsyncMock with send() for notifications."""
    notify = AsyncMock()
    notify.send = AsyncMock(return_value=None)
    notify.send_trade_alert = AsyncMock(return_value=None)
    return notify


@pytest.fixture
def app_context(
    sample_config: AppConfig,
    mock_broker: AsyncMock,
    mock_llm: AsyncMock,
    mock_db: AsyncMock,
    mock_market_data: AsyncMock,
    mock_notify: AsyncMock,
) -> AppContext:
    """AppContext assembled from all mocks."""
    market_hours = MarketHoursChecker(sample_config)
    bus = EventBus()
    return AppContext(
        config=sample_config,
        db=mock_db,
        broker=mock_broker,
        llm=mock_llm,
        market_data=mock_market_data,
        notify=mock_notify,
        market_hours=market_hours,
        event_bus=bus,
    )


@pytest.fixture
def event_bus() -> EventBus:
    """Fresh EventBus instance."""
    return EventBus()
