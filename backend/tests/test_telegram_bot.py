"""Tests for Telegram bot and notifier integration."""

from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from yolovest.notify import Notifier, _format_trade_alert
from yolovest.telegram_bot import TelegramBot


class TestFormatTradeAlert:
    def test_buy_alert(self):
        trade = {
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "quantity": 10,
            "fill_price": 2501.50,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "mode": "paper",
        }
        msg = _format_trade_alert(trade)

        assert "BUY" in msg
        assert "RELIANCE" in msg
        assert "PAPER" in msg
        assert "2501.50" in msg

    def test_sell_alert(self):
        trade = {
            "symbol": "TCS",
            "signal_type": "SELL",
            "quantity": 5,
            "entry_price": 3500.0,
            "stop_loss_price": 3550.0,
            "target_price": 3400.0,
            "mode": "live",
        }
        msg = _format_trade_alert(trade)

        assert "SELL" in msg
        assert "LIVE" in msg


class TestNotifierTelegramIntegration:
    async def test_send_with_telegram_bot(self, sample_config):
        sample_config.notifications.telegram.enabled = True
        notifier = Notifier(sample_config)

        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(return_value=True)
        notifier.set_telegram_bot(mock_bot)

        result = await notifier.send("Test message")

        assert result is True
        mock_bot.send_message.assert_awaited_once_with("Test message")

    async def test_send_without_telegram(self, sample_config):
        notifier = Notifier(sample_config)

        result = await notifier.send("Test message")

        assert result is True
        assert "Test message" in notifier.sent_messages

    async def test_send_trade_alert_respects_config(self, sample_config):
        sample_config.notifications.telegram.alerts.trade_entry = False
        notifier = Notifier(sample_config)

        trade = {
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "quantity": 10,
            "entry_price": 2500.0,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
            "mode": "paper",
        }
        await notifier.send_trade_alert(trade)

        # Should log locally but not call send() (which would send to Telegram)
        assert len(notifier.sent_messages) == 1  # logged locally

    async def test_telegram_send_failure_handled(self, sample_config):
        sample_config.notifications.telegram.enabled = True
        notifier = Notifier(sample_config)

        mock_bot = AsyncMock()
        mock_bot.send_message = AsyncMock(side_effect=Exception("Network error"))
        notifier.set_telegram_bot(mock_bot)

        # Should not raise, just log warning
        result = await notifier.send("Test message")
        assert result is True  # console delivery still succeeded


class TestTelegramBot:
    def test_bot_disabled_without_token(self, app_context):
        app_context.config.notifications.telegram.enabled = True
        app_context.config.notifications.telegram.bot_token = SecretStr("")
        bot = TelegramBot(app_context)

        assert not bot.enabled

    def test_bot_disabled_when_not_enabled(self, app_context):
        app_context.config.notifications.telegram.enabled = False
        app_context.config.notifications.telegram.bot_token = SecretStr("test-token")
        bot = TelegramBot(app_context)

        assert not bot.enabled

    def test_bot_enabled_with_token(self, app_context):
        app_context.config.notifications.telegram.enabled = True
        app_context.config.notifications.telegram.bot_token = SecretStr("test-token")
        bot = TelegramBot(app_context)

        assert bot.enabled

    async def test_send_message_when_disabled(self, app_context):
        bot = TelegramBot(app_context)

        result = await bot.send_message("test")
        assert result is False


class TestKillSwitchCommands:
    """Test that kill switch skill uses correct DB method."""

    async def test_stop_sets_system_state(self, app_context):
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(app_context)
        result = await skill.execute(command="stop")

        assert result.success
        # kill_switch now writes two keys: the binary active flag
        # plus the granular mode (pause / stop / kill / ""). The
        # assertion has to use assert_any_await because the granular
        # mode is the LAST call and assert_awaited_with only matches
        # that one.
        app_context.db.set_system_state.assert_any_await("kill_switch", "active")
        app_context.db.set_system_state.assert_any_await("kill_switch_mode", "stop")

    async def test_resume_clears_system_state(self, app_context):

        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(app_context)

        # Mock health check to avoid NotImplementedError in _check_disk_space
        with patch(
            "yolovest.skills.health_check.HealthCheckSkill.execute",
            new_callable=AsyncMock,
        ) as mock_health:
            from yolovest.skills.base import SkillResult

            mock_health.return_value = SkillResult(
                success=True,
                skill_name="health-check",
                data={"all_healthy": True, "checks": {}},
            )
            result = await skill.execute(command="resume")

        assert result.success
        # See test_stop_sets_system_state for why assert_any_await is
        # used here — kill_switch_mode is cleared as a second call.
        app_context.db.set_system_state.assert_any_await("kill_switch", "inactive")
        app_context.db.set_system_state.assert_any_await("kill_switch_mode", "")


class TestBrokerLoginUrl:
    def test_zerodha_login_url(self):
        from urllib.parse import parse_qs, urlparse

        from yolovest.broker.zerodha import ZerodhaBroker

        broker = ZerodhaBroker(api_key="test_key", api_secret="test_secret")
        url = broker.get_login_url()

        # Check the host exactly (not a substring — "kite.zerodha.com" is a
        # substring of "kite.zerodha.com.evil.com" too) and the api_key via
        # the parsed query rather than a loose `in url`.
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "kite.zerodha.com"
        assert parse_qs(parsed.query).get("api_key") == ["test_key"]
