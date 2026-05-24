"""Tests for resolve_round_trip_costs — broker actuals with config fallback."""

from unittest.mock import AsyncMock

import pytest

from yolovest.config import TransactionCostConfig
from yolovest.costs import (
    compute_transaction_cost_breakdown,
    compute_transaction_costs,
    resolve_round_trip_costs,
)


@pytest.fixture
def cost_config() -> TransactionCostConfig:
    return TransactionCostConfig()


class TestComputeTransactionCosts:
    def test_intraday_cap_applied(self):
        # Brokerage capped at ₹20 per leg regardless of value. On a
        # ₹250k notional trade (100 × ₹2500-ish), total round-trip
        # comes out to ~₹40 brokerage + ~₹62 STT + ~₹20 exchange +
        # ~₹14 GST + small other = roughly ₹150. The cap test verifies
        # the BROKERAGE component doesn't scale with notional —
        # without it brokerage alone would be ~₹250 on this trade.
        costs = compute_transaction_costs(2500, 2510, 100, product="MIS")
        assert costs > 0
        # Sanity ceiling: without the brokerage cap costs would be
        # 2-3× higher. 250 is comfortable headroom over the realistic
        # ~150 floor while still catching a missing-cap regression.
        assert costs < 250

    def test_delivery_stt_higher(self):
        mis = compute_transaction_costs(2500, 2510, 10, product="MIS")
        cnc = compute_transaction_costs(2500, 2510, 10, product="CNC")
        assert cnc > mis  # CNC STT is 0.1% vs 0.025% for MIS


class TestResolveRoundTripCostsBrokerPath:
    @pytest.mark.asyncio
    async def test_uses_broker_actuals_when_available(self, cost_config):
        broker = AsyncMock()
        broker.compute_charges = AsyncMock(return_value=[
            {"brokerage": 20.0, "stt": 1.5, "other_charges": 0.8, "total": 22.3},
            {"brokerage": 20.0, "stt": 6.3, "other_charges": 0.8, "total": 27.1},
        ])

        total, src, breakdown = await resolve_round_trip_costs(
            broker, symbol="RELIANCE", signal_type="BUY",
            entry_price=2500.0, exit_price=2510.0, quantity=10,
            product="MIS", cost_config=cost_config,
        )

        assert src == "broker"
        assert total == pytest.approx(49.4)
        assert breakdown["source"] == "broker"
        assert breakdown["brokerage"] == 40.0
        assert breakdown["stt"] == pytest.approx(7.8)
        assert breakdown["other_charges"] == pytest.approx(1.6)
        assert breakdown["total"] == pytest.approx(49.4)

        # Verify the legs sent to the broker
        broker.compute_charges.assert_awaited_once()
        legs = broker.compute_charges.await_args.args[0]
        assert len(legs) == 2
        assert legs[0]["transaction_type"] == "BUY"
        assert legs[0]["average_price"] == 2500.0
        assert legs[1]["transaction_type"] == "SELL"
        assert legs[1]["average_price"] == 2510.0
        assert legs[0]["tradingsymbol"] == "RELIANCE"

    @pytest.mark.asyncio
    async def test_sell_signal_flips_legs(self, cost_config):
        broker = AsyncMock()
        broker.compute_charges = AsyncMock(return_value=[
            {"brokerage": 20.0, "stt": 1.0, "other_charges": 0.5, "total": 21.5},
            {"brokerage": 20.0, "stt": 1.0, "other_charges": 0.5, "total": 21.5},
        ])

        await resolve_round_trip_costs(
            broker, symbol="HDFCLIFE", signal_type="SELL",
            entry_price=600.0, exit_price=595.0, quantity=20,
            product="MIS", cost_config=cost_config,
        )

        legs = broker.compute_charges.await_args.args[0]
        assert legs[0]["transaction_type"] == "SELL"  # entry side
        assert legs[1]["transaction_type"] == "BUY"   # exit side


class TestResolveRoundTripCostsFallback:
    @pytest.mark.asyncio
    async def test_falls_back_when_broker_returns_none(self, cost_config):
        broker = AsyncMock()
        broker.compute_charges = AsyncMock(return_value=None)

        total, src, breakdown = await resolve_round_trip_costs(
            broker, symbol="RELIANCE", signal_type="BUY",
            entry_price=2500.0, exit_price=2510.0, quantity=10,
            product="MIS", cost_config=cost_config,
        )

        assert src == "estimate"
        assert breakdown["source"] == "estimate"
        expected = compute_transaction_costs(
            2500.0, 2510.0, 10, product="MIS", cost_config=cost_config,
        )
        assert total == expected
        assert breakdown["total"] == expected

    @pytest.mark.asyncio
    async def test_falls_back_when_broker_raises(self, cost_config):
        broker = AsyncMock()
        broker.compute_charges = AsyncMock(side_effect=RuntimeError("kite down"))

        total, src, breakdown = await resolve_round_trip_costs(
            broker, symbol="RELIANCE", signal_type="BUY",
            entry_price=2500.0, exit_price=2510.0, quantity=10,
            product="MIS", cost_config=cost_config,
        )

        assert src == "estimate"
        assert breakdown["source"] == "estimate"
        assert total > 0

    @pytest.mark.asyncio
    async def test_falls_back_on_mismatched_leg_count(self, cost_config):
        broker = AsyncMock()
        broker.compute_charges = AsyncMock(return_value=[
            {"brokerage": 20.0, "stt": 1.0, "other_charges": 0.5, "total": 21.5},
        ])  # only one leg returned

        total, src, _ = await resolve_round_trip_costs(
            broker, symbol="RELIANCE", signal_type="BUY",
            entry_price=2500.0, exit_price=2510.0, quantity=10,
            product="MIS", cost_config=cost_config,
        )

        assert src == "estimate"

    @pytest.mark.asyncio
    async def test_no_broker_uses_estimate(self, cost_config):
        total, src, _ = await resolve_round_trip_costs(
            None, symbol="RELIANCE", signal_type="BUY",
            entry_price=2500.0, exit_price=2510.0, quantity=10,
            product="MIS", cost_config=cost_config,
        )
        assert src == "estimate"
        assert total > 0


class TestBreakdownStillWorks:
    """Existing detail breakdown (used by dashboard cost detail) untouched."""

    def test_breakdown_keys(self):
        bd = compute_transaction_cost_breakdown(2500, 2510, 10, product="MIS")
        assert set(bd.keys()) == {"brokerage", "stt", "other_charges", "total"}
        assert bd["total"] == round(bd["brokerage"] + bd["stt"] + bd["other_charges"], 2)
