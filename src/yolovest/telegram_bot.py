"""Telegram bot for YoloVest.

Handles:
- Real-time trade alerts (FR-8.6)
- Kill switch commands: /stop, /kill, /resume (FR-5.14)
- Daily auth token flow: /auth <request_token> (FR-6.3)
- Status commands: /status, /pnl, /positions

Uses python-telegram-bot async API. Runs as a background task
alongside the heartbeat orchestrator.
"""

import asyncio
import logging
from typing import Any

from yolovest.context import AppContext

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot for YoloVest commands and alerts."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._cfg = ctx.config.notifications.telegram
        self._bot: Any = None
        self._app: Any = None

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled and bool(self._cfg.bot_token)

    async def start(self) -> None:
        """Start the Telegram bot (long-polling)."""
        if not self.enabled:
            logger.info("Telegram bot disabled (no token or not enabled)")
            return

        try:
            from telegram import Update
            from telegram.ext import (
                ApplicationBuilder,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.warning("python-telegram-bot not installed, Telegram bot disabled")
            return

        self._app = (
            ApplicationBuilder()
            .token(self._cfg.bot_token)
            .build()
        )

        # Register command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(CommandHandler("kill", self._cmd_kill))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("auth", self._cmd_auth))

        logger.info("Telegram bot starting (polling)")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send_message(self, text: str) -> bool:
        """Send a message to the configured chat_id."""
        if not self.enabled or not self._cfg.chat_id:
            return False

        try:
            from telegram import Bot

            bot = Bot(token=self._cfg.bot_token)
            await bot.send_message(
                chat_id=self._cfg.chat_id,
                text=text,
                parse_mode="HTML",
            )
            return True
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Any, context: Any) -> None:
        """Handle /start command."""
        chat_id = update.effective_chat.id
        await update.message.reply_text(
            f"YoloVest Bot\n"
            f"Chat ID: {chat_id}\n\n"
            "Commands:\n"
            "/status — System status\n"
            "/pnl — Today's PnL\n"
            "/positions — Open positions\n"
            "/stop — Pause trading\n"
            "/kill — Square off everything\n"
            "/resume — Resume trading\n"
            "/auth <token> — Daily Kite auth"
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        """Handle /status command."""
        db_ok = await self._ctx.db.health_check()
        kill_active = await self._ctx.db.is_kill_switch_active()
        positions = await self._ctx.db.get_open_positions()
        mode = self._ctx.config.mode

        status_text = (
            f"<b>YoloVest Status</b>\n"
            f"Mode: {mode.upper()}\n"
            f"Database: {'OK' if db_ok else 'DOWN'}\n"
            f"Kill Switch: {'ACTIVE' if kill_active else 'Off'}\n"
            f"Open Positions: {len(positions)}"
        )
        await update.message.reply_html(status_text)

    async def _cmd_pnl(self, update: Any, context: Any) -> None:
        """Handle /pnl command."""
        trades = await self._ctx.db.get_todays_trades()
        total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
        wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in trades if (t.get("pnl") or 0) < 0)

        sign = "+" if total_pnl >= 0 else ""
        await update.message.reply_html(
            f"<b>Today's PnL</b>\n"
            f"Total: {sign}₹{total_pnl:,.2f}\n"
            f"Trades: {len(trades)} (W:{wins} L:{losses})"
        )

    async def _cmd_positions(self, update: Any, context: Any) -> None:
        """Handle /positions command."""
        positions = await self._ctx.db.get_open_positions()
        if not positions:
            await update.message.reply_text("No open positions.")
            return

        lines = ["<b>Open Positions</b>"]
        for pos in positions:
            lines.append(
                f"  {pos.get('signal_type', '?')} {pos.get('symbol', '?')} "
                f"qty={pos.get('quantity', 0)} @ {pos.get('entry_price', 0):.2f}"
            )
        await update.message.reply_html("\n".join(lines))

    async def _cmd_stop(self, update: Any, context: Any) -> None:
        """Handle /stop — pause trading (FR-5.14)."""
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(self._ctx)
        result = await skill.execute(command="stop")

        cancelled = result.data.get("orders_cancelled", 0)
        await update.message.reply_html(
            f"<b>STOPPED</b>\n"
            f"Trading paused. {cancelled} orders cancelled.\n"
            "Use /resume to restart."
        )

    async def _cmd_kill(self, update: Any, context: Any) -> None:
        """Handle /kill — square off everything (FR-5.14)."""
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(self._ctx)
        result = await skill.execute(command="kill")

        pnl = result.data.get("total_pnl", 0)
        await update.message.reply_html(
            f"<b>KILLED</b>\n"
            f"All positions squared off. PnL: ₹{pnl:,.2f}\n"
            "Use /resume to restart."
        )

    async def _cmd_resume(self, update: Any, context: Any) -> None:
        """Handle /resume — resume trading (FR-5.14)."""
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(self._ctx)
        result = await skill.execute(command="resume")

        healthy = result.data.get("system_healthy", False)
        status = "All systems healthy." if healthy else "WARNING: Some systems unhealthy."
        await update.message.reply_html(f"<b>RESUMED</b>\n{status}")

    async def _cmd_auth(self, update: Any, context: Any) -> None:
        """Handle /auth <request_token> — daily Kite authentication (FR-6.3)."""
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /auth <request_token>\n"
                "Get the token from Kite login redirect URL."
            )
            return

        request_token = args[0]
        try:
            await self._ctx.broker.authenticate(request_token)
            margins = await self._ctx.broker.get_margins()
            cash = margins.get("available_cash", margins.get("equity", {}).get("available", "?"))
            await update.message.reply_html(
                f"<b>Authenticated</b>\nAvailable cash: ₹{cash}"
            )
        except Exception as e:
            await update.message.reply_text(f"Auth failed: {e}")
