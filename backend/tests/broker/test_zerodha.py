"""Tests for ZerodhaBroker — paper mode and mocked live mode."""

import pytest

from yolovest.broker.zerodha import ZerodhaBroker


@pytest.fixture
def paper_broker():
    return ZerodhaBroker(api_key="test", api_secret="test", mode="paper")


class TestPaperMode:
    async def test_authenticate_paper(self, paper_broker):
        result = await paper_broker.authenticate("dummy_token")
        assert result is True
        assert await paper_broker.is_authenticated() is True

    async def test_not_authenticated_by_default(self, paper_broker):
        assert await paper_broker.is_authenticated() is False

    async def test_place_market_order(self, paper_broker):
        await paper_broker.authenticate("token")
        order_id = await paper_broker.place_order(
            symbol="RELIANCE", side="BUY", quantity=10,
            order_type="MARKET", product="MIS", price=2500.0,
        )
        assert order_id.startswith("PAPER-")
        status = await paper_broker.get_order_status(order_id)
        assert status["status"] == "filled"
        assert status["symbol"] == "RELIANCE"

    async def test_place_limit_order(self, paper_broker):
        await paper_broker.authenticate("token")
        order_id = await paper_broker.place_order(
            symbol="TCS", side="BUY", quantity=5,
            order_type="LIMIT", product="CNC", price=3500.0,
        )
        status = await paper_broker.get_order_status(order_id)
        assert status["status"] == "open"

    async def test_cancel_order(self, paper_broker):
        await paper_broker.authenticate("token")
        order_id = await paper_broker.place_order(
            symbol="INFY", side="SELL", quantity=10,
            order_type="LIMIT", product="MIS", price=1500.0,
        )
        assert await paper_broker.cancel_order(order_id) is True
        status = await paper_broker.get_order_status(order_id)
        assert status["status"] == "cancelled"

    async def test_cancel_nonexistent(self, paper_broker):
        assert await paper_broker.cancel_order("FAKE-123") is False

    async def test_get_positions(self, paper_broker):
        await paper_broker.authenticate("token")
        await paper_broker.place_order(
            symbol="RELIANCE", side="BUY", quantity=10,
            order_type="MARKET", product="MIS", price=2500.0,
        )
        positions = await paper_broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "RELIANCE"

    async def test_get_pending_orders(self, paper_broker):
        await paper_broker.authenticate("token")
        await paper_broker.place_order(
            symbol="TCS", side="BUY", quantity=5,
            order_type="LIMIT", product="CNC", price=3500.0,
        )
        pending = await paper_broker.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["order_type"] == "LIMIT"

    async def test_get_margins(self, paper_broker):
        margins = await paper_broker.get_margins()
        assert "available" in margins
        # Paper mode without Kite returns 0 (real balance comes from Kite API)
        assert margins["available"]["cash"] == 0

    async def test_slippage_applied_to_market_buy(self, paper_broker):
        await paper_broker.authenticate("token")
        order_id = await paper_broker.place_order(
            symbol="RELIANCE", side="BUY", quantity=10,
            order_type="MARKET", product="MIS", price=1000.0,
        )
        status = await paper_broker.get_order_status(order_id)
        # BUY slippage: price * (1 + 0.001) = 1001.0
        assert status["fill_price"] == pytest.approx(1001.0)

    async def test_slippage_applied_to_market_sell(self, paper_broker):
        await paper_broker.authenticate("token")
        order_id = await paper_broker.place_order(
            symbol="RELIANCE", side="SELL", quantity=10,
            order_type="MARKET", product="MIS", price=1000.0,
        )
        status = await paper_broker.get_order_status(order_id)
        # SELL slippage: price * (1 - 0.001) = 999.0
        assert status["fill_price"] == pytest.approx(999.0)

    async def test_order_status_unknown(self, paper_broker):
        status = await paper_broker.get_order_status("NONEXISTENT")
        assert status["status"] == "unknown"

    async def test_multiple_orders_tracked(self, paper_broker):
        await paper_broker.authenticate("token")
        id1 = await paper_broker.place_order("A", "BUY", 1, "MARKET", "MIS", 100.0)
        id2 = await paper_broker.place_order("B", "BUY", 1, "MARKET", "MIS", 200.0)
        assert id1 != id2
        positions = await paper_broker.get_positions()
        assert len(positions) == 2
