"""Tests for the Notifier in notify.py.

Tests console backend, enabled/disabled toggle, and message tracking.
"""

import pytest

from yolovest.notify import Notifier


@pytest.fixture
def notifier(sample_config) -> Notifier:
    return Notifier(sample_config)


class TestNotifierSend:
    async def test_send_works_with_console_backend(self, notifier):
        result = await notifier.send("Test message")
        assert result is True

    async def test_send_records_message(self, notifier):
        await notifier.send("Hello world")
        assert "Hello world" in notifier.sent_messages

    async def test_send_multiple_messages(self, notifier):
        await notifier.send("First")
        await notifier.send("Second")
        await notifier.send("Third")
        assert len(notifier.sent_messages) == 3
        assert notifier.sent_messages[0] == "First"
        assert notifier.sent_messages[2] == "Third"


class TestNotifierEnabledDisabled:
    async def test_disabled_notifier_does_not_send(self, notifier):
        notifier.enabled = False
        result = await notifier.send("Should not be sent")
        assert result is False
        assert len(notifier.sent_messages) == 0

    async def test_notifier_starts_enabled(self, notifier):
        assert notifier.enabled is True

    async def test_re_enabling_notifier_works(self, notifier):
        notifier.enabled = False
        await notifier.send("Blocked")
        assert len(notifier.sent_messages) == 0

        notifier.enabled = True
        await notifier.send("Allowed")
        assert len(notifier.sent_messages) == 1
        assert notifier.sent_messages[0] == "Allowed"


class TestNotifierTelegramDisabled:
    async def test_telegram_disabled_in_test_config(self, notifier):
        """Telegram is disabled in sample_config, so only console backend is used."""
        result = await notifier.send("Test")
        assert result is True
        # Message should still be recorded via console backend
        assert len(notifier.sent_messages) == 1
