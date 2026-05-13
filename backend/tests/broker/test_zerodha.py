"""Tests for ZerodhaBroker — paper mode and mocked live mode."""

from unittest.mock import MagicMock

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

    async def test_compute_charges_returns_none_in_paper(self, paper_broker):
        result = await paper_broker.compute_charges([
            {"exchange": "NSE", "tradingsymbol": "RELIANCE",
             "transaction_type": "BUY", "variety": "regular",
             "product": "MIS", "order_type": "MARKET",
             "quantity": 1, "average_price": 2500.0},
        ])
        assert result is None


class TestComputeChargesLive:
    """Live-mode compute_charges with a mocked Kite client."""

    @pytest.fixture
    def live_broker(self):
        return ZerodhaBroker(api_key="test", api_secret="test", mode="live")

    async def test_maps_kite_charges_block(self, live_broker):
        live_broker._kite = MagicMock()
        live_broker._kite.get_virtual_contract_note = MagicMock(return_value=[
            {
                "type": "equity",
                "total": 22.30,
                "charges": {
                    "transaction_tax": 1.50,
                    "transaction_tax_type": "stt",
                    "exchange_turnover_charge": 0.30,
                    "sebi_turnover_charge": 0.05,
                    "brokerage": 20.0,
                    "stamp_duty": 0.10,
                    "gst": {"igst": 0.0, "cgst": 0.18, "sgst": 0.17, "total": 0.35},
                    "total": 22.30,
                },
            },
            {
                "type": "equity",
                "total": 27.10,
                "charges": {
                    "transaction_tax": 6.30,
                    "transaction_tax_type": "stt",
                    "exchange_turnover_charge": 0.30,
                    "sebi_turnover_charge": 0.05,
                    "brokerage": 20.0,
                    "stamp_duty": 0.10,
                    "gst": {"total": 0.35},
                    "total": 27.10,
                },
            },
        ])

        legs = [
            {"exchange": "NSE", "tradingsymbol": "RELIANCE",
             "transaction_type": "BUY", "variety": "regular",
             "product": "MIS", "order_type": "MARKET",
             "quantity": 10, "average_price": 2500.0},
            {"exchange": "NSE", "tradingsymbol": "RELIANCE",
             "transaction_type": "SELL", "variety": "regular",
             "product": "MIS", "order_type": "MARKET",
             "quantity": 10, "average_price": 2510.0},
        ]
        result = await live_broker.compute_charges(legs)

        assert result is not None
        assert len(result) == 2
        assert result[0]["brokerage"] == 20.0
        assert result[0]["stt"] == 1.50
        # other = exchange + sebi + stamp + gst.total = 0.30 + 0.05 + 0.10 + 0.35
        assert result[0]["other_charges"] == pytest.approx(0.80)
        assert result[0]["total"] == 22.30
        assert result[1]["stt"] == 6.30
        assert result[1]["total"] == 27.10

        # Verify the legs got forwarded verbatim
        live_broker._kite.get_virtual_contract_note.assert_called_once_with(legs)

    async def test_returns_none_when_kite_raises(self, live_broker):
        live_broker._kite = MagicMock()
        live_broker._kite.get_virtual_contract_note = MagicMock(
            side_effect=RuntimeError("Kite API error")
        )
        result = await live_broker.compute_charges([
            {"exchange": "NSE", "tradingsymbol": "RELIANCE",
             "transaction_type": "BUY", "variety": "regular",
             "product": "MIS", "order_type": "MARKET",
             "quantity": 1, "average_price": 2500.0},
        ])
        assert result is None

    async def test_returns_none_when_kite_response_count_mismatches(self, live_broker):
        live_broker._kite = MagicMock()
        live_broker._kite.get_virtual_contract_note = MagicMock(return_value=[
            {"charges": {"total": 22.30}},  # only one back, two sent
        ])
        result = await live_broker.compute_charges([
            {"exchange": "NSE", "tradingsymbol": "X", "transaction_type": "BUY",
             "variety": "regular", "product": "MIS", "order_type": "MARKET",
             "quantity": 1, "average_price": 100.0},
            {"exchange": "NSE", "tradingsymbol": "X", "transaction_type": "SELL",
             "variety": "regular", "product": "MIS", "order_type": "MARKET",
             "quantity": 1, "average_price": 101.0},
        ])
        assert result is None

    async def test_returns_none_when_no_legs(self, live_broker):
        live_broker._kite = MagicMock()
        result = await live_broker.compute_charges([])
        assert result is None
        live_broker._kite.get_virtual_contract_note.assert_not_called()
