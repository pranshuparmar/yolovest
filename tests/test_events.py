"""Tests for the EventBus in events.py.

Tests publish/subscribe, multiple subscribers, unsubscribe,
and async handler support.
"""


from yolovest.events import Event


class TestEventBusSubscribe:
    async def test_publish_subscribe_works(self, event_bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe("test.event", handler)
        await event_bus.publish(Event(event_type="test.event", data={"key": "value"}))

        assert len(received) == 1
        assert received[0].data == {"key": "value"}

    async def test_multiple_subscribers_receive_events(self, event_bus):
        received_a = []
        received_b = []

        async def handler_a(event: Event):
            received_a.append(event)

        async def handler_b(event: Event):
            received_b.append(event)

        event_bus.subscribe("multi.event", handler_a)
        event_bus.subscribe("multi.event", handler_b)
        await event_bus.publish(Event(event_type="multi.event", data={"n": 1}))

        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_subscriber_only_receives_subscribed_event_type(self, event_bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe("type.a", handler)
        await event_bus.publish(Event(event_type="type.b", data={}))

        assert len(received) == 0

    async def test_subscriber_count(self, event_bus):
        async def handler(event: Event):
            pass

        assert event_bus.subscriber_count("some.event") == 0
        event_bus.subscribe("some.event", handler)
        assert event_bus.subscriber_count("some.event") == 1


class TestEventBusUnsubscribe:
    async def test_unsubscribed_handler_does_not_receive(self, event_bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe("unsub.event", handler)
        event_bus.unsubscribe("unsub.event", handler)
        await event_bus.publish(Event(event_type="unsub.event"))

        assert len(received) == 0

    async def test_unsubscribe_nonexistent_handler_is_safe(self, event_bus):
        async def handler(event: Event):
            pass

        # Should not raise
        event_bus.unsubscribe("no.such.event", handler)

    async def test_unsubscribe_one_keeps_others(self, event_bus):
        received_a = []
        received_b = []

        async def handler_a(event: Event):
            received_a.append(event)

        async def handler_b(event: Event):
            received_b.append(event)

        event_bus.subscribe("partial.unsub", handler_a)
        event_bus.subscribe("partial.unsub", handler_b)
        event_bus.unsubscribe("partial.unsub", handler_a)
        await event_bus.publish(Event(event_type="partial.unsub"))

        assert len(received_a) == 0
        assert len(received_b) == 1


class TestEventBusAsync:
    async def test_async_handlers_work_correctly(self, event_bus):
        results = []

        async def async_handler(event: Event):
            results.append(event.data.get("value"))

        event_bus.subscribe("async.event", async_handler)
        await event_bus.publish(Event(event_type="async.event", data={"value": 42}))

        assert results == [42]

    async def test_publish_no_subscribers_does_not_raise(self, event_bus):
        await event_bus.publish(Event(event_type="no.subscribers"))

    async def test_multiple_publishes_received_in_order(self, event_bus):
        order = []

        async def handler(event: Event):
            order.append(event.data["seq"])

        event_bus.subscribe("order.test", handler)
        for i in range(5):
            await event_bus.publish(Event(event_type="order.test", data={"seq": i}))

        assert order == [0, 1, 2, 3, 4]


class TestEventCreation:
    def test_event_has_timestamp(self):
        event = Event(event_type="test")
        assert event.timestamp is not None

    def test_event_default_empty_data(self):
        event = Event(event_type="test")
        assert event.data == {}
