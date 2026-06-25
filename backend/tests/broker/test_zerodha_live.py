"""Tests for ZerodhaBroker — mocked live mode (Kite SDK)."""

from unittest.mock import MagicMock, patch

import pytest

from yolovest.broker.zerodha import ZerodhaBroker


@pytest.fixture
def live_broker():
    """Create a live mode broker with mocked Kite SDK."""
    broker = ZerodhaBroker(
        api_key="test_key",
        api_secret="test_secret",
        mode="live",
        max_retries=2,
        retry_base_delay=0.01,  # fast retries for tests
    )
    return broker


def _mock_kite():
    """Create a mock KiteConnect instance."""
    kite = MagicMock()
    kite.access_token = "live_token_123"
    kite.generate_session.return_value = {"access_token": "live_token_123"}
    kite.profile.return_value = {"user_name": "test"}
    kite.place_order.return_value = "ORD-LIVE-001"
    # LTP source for MARKET→LIMIT conversion. Without it the broker
    # now ABORTS a MARKET order rather than submitting an unprotected
    # raw MARKET (see _live_place_order). Both ltp() and ohlc() key by
    # "EXCH:SYMBOL"; ohlc() is the path used when kite_data_enabled is
    # off (the live_broker fixture's default).
    _prices = {
        "NSE:RELIANCE": {"last_price": 2500.0},
        "NSE:TCS": {"last_price": 3500.0},
        "NSE:INFY": {"last_price": 1500.0},
    }
    kite.ltp.return_value = _prices
    kite.ohlc.return_value = _prices
    kite.orders.return_value = [
        {"order_id": "ORD-LIVE-001", "status": "COMPLETE"},
    ]
    kite.positions.return_value = {
        "net": [{"tradingsymbol": "RELIANCE", "quantity": 10}],
    }
    kite.margins.return_value = {
        "equity": {"available": {"cash": 50000}},
    }
    return kite


class TestLiveAuthentication:
    async def test_authenticate_success(self, live_broker):
        mock_kite_instance = _mock_kite()
        with patch.object(live_broker, "_create_kite_session", return_value=mock_kite_instance):
            result = await live_broker.authenticate("valid_token")

        assert result is True
        assert live_broker._access_token == "live_token_123"

    async def test_authenticate_failure(self, live_broker):
        with patch.object(
            live_broker, "_create_kite_session", side_effect=Exception("Invalid token")
        ):
            result = await live_broker.authenticate("bad_token")

        assert result is False
        assert live_broker._access_token is None

    async def test_is_authenticated_checks_profile(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        result = await live_broker.is_authenticated()
        assert result is True
        mock_kite_instance.profile.assert_called_once()

    async def test_is_authenticated_false_when_no_kite(self, live_broker):
        assert await live_broker.is_authenticated() is False

    async def test_is_authenticated_false_on_api_error(self, live_broker):
        mock_kite_instance = _mock_kite()
        mock_kite_instance.profile.side_effect = Exception("Session expired")
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        result = await live_broker.is_authenticated()
        assert result is False


class TestLiveOrderPlacement:
    async def test_place_market_order(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        order_id = await live_broker.place_order(
            symbol="RELIANCE", side="BUY", quantity=10,
            order_type="MARKET", product="MIS",
        )

        assert order_id == "ORD-LIVE-001"
        mock_kite_instance.place_order.assert_called_once()
        call_kwargs = mock_kite_instance.place_order.call_args
        assert call_kwargs.kwargs["tradingsymbol"] == "RELIANCE"

    async def test_place_order_not_authenticated_raises(self, live_broker):
        with pytest.raises(RuntimeError, match="Not authenticated"):
            await live_broker.place_order(
                symbol="RELIANCE", side="BUY", quantity=10,
                order_type="MARKET", product="MIS",
            )

    async def test_place_limit_order_with_price(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        await live_broker.place_order(
            symbol="TCS", side="BUY", quantity=5,
            order_type="LIMIT", product="CNC", price=3500.0,
        )

        call_kwargs = mock_kite_instance.place_order.call_args.kwargs
        assert call_kwargs["price"] == 3500.0

    async def test_place_sl_order_with_trigger(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        await live_broker.place_order(
            symbol="INFY", side="SELL", quantity=10,
            order_type="SL-M", product="MIS",
            trigger_price=1450.0,
        )

        call_kwargs = mock_kite_instance.place_order.call_args.kwargs
        assert call_kwargs["trigger_price"] == 1450.0


class TestLiveRetry:
    async def test_place_order_does_not_retry_on_failure(self, live_broker):
        # Order creation is non-idempotent: Kite can return an error AFTER
        # the exchange already accepted the order, so a retry would place a
        # DUPLICATE. place_order must make exactly one attempt and surface the
        # error to the skill layer (which reconciles via kite.orders()),
        # never retry under the broker.
        mock_kite_instance = MagicMock()
        mock_kite_instance.place_order.side_effect = [
            ConnectionError("timeout"),
            "ORD-RETRY-001",
        ]
        mock_kite_instance.ohlc.return_value = {"NSE:RELIANCE": {"last_price": 2500.0}}
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        with pytest.raises(ConnectionError, match="timeout"):
            await live_broker.place_order(
                symbol="RELIANCE", side="BUY", quantity=10,
                order_type="MARKET", product="MIS",
            )

        # Single attempt — the second side_effect value is never reached.
        assert mock_kite_instance.place_order.call_count == 1

    async def test_place_order_failure_raises(self, live_broker):
        mock_kite_instance = MagicMock()
        mock_kite_instance.place_order.side_effect = ConnectionError("always fails")
        mock_kite_instance.ohlc.return_value = {"NSE:RELIANCE": {"last_price": 2500.0}}
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        with pytest.raises(ConnectionError, match="always fails"):
            await live_broker.place_order(
                symbol="RELIANCE", side="BUY", quantity=10,
                order_type="MARKET", product="MIS",
            )
        assert mock_kite_instance.place_order.call_count == 1

    async def test_idempotent_call_retries(self, live_broker):
        # The generic retry loop still applies to idempotent calls (reads,
        # absolute-state modifies): a transient failure is retried.
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("transient")
            return "OK"

        result = await live_broker._retry_api_call(fn)  # idempotent=True default
        assert result == "OK"
        assert calls["n"] == 2

    async def test_non_idempotent_call_single_attempt(self, live_broker):
        # idempotent=False breaks out after the first attempt.
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError, match="boom"):
            await live_broker._retry_api_call(fn, idempotent=False)
        assert calls["n"] == 1


class TestLiveOrderManagement:
    async def test_cancel_order(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        result = await live_broker.cancel_order("ORD-001")
        assert result is True

    async def test_cancel_order_failure(self, live_broker):
        mock_kite_instance = _mock_kite()
        mock_kite_instance.cancel_order.side_effect = Exception("Order not found")
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        result = await live_broker.cancel_order("ORD-FAKE")
        assert result is False

    async def test_get_order_status(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        status = await live_broker.get_order_status("ORD-LIVE-001")
        assert status["status"] == "COMPLETE"

    async def test_get_order_status_unknown(self, live_broker):
        mock_kite_instance = _mock_kite()
        mock_kite_instance.orders.return_value = []
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        status = await live_broker.get_order_status("NONEXISTENT")
        assert status["status"] == "unknown"

    async def test_get_positions(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        positions = await live_broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["tradingsymbol"] == "RELIANCE"

    async def test_get_pending_orders(self, live_broker):
        mock_kite_instance = _mock_kite()
        mock_kite_instance.orders.return_value = [
            {"order_id": "O1", "status": "OPEN"},
            {"order_id": "O2", "status": "COMPLETE"},
        ]
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        pending = await live_broker.get_pending_orders()
        assert len(pending) == 1
        assert pending[0]["order_id"] == "O1"

    async def test_get_margins(self, live_broker):
        mock_kite_instance = _mock_kite()
        live_broker._kite = mock_kite_instance
        live_broker._access_token = "token"

        margins = await live_broker.get_margins()
        assert "equity" in margins


class TestRateLimiting:
    async def test_semaphore_limits_concurrent_calls(self, live_broker):
        """Verify the rate limiter's concurrency cap is 8. The semaphore
        moved behind KiteRateLimiter._semaphore when concurrency + time
        gating were combined into one limiter."""
        assert live_broker._rate_limiter._semaphore._value == 8
