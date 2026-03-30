"""Telegram bot for YoloVest.

Handles:
- Real-time trade alerts
- Kill switch commands: /stop, /kill, /resume
- Daily auth token flow: /auth <request_token>
- Status commands: /status, /pnl, /positions

Uses python-telegram-bot async API. Runs as a background task
alongside the heartbeat orchestrator.
"""

import asyncio
import logging
from typing import Any

from yolovest.context import AppContext

logger = logging.getLogger(__name__)


def _fmt_inr(n: float, decimals: int = 2) -> str:
    """Format a number using Indian numbering system (lakhs/crores).

    Examples: 1,00,000.00  12,34,567.50  5,00,00,000.00
    """
    if n < 0:
        return "-" + _fmt_inr(-n, decimals)
    rounded = round(n, decimals) if decimals > 0 else round(n)
    integer = int(rounded)
    if decimals > 0:
        frac = abs(rounded - integer)
        decimal_part = f"{frac:.{decimals}f}"[1:]  # ".XX"
    else:
        decimal_part = ""
    s = str(integer)
    if len(s) <= 3:
        return s + decimal_part
    # Last 3 digits, then groups of 2 from right
    result = s[-3:]
    s = s[:-3]
    while s:
        result = s[-2:] + "," + result
        s = s[:-2]
    return result + decimal_part


class TelegramBot:
    """Telegram bot for YoloVest commands and alerts."""

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._cfg = ctx.config.notifications.telegram
        self._bot: Any = None
        self._app: Any = None
        self._stop_event: asyncio.Event = asyncio.Event()

    @property
    def enabled(self) -> bool:
        token = self._cfg.bot_token.get_secret_value()
        return self._cfg.enabled and bool(token)

    async def start(self) -> None:
        """Start the Telegram bot (long-polling)."""
        if not self.enabled:
            logger.info("Telegram bot disabled (no token or not enabled)")
            return

        try:
            from telegram.ext import (
                ApplicationBuilder,
                CommandHandler,
            )
        except ImportError:
            logger.warning("python-telegram-bot not installed, Telegram bot disabled")
            return

        self._app = (
            ApplicationBuilder()
            .token(self._cfg.bot_token.get_secret_value())
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
        self._app.add_handler(CommandHandler("dashboard", self._cmd_dashboard))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("approve", self._cmd_approve))
        self._app.add_handler(CommandHandler("reject", self._cmd_reject))

        logger.info("Telegram bot starting (polling)")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        # Block until stop is requested — keeps the task alive and cancellable.
        # When the task is cancelled (Ctrl+C), CancelledError propagates and
        # the finally block ensures the updater is stopped promptly.
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            logger.info("Telegram bot task cancelled, stopping updater")
            raise

    async def stop(self) -> None:
        """Stop the Telegram bot with timeouts to avoid hanging on shutdown."""
        self._stop_event.set()
        if self._app:
            # Short timeouts — the polling task should already be cancelled
            # by the time stop() is called, so these are just cleanup.
            try:
                await asyncio.wait_for(self._app.updater.stop(), timeout=1.5)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Telegram updater stop timed out")
            try:
                await asyncio.wait_for(self._app.stop(), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Telegram app stop timed out")
            try:
                await asyncio.wait_for(self._app.shutdown(), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Telegram app shutdown timed out")

    async def send_message(self, text: str) -> bool:
        """Send a message to the configured chat_id."""
        if not self.enabled or not self._cfg.chat_id:
            return False

        try:
            if self._app:
                # Use the already-initialized bot from the running Application
                await self._app.bot.send_message(
                    chat_id=self._cfg.chat_id,
                    text=text,
                    parse_mode="HTML",
                )
            else:
                # Fallback: create and initialize a standalone bot
                from telegram import Bot

                bot = Bot(token=self._cfg.bot_token.get_secret_value())
                async with bot:
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
            "/auth <token> — Daily Kite auth\n"
            "/dashboard — High-level overview"
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        """Handle /status command — system status + integration health."""
        db_ok = await self._ctx.db.health_check()
        kill_active = await self._ctx.db.is_kill_switch_active()
        positions = await self._ctx.db.get_open_positions()
        mode = self._ctx.config.mode

        # Integration checks
        gemini_ok = False
        try:
            gemini_ok = await self._ctx.llm.ping()
        except Exception:
            pass

        broker_ok = False
        try:
            broker_ok = await self._ctx.broker.is_authenticated()
        except Exception:
            pass

        market_data_ok = False
        try:
            market_data_ok = await self._ctx.market_data.health_check()
        except Exception:
            pass

        def icon(ok: bool) -> str:
            return "OK" if ok else "DOWN"

        status_text = (
            f"<b>YoloVest Status</b>\n"
            f"Mode: {mode.upper()}\n"
            f"Kill Switch: {'ACTIVE' if kill_active else 'Off'}\n"
            f"Open Positions: {len(positions)}\n"
            f"\n<b>Integrations</b>\n"
            f"Database: {icon(db_ok)}\n"
            f"Gemini LLM: {icon(gemini_ok)}\n"
            f"Zerodha Broker: {icon(broker_ok)}\n"
            f"Market Data: {icon(market_data_ok)}\n"
            f"Telegram: OK"  # If we're receiving this, Telegram works
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
            f"Total: {sign}₹{_fmt_inr(total_pnl)}\n"
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
                f"qty={pos.get('quantity', 0)} @ ₹{_fmt_inr(pos.get('entry_price', 0))}"
            )
        await update.message.reply_html("\n".join(lines))

    async def _cmd_stop(self, update: Any, context: Any) -> None:
        """Handle /stop — pause trading."""
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
        """Handle /kill — square off everything."""
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(self._ctx)
        result = await skill.execute(command="kill")

        pnl = result.data.get("total_pnl", 0)
        await update.message.reply_html(
            f"<b>KILLED</b>\n"
            f"All positions squared off. PnL: ₹{_fmt_inr(pnl)}\n"
            "Use /resume to restart."
        )

    async def _cmd_resume(self, update: Any, context: Any) -> None:
        """Handle /resume — resume trading."""
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(self._ctx)
        result = await skill.execute(command="resume")

        healthy = result.data.get("system_healthy", False)
        status = "All systems healthy." if healthy else "WARNING: Some systems unhealthy."
        await update.message.reply_html(f"<b>RESUMED</b>\n{status}")

    async def _cmd_auth(self, update: Any, context: Any) -> None:
        """Handle /auth <request_token> — daily Kite authentication."""
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

    async def _cmd_dashboard(self, update: Any, context: Any) -> None:
        """Handle /dashboard — high-level overview of portfolio, trades, and system."""
        portfolio = await self._ctx.db.get_portfolio_state()
        positions = await self._ctx.db.get_open_positions()
        todays_trades = await self._ctx.db.get_todays_trades()
        kill_active = await self._ctx.db.is_kill_switch_active()

        # Compute today's stats
        total_pnl = sum(t.get("pnl", 0) for t in todays_trades if t.get("pnl") is not None)
        wins = sum(1 for t in todays_trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in todays_trades if (t.get("pnl") or 0) < 0)
        open_trades = sum(1 for t in todays_trades if t.get("pnl") is None)

        total_capital = portfolio.get("total_capital", 0)
        available_cash = portfolio.get("available_cash", 0)
        exposure_pct = portfolio.get("exposure_pct", 0) * 100
        daily_pnl_pct = portfolio.get("daily_pnl_pct", 0)
        weekly_pnl_pct = portfolio.get("weekly_pnl_pct", 0)

        # Position summary
        pos_lines = []
        for p in positions[:5]:  # Top 5 positions
            symbol = p.get("symbol", "?")
            signal = p.get("signal_type", "?")
            qty = p.get("quantity", 0)
            entry = p.get("entry_price", 0)
            pos_lines.append(f"  {signal} {symbol} x{qty} @ ₹{_fmt_inr(entry, 0)}")
        if len(positions) > 5:
            pos_lines.append(f"  ... and {len(positions) - 5} more")

        sign_d = "+" if daily_pnl_pct >= 0 else ""
        sign_w = "+" if weekly_pnl_pct >= 0 else ""
        sign_p = "+" if total_pnl >= 0 else ""

        msg = (
            f"<b>YoloVest Dashboard</b>\n"
            f"Mode: {self._ctx.config.mode.upper()}"
            f"{' | KILL SWITCH ACTIVE' if kill_active else ''}\n"
            f"\n<b>Portfolio</b>\n"
            f"Capital: ₹{_fmt_inr(total_capital, 0)}\n"
            f"Cash: ₹{_fmt_inr(available_cash, 0)}\n"
            f"Exposure: {exposure_pct:.1f}%\n"
            f"Daily PnL: {sign_d}{daily_pnl_pct:.2f}%\n"
            f"Weekly PnL: {sign_w}{weekly_pnl_pct:.2f}%\n"
            f"\n<b>Today's Activity</b>\n"
            f"Trades: {len(todays_trades)} (W:{wins} L:{losses} Open:{open_trades})\n"
            f"PnL: {sign_p}₹{_fmt_inr(total_pnl)}\n"
        )

        if positions:
            msg += f"\n<b>Open Positions ({len(positions)})</b>\n"
            msg += "\n".join(pos_lines)

        await update.message.reply_html(msg)

    async def _cmd_pending(self, update: Any, context: Any) -> None:
        """Handle /pending — show trades awaiting manual approval."""
        pending = await self._ctx.db.get_pending_trades()
        if not pending:
            await update.message.reply_text("No pending trades.")
            return

        lines = []
        for t in pending:
            lines.append(
                f"#{t['id']} {t['signal_type']} {t['symbol']} "
                f"@ ₹{t['entry_price']:.2f} "
                f"(conf {(t.get('confidence_score') or 0):.0%})"
            )
        msg = "<b>Pending Trades</b>\n\n" + "\n".join(lines)
        msg += "\n\n/approve <id> or /reject <id>"
        await update.message.reply_html(msg)

    async def _cmd_approve(self, update: Any, context: Any) -> None:
        """Handle /approve <id> — approve a pending trade for execution."""
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /approve <id>")
            return

        try:
            trade_id = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid trade ID.")
            return

        signal = await self._ctx.db.decide_pending_trade(
            trade_id, "approved", "telegram",
        )
        if signal is None:
            await update.message.reply_text(f"Trade #{trade_id} not found or already decided.")
            return

        # Execute the approved trade
        from yolovest.skills.trade_execute import TradeExecuteSkill
        skill = TradeExecuteSkill(self._ctx)
        result = await skill.execute(signal=signal)

        if result.success:
            trade = result.data.get("trade", {}) if result.data else {}
            await update.message.reply_html(
                f"Approved & executed: {trade.get('signal_type')} {trade.get('symbol')} "
                f"qty={trade.get('quantity')} @ ₹{trade.get('fill_price', 0):.2f}"
            )
        else:
            await update.message.reply_text(f"Approved but execution failed: {result.error}")

    async def _cmd_reject(self, update: Any, context: Any) -> None:
        """Handle /reject <id> — reject a pending trade."""
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /reject <id>")
            return

        try:
            trade_id = int(args[0])
        except ValueError:
            await update.message.reply_text("Invalid trade ID.")
            return

        # Check if trade exists and is pending before deciding
        pending = await self._ctx.db.get_pending_trades()
        if not any(t["id"] == trade_id for t in pending):
            await update.message.reply_text(f"Trade #{trade_id} not found or already decided.")
            return

        await self._ctx.db.decide_pending_trade(trade_id, "rejected", "telegram")
        await update.message.reply_text(f"Rejected trade #{trade_id}.")
