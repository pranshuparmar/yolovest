"""The Quick ML Review (/api/review) must work for ANY NSE symbol, not just
the ingested universe. When the local DB has no history for a symbol, the
endpoint falls back to an on-demand provider fetch so it still produces a real
recommendation (regression for the "only returns for watchlist" gap)."""

import types
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.dashboard.app import create_app
from yolovest.events import EventBus
from yolovest.models.schemas import OHLCVBar

AUTH = ("admin", "yolovest")


def _bars(n: int = 250) -> list[OHLCVBar]:
    """A gentle uptrend with valid OHLC + volume so compute_features works."""
    out: list[OHLCVBar] = []
    base = datetime(2025, 1, 1)
    price = 100.0
    for i in range(n):
        price *= 1.004
        out.append(
            OHLCVBar(
                timestamp=base + timedelta(days=i),
                open=round(price * 0.99, 2),
                high=round(price * 1.02, 2),
                low=round(price * 0.98, 2),
                close=round(price, 2),
                volume=100_000 + i * 10,
            )
        )
    return out


def _ctx(sample_config, mock_db, mock_broker, mock_market_data, mock_llm, mock_notify, *, ml=None):
    return AppContext(
        config=sample_config, db=mock_db, broker=mock_broker, llm=mock_llm,
        market_data=mock_market_data, notify=mock_notify, ml=ml,
        market_hours=MarketHoursChecker(sample_config), event_bus=EventBus(),
    )


@pytest.fixture(autouse=True)
def _empty_db_and_broker(mock_db, mock_broker, mock_market_data):
    # Symbol is NOT in the ingested universe → DB returns nothing.
    from yolovest.data import ohlcv_cache
    ohlcv_cache.clear()  # module-level cache must not leak across tests
    mock_db.get_ohlcv = AsyncMock(return_value=[])
    mock_db.get_open_positions = AsyncMock(return_value=[])
    mock_broker.get_holdings = AsyncMock(return_value=[])
    mock_market_data.get_ltp = AsyncMock(return_value=105.0)


def test_review_falls_back_to_provider_for_non_universe_symbol(
    sample_config, mock_db, mock_broker, mock_market_data, mock_llm, mock_notify,
):
    # Provider chain serves the full history the DB lacked.
    mock_market_data.get_ohlcv = AsyncMock(return_value=_bars())
    ml = types.SimpleNamespace(
        predict_swing=AsyncMock(return_value=types.SimpleNamespace(
            signal_type="BUY", confidence=0.72, target_price=130.0, stop_loss_price=95.0)),
        predict_intraday=AsyncMock(return_value=types.SimpleNamespace(
            signal_type="HOLD", confidence=0.30, target_price=0.0, stop_loss_price=0.0)),
    )
    client = TestClient(create_app(_ctx(sample_config, mock_db, mock_broker, mock_market_data, mock_llm, mock_notify, ml=ml)))

    r = client.post("/api/review", json={"symbols": ["NEWSYM"]}, auth=AUTH)
    assert r.status_code == 200
    recos = r.json()["recommendations"]
    assert len(recos) == 1
    reco = recos[0]
    assert reco["symbol"] == "NEWSYM"
    assert reco["action"] == "BUY"                 # a real call, not a no-data HOLD
    assert "Insufficient data" not in reco["reasoning"]
    mock_market_data.get_ohlcv.assert_awaited()    # the on-demand fallback fired


def test_review_reports_insufficient_only_when_provider_also_empty(
    sample_config, mock_db, mock_broker, mock_market_data, mock_llm, mock_notify,
):
    # Truly unknown symbol: DB empty AND provider empty → graceful message.
    mock_market_data.get_ohlcv = AsyncMock(return_value=[])
    client = TestClient(create_app(_ctx(sample_config, mock_db, mock_broker, mock_market_data, mock_llm, mock_notify)))

    r = client.post("/api/review", json={"symbols": ["BOGUS"]}, auth=AUTH)
    assert r.status_code == 200
    reco = r.json()["recommendations"][0]
    assert reco["action"] == "HOLD"
    assert "Insufficient data" in reco["reasoning"]
    mock_market_data.get_ohlcv.assert_awaited()
