"""Tests for the Telegram bot authorization gate.

The bot token only authenticates the bot to Telegram — anyone who finds
the bot's username can send it commands. _authorize_update must drop
every update that isn't from the configured chat_id, and must fail
closed when no chat_id is configured.
"""

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from yolovest.telegram_bot import TelegramBot


def _bot(sample_config, chat_id: str) -> TelegramBot:
    sample_config.notifications.telegram.chat_id = chat_id
    ctx = SimpleNamespace(config=sample_config)
    return TelegramBot(ctx)


def _update(chat_id: object = None, user_id: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id) if chat_id is not None else None,
        effective_user=SimpleNamespace(id=user_id) if user_id is not None else None,
    )


class TestAuthorizeUpdate:
    async def test_configured_chat_id_passes(self, sample_config):
        bot = _bot(sample_config, "12345")
        # Returning without raising means downstream handlers run.
        await bot._authorize_update(_update(chat_id=12345, user_id=12345), None)

    async def test_user_id_match_passes(self, sample_config):
        # Private chats have chat.id == user.id, but match on either so
        # group-admin setups keep working.
        bot = _bot(sample_config, "12345")
        await bot._authorize_update(_update(chat_id=-99887766, user_id=12345), None)

    async def test_wrong_sender_blocked(self, sample_config):
        bot = _bot(sample_config, "12345")
        with pytest.raises(ApplicationHandlerStop):
            await bot._authorize_update(_update(chat_id=666, user_id=666), None)

    async def test_unset_chat_id_fails_closed(self, sample_config):
        bot = _bot(sample_config, "")
        with pytest.raises(ApplicationHandlerStop):
            await bot._authorize_update(_update(chat_id=12345, user_id=12345), None)

    async def test_update_without_sender_blocked(self, sample_config):
        bot = _bot(sample_config, "12345")
        with pytest.raises(ApplicationHandlerStop):
            await bot._authorize_update(_update(), None)

    async def test_guard_registered_before_command_handlers(self, sample_config):
        """start() must register the TypeHandler gate in group -1 so it
        runs before every CommandHandler (group 0)."""
        from telegram.ext import TypeHandler

        bot = _bot(sample_config, "12345")
        sample_config.notifications.telegram.enabled = True
        from pydantic import SecretStr

        sample_config.notifications.telegram.bot_token = SecretStr("123:fake-token")

        captured: dict[int, list[object]] = {}

        class _FakeApp:
            def add_handler(self, handler: object, group: int = 0) -> None:
                captured.setdefault(group, []).append(handler)

        class _FakeBuilder:
            def token(self, _tok: str) -> "_FakeBuilder":
                return self

            def build(self) -> _FakeApp:
                return _FakeApp()

        from unittest.mock import patch

        # Stop start() right after handler registration by making
        # initialize raise — we only care about registration order.
        fake_app = _FakeApp()

        class _Builder2(_FakeBuilder):
            def build(self) -> _FakeApp:
                return fake_app

        import telegram.ext as ptb_ext

        with patch.object(ptb_ext, "ApplicationBuilder", _Builder2):
            fake_app.initialize = _raise_async  # type: ignore[attr-defined]
            with pytest.raises(_StopStartError):
                await bot.start()

        guards = captured.get(-1, [])
        assert len(guards) == 1, "exactly one group -1 authorization gate"
        assert isinstance(guards[0], TypeHandler)
        assert captured.get(0), "command handlers registered in group 0"


class _StopStartError(Exception):
    pass


async def _raise_async() -> None:
    raise _StopStartError
