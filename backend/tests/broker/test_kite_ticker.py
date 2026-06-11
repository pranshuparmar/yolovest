"""Tests for KiteTickerClient — the Twisted→asyncio bridge.

No real WebSocket: callbacks are invoked directly the way the Twisted
thread would, against a fake ticker object. Covers the LTP cache
freshness contract, subscribe dedup, the 403 auth-failure latch (which
stops kiteconnect's infinite reconnect hammer), and the tick-broadcast
throttle.
"""

import asyncio
from typing import Any

from yolovest.broker.kite_ticker import KiteTickerClient


class _FakeTicker:
    MODE_LTP = "ltp"

    def __init__(self) -> None:
        self.subscribed: list[list[int]] = []
        self.modes: list[tuple[str, list[int]]] = []
        self.unsubscribed: list[list[int]] = []
        self.closed = False

    def subscribe(self, tokens: list[int]) -> None:
        self.subscribed.append(list(tokens))

    def set_mode(self, mode: str, tokens: list[int]) -> None:
        self.modes.append((mode, list(tokens)))

    def unsubscribe(self, tokens: list[int]) -> None:
        self.unsubscribed.append(list(tokens))

    def close(self) -> None:
        self.closed = True


class _FakeProvider:
    def __init__(self, tokens: dict[str, int]) -> None:
        self._tokens = tokens

    async def get_instrument_token(self, symbol: str) -> int | None:
        return self._tokens.get(symbol)


def _client(**kwargs: Any) -> KiteTickerClient:
    return KiteTickerClient(
        api_key="key",
        access_token="token",
        kite_data_provider=_FakeProvider({"RELIANCE": 738561, "TCS": 2953217}),
        **kwargs,
    )


class TestLtpCache:
    async def test_tick_updates_cache(self):
        c = _client()
        c._token_to_symbol = {738561: "RELIANCE"}
        c._on_ticks(None, [{"instrument_token": 738561, "last_price": 2501.5}])
        assert c.get_ltp("RELIANCE") == 2501.5

    async def test_stale_entry_returns_none(self):
        c = _client()
        c._token_to_symbol = {738561: "RELIANCE"}
        c._on_ticks(None, [{"instrument_token": 738561, "last_price": 2501.5}])
        assert c.get_ltp("RELIANCE", max_age_sec=0.0) is None

    async def test_unknown_symbol_returns_none(self):
        assert _client().get_ltp("NOPE") is None

    async def test_malformed_ticks_ignored(self):
        c = _client()
        c._token_to_symbol = {738561: "RELIANCE"}
        c._on_ticks(None, [{"instrument_token": None}, {"last_price": 1.0}, {}])
        assert c.get_ltp("RELIANCE") is None


class TestSubscribe:
    async def test_subscribe_resolves_and_sets_ltp_mode(self):
        c = _client()
        fake = _FakeTicker()
        c._ticker = fake
        await c.subscribe(["RELIANCE", "TCS"])
        assert fake.subscribed == [[738561, 2953217]]
        assert fake.modes == [("ltp", [738561, 2953217])]

    async def test_resubscribe_is_deduped(self):
        c = _client()
        fake = _FakeTicker()
        c._ticker = fake
        await c.subscribe(["RELIANCE"])
        await c.subscribe(["RELIANCE", "TCS"])
        # Second call only carries the genuinely new token.
        assert fake.subscribed == [[738561], [2953217]]

    async def test_unresolvable_symbol_skipped(self):
        c = _client()
        fake = _FakeTicker()
        c._ticker = fake
        await c.subscribe(["UNKNOWN-SYM"])
        assert fake.subscribed == []

    async def test_unsubscribe_releases_tokens(self):
        c = _client()
        fake = _FakeTicker()
        c._ticker = fake
        await c.subscribe(["RELIANCE"])
        await c.unsubscribe(["RELIANCE"])
        assert fake.unsubscribed == [[738561]]
        # Token can be re-subscribed afterwards.
        await c.subscribe(["RELIANCE"])
        assert fake.subscribed == [[738561], [738561]]


class TestAuthFailureLatch:
    async def test_403_close_sets_latch_and_schedules_stop(self):
        c = _client()
        c._loop = asyncio.get_running_loop()
        fake = _FakeTicker()
        c._ticker = fake
        c._on_close(None, 1006, "connection was closed: 403 Forbidden")
        assert c._auth_failed is True
        # stop() was marshalled onto the loop — give it a turn to run.
        for _ in range(5):
            await asyncio.sleep(0)
        assert fake.closed is True
        assert c._ticker is None

    async def test_non_auth_close_does_not_latch(self):
        c = _client()
        c._on_close(None, 1006, "connection lost")
        assert c._auth_failed is False

    async def test_reconnect_after_latch_is_ignored(self):
        c = _client()
        c._auth_failed = True
        c._on_reconnect(None, attempts=7)  # must not raise

    async def test_latch_without_loop_does_not_crash(self):
        c = _client()
        c._loop = None
        c._on_error(None, 403, "forbidden")
        assert c._auth_failed is True


class TestTickBroadcastThrottle:
    async def test_at_most_one_broadcast_per_symbol_per_window(self):
        received: list[dict[str, Any]] = []

        async def cb(tick: dict[str, Any]) -> None:
            received.append(tick)

        c = _client(tick_broadcast_callback=cb, tick_broadcast_throttle_sec=60.0)
        c._loop = asyncio.get_running_loop()
        c._token_to_symbol = {738561: "RELIANCE"}
        tick = {"instrument_token": 738561, "last_price": 100.0}
        c._on_ticks(None, [tick])
        c._on_ticks(None, [tick])
        c._on_ticks(None, [tick])
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(received) == 1
        assert received[0] == {"symbol": "RELIANCE", "ltp": 100.0}

    async def test_cache_still_updates_when_throttled(self):
        async def cb(_t: dict[str, Any]) -> None:
            pass

        c = _client(tick_broadcast_callback=cb, tick_broadcast_throttle_sec=60.0)
        c._loop = asyncio.get_running_loop()
        c._token_to_symbol = {738561: "RELIANCE"}
        c._on_ticks(None, [{"instrument_token": 738561, "last_price": 100.0}])
        c._on_ticks(None, [{"instrument_token": 738561, "last_price": 101.0}])
        assert c.get_ltp("RELIANCE") == 101.0


class TestOrderUpdateBridge:
    async def test_order_update_marshalled_to_loop(self):
        received: list[dict[str, Any]] = []

        async def cb(order: dict[str, Any]) -> None:
            received.append(order)

        c = _client(order_update_callback=cb)
        c._loop = asyncio.get_running_loop()
        c._on_order_update(None, {"order_id": "X1", "status": "COMPLETE"})
        for _ in range(5):
            await asyncio.sleep(0)
        assert received == [{"order_id": "X1", "status": "COMPLETE"}]

    async def test_no_callback_is_noop(self):
        c = _client()
        c._loop = asyncio.get_running_loop()
        c._on_order_update(None, {"order_id": "X1"})  # must not raise
