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
import math
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
        self._app.add_handler(CommandHandler("trade", self._cmd_trade))
        self._app.add_handler(CommandHandler("holiday", self._cmd_holiday))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

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
        """Handle /start — quick status summary."""
        mode = self._ctx.config.mode.upper()
        kill_active = await self._ctx.db.is_kill_switch_active()
        positions = await self._ctx.db.get_open_positions()
        trades = await self._ctx.db.get_todays_trades()
        pending = await self._ctx.db.get_pending_trades()
        total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
        sign = "+" if total_pnl >= 0 else ""

        msg = (
            f"<b>YoloVest</b> — {mode}"
            f"{' | PAUSED' if kill_active else ''}\n"
            f"Positions: {len(positions)} | "
            f"Trades today: {len(trades)} | "
            f"PnL: {sign}₹{_fmt_inr(total_pnl)}\n"
        )
        if pending:
            msg += f"<b>{len(pending)} pending</b> — /pending to review\n"
        msg += "\nType /help for commands"
        await update.message.reply_html(msg)

    async def _cmd_help(self, update: Any, context: Any) -> None:
        """Handle /help command — full command reference."""
        await update.message.reply_html(
            "<b>YoloVest Commands</b>\n\n"

            "<b>General</b>\n"
            "/start — Quick status summary\n"
            "/help — This reference\n\n"

            "<b>Trading</b>\n"
            "/pending — Show pending trades\n"
            "/approve SYMBOL — Approve as-is\n"
            "/approve SYMBOL BUY 422 427 420 — Full override\n"
            "/approve SYMBOL BUY 422 427 420 CNC 50 — Override + product + qty\n"
            "/approve SYMBOL target 427 — Change target\n"
            "/approve SYMBOL sl 420 — Change SL\n"
            "/approve SYMBOL qty 50 — Change quantity\n"
            "/approve SYMBOL product CNC — Change product\n"
            "/approve SYMBOL BUY — Flip direction\n"
            "/reject SYMBOL — Skip trade\n"
            "/trade BUY RELIANCE 2500 2550 2475 — Manual trade\n"
            "/trade SELL INFY 422 415 427 CNC 50 — With product + qty\n\n"

            "<b>Monitoring</b>\n"
            "/status — System status + integrations\n"
            "/pnl — Today's PnL summary\n"
            "/positions — Open positions\n"
            "/dashboard — Full overview\n\n"

            "<b>Controls</b>\n"
            "/stop — Pause trading (kill switch)\n"
            "/kill — Square off everything + pause\n"
            "/resume — Resume trading\n\n"

            "<b>Setup</b>\n"
            "/auth TOKEN — Daily Kite auth\n"
            "/holiday — List holidays\n"
            "/holiday add YYYY-MM-DD — Add holiday\n"
            "/holiday add today — Add today\n"
            "/holiday rm YYYY-MM-DD — Remove holiday"
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
                f"<b>#{t['id']}</b> {t['signal_type']} <b>{t['symbol']}</b> "
                f"{t.get('product', 'MIS')} x{t.get('position_size', '?')}\n"
                f"    Entry ₹{t['entry_price']:.2f} → Target ₹{t['target_price']:.2f} "
                f"SL ₹{t['stop_loss_price']:.2f} "
                f"(conf {(t.get('confidence_score') or 0):.0%})"
            )
        msg = "<b>Pending Trades</b>\n\n" + "\n\n".join(lines)
        msg += (
            "\n\n<i>/approve SYMBOL</i> — approve as-is\n"
            "<i>/approve SYMBOL BUY 422 427 420 [CNC] [qty]</i> — override\n"
            "<i>/approve SYMBOL target 427</i> — change target\n"
            "<i>/approve SYMBOL sl 420</i> — change SL\n"
            "<i>/approve SYMBOL qty 50</i> — change quantity\n"
            "<i>/reject SYMBOL</i>"
        )
        await update.message.reply_html(msg)

    async def _cmd_approve(self, update: Any, context: Any) -> None:
        """Handle /approve <symbol> [overrides] — approve a pending trade with optional overrides.

        Syntaxes:
            /approve INFY                                   — approve as-is
            /approve INFY BUY 422.50 427.25 420.00          — full override
            /approve INFY BUY 422.50 427.25 420.00 CNC      — full override + product
            /approve INFY BUY 422.50 427.25 420.00 CNC 50   — full override + product + qty
            /approve INFY target 427.25                     — override just target
            /approve INFY sl 420.00                         — override just SL
            /approve INFY qty 50                            — override just quantity
            /approve INFY BUY                               — override just direction (flip)
            /approve INFY product CNC                       — override just product
        """
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage:\n"
                "/approve SYMBOL — approve as-is\n"
                "/approve SYMBOL BUY/SELL — flip direction\n"
                "/approve SYMBOL target <price> — override target\n"
                "/approve SYMBOL sl <price> — override SL\n"
                "/approve SYMBOL qty <number> — override quantity\n"
                "/approve SYMBOL product MIS/CNC — override product\n"
                "/approve SYMBOL BUY 422.50 427.25 420.00 [CNC] [qty] — full override"
            )
            return

        # Resolve symbol to pending trade ID
        symbol = args[0].upper()
        original = await self._ctx.db.get_pending_trade_by_symbol(symbol)
        if original is None:
            await update.message.reply_text(f"No pending trade found for {symbol}.")
            return
        trade_id = original["id"]

        # Parse overrides from remaining args
        overrides: dict[str, Any] = {}
        override_notes: list[str] = []

        if len(args) > 1:
            arg1 = args[1].upper()

            if arg1 in ("BUY", "SELL") and len(args) >= 5:
                # Full override: signal_type entry target SL [product] [qty]
                try:
                    overrides["signal_type"] = arg1
                    overrides["entry_price"] = float(args[2])
                    overrides["target_price"] = float(args[3])
                    overrides["stop_loss_price"] = float(args[4])
                except ValueError:
                    await update.message.reply_text(
                        "Invalid prices. Use: /approve SYMBOL BUY/SELL <entry> <target> <SL> [product] [qty]"
                    )
                    return
                if original and original["signal_type"] != arg1:
                    override_notes.append(
                        f"direction flipped from {original['signal_type']} to {arg1}"
                    )
                override_notes.append(
                    f"entry={overrides['entry_price']:.2f}, "
                    f"target={overrides['target_price']:.2f}, "
                    f"SL={overrides['stop_loss_price']:.2f}"
                )
                # Optional 6th arg: product or qty
                for extra_arg in args[5:]:
                    upper = extra_arg.upper()
                    if upper in ("MIS", "CNC"):
                        overrides["product"] = upper
                        override_notes.append(f"product={upper}")
                    else:
                        try:
                            overrides["position_size"] = int(extra_arg)
                            override_notes.append(f"qty={overrides['position_size']}")
                        except ValueError:
                            pass

            elif arg1 in ("BUY", "SELL") and len(args) == 2:
                overrides["signal_type"] = arg1
                if original and original["signal_type"] != arg1:
                    override_notes.append(
                        f"direction flipped from {original['signal_type']} to {arg1}"
                    )
                else:
                    override_notes.append(f"direction set to {arg1}")

            elif arg1 == "TARGET" and len(args) >= 3:
                try:
                    overrides["target_price"] = float(args[2])
                except ValueError:
                    await update.message.reply_text("Invalid target price.")
                    return
                override_notes.append(f"target={overrides['target_price']:.2f}")

            elif arg1 == "SL" and len(args) >= 3:
                try:
                    overrides["stop_loss_price"] = float(args[2])
                except ValueError:
                    await update.message.reply_text("Invalid stop-loss price.")
                    return
                override_notes.append(f"SL={overrides['stop_loss_price']:.2f}")

            elif arg1 == "QTY" and len(args) >= 3:
                try:
                    overrides["position_size"] = int(args[2])
                except ValueError:
                    await update.message.reply_text("Invalid quantity.")
                    return
                override_notes.append(f"qty={overrides['position_size']}")

            elif arg1 == "PRODUCT" and len(args) >= 3:
                product = args[2].upper()
                if product not in ("MIS", "CNC"):
                    await update.message.reply_text("Invalid product. Use MIS or CNC.")
                    return
                overrides["product"] = product
                override_notes.append(f"product={product}")

            else:
                await update.message.reply_text(
                    "Unrecognized override. Use:\n"
                    "/approve SYMBOL BUY/SELL — flip direction\n"
                    "/approve SYMBOL target <price>\n"
                    "/approve SYMBOL sl <price>\n"
                    "/approve SYMBOL qty <number>\n"
                    "/approve SYMBOL product MIS/CNC\n"
                    "/approve SYMBOL BUY 422.50 427.25 420.00 [CNC] [qty]"
                )
                return

        signal = await self._ctx.db.decide_pending_trade(
            trade_id, "approved", "telegram",
            overrides=overrides if overrides else None,
        )
        if signal is None:
            await update.message.reply_text(f"Trade for {symbol} not found or already decided.")
            return

        # Execute the approved trade
        from yolovest.skills.trade_execute import TradeExecuteSkill
        skill = TradeExecuteSkill(self._ctx)
        result = await skill.execute(signal=signal)

        if result.success:
            trade = result.data.get("trade", {}) if result.data else {}
            msg = (
                f"Approved & executed: {trade.get('signal_type')} {trade.get('symbol')} "
                f"{trade.get('product', 'MIS')} qty={trade.get('quantity')} "
                f"@ ₹{trade.get('fill_price', 0):.2f}\n"
                f"  Target: ₹{trade.get('target_price', 0):.2f} | "
                f"SL: ₹{trade.get('stop_loss_price', 0):.2f}"
            )
            if override_notes:
                msg += f"\n  [OVERRIDE: {'; '.join(override_notes)}]"
            await update.message.reply_html(msg)
        else:
            await update.message.reply_text(f"Approved but execution failed: {result.error}")

    async def _cmd_reject(self, update: Any, context: Any) -> None:
        """Handle /reject <symbol> — reject a pending trade."""
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /reject SYMBOL")
            return

        symbol = args[0].upper()
        trade = await self._ctx.db.get_pending_trade_by_symbol(symbol)
        if trade is None:
            await update.message.reply_text(f"No pending trade found for {symbol}.")
            return

        await self._ctx.db.decide_pending_trade(trade["id"], "rejected", "telegram")
        await update.message.reply_text(f"Rejected {trade['signal_type']} {symbol}.")

    async def _cmd_trade(self, update: Any, context: Any) -> None:
        """Handle /trade — place a manual trade.

        Syntax:
            /trade BUY RELIANCE 2500 2550 2475         — BUY symbol entry target SL (MIS)
            /trade SELL INFY 422.50 415.80 427.00 CNC  — with explicit product
            /trade BUY TCS 3500 3600 3450 CNC 50       — with product and qty
        """
        args = context.args
        if not args or len(args) < 5:
            await update.message.reply_text(
                "Usage: /trade BUY/SELL SYMBOL ENTRY TARGET SL [product] [qty]\n\n"
                "Examples:\n"
                "/trade BUY RELIANCE 2500 2550 2475\n"
                "/trade SELL INFY 422.50 415.80 427.00 CNC\n"
                "/trade BUY TCS 3500 3600 3450 CNC 50"
            )
            return

        # Parse signal_type
        signal_type = args[0].upper()
        if signal_type not in ("BUY", "SELL"):
            await update.message.reply_text("First argument must be BUY or SELL.")
            return

        # Parse symbol
        symbol = args[1].upper()

        # Parse prices
        try:
            entry_price = float(args[2])
            target_price = float(args[3])
            stop_loss_price = float(args[4])
        except ValueError:
            await update.message.reply_text(
                "Invalid price values. Entry, target, and SL must be numbers."
            )
            return

        if entry_price <= 0 or target_price <= 0 or stop_loss_price <= 0:
            await update.message.reply_text("All prices must be positive.")
            return

        # Validate SL direction
        if signal_type == "BUY" and stop_loss_price >= entry_price:
            await update.message.reply_text("For BUY, stop-loss must be below entry price.")
            return
        if signal_type == "SELL" and stop_loss_price <= entry_price:
            await update.message.reply_text("For SELL, stop-loss must be above entry price.")
            return

        # Parse optional product (default MIS)
        product = "MIS"
        explicit_qty: int | None = None
        if len(args) >= 6:
            if args[5].upper() in ("MIS", "CNC"):
                product = args[5].upper()
            else:
                # Maybe it's qty directly (no product specified)
                try:
                    explicit_qty = int(args[5])
                except ValueError:
                    await update.message.reply_text(
                        f"Invalid product or qty: '{args[5]}'. Product must be MIS or CNC."
                    )
                    return

        # Parse optional qty
        if len(args) >= 7 and explicit_qty is None:
            try:
                explicit_qty = int(args[6])
            except ValueError:
                await update.message.reply_text(f"Invalid qty: '{args[6]}'. Must be an integer.")
                return

        # Compute position size if not provided
        if explicit_qty is not None:
            position_size = explicit_qty
        else:
            # qty = floor(capital * risk_per_trade / abs(entry - sl))
            try:
                cap_str = await self._ctx.db.get_system_state("initial_capital")
                capital = float(cap_str) if cap_str else 100_000.0
            except (ValueError, TypeError):
                capital = 100_000.0

            risk_pct = self._ctx.config.risk.max_risk_per_trade_pct
            risk_per_share = abs(entry_price - stop_loss_price)
            if risk_per_share <= 0:
                await update.message.reply_text("Entry and SL prices cannot be equal.")
                return
            position_size = max(1, math.floor(capital * risk_pct / risk_per_share))

        # Build signal dict
        signal = {
            "symbol": symbol,
            "signal_type": signal_type,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_loss_price": stop_loss_price,
            "position_size": position_size,
            "product": product,
            "source": "manual_telegram",
        }

        try:
            # Insert as manual trade (pre-approved)
            await self._ctx.db.insert_manual_trade(
                {**signal, "decided_by": "telegram"},
            )

            # Execute via TradeExecuteSkill
            from yolovest.skills.trade_execute import TradeExecuteSkill
            skill = TradeExecuteSkill(self._ctx)
            result = await skill.execute(signal=signal)

            if result.success:
                trade = result.data.get("trade", {}) if result.data else {}
                await update.message.reply_html(
                    f"<b>Manual trade executed</b>\n"
                    f"{trade.get('signal_type', signal_type)} {trade.get('symbol', symbol)} "
                    f"{trade.get('product', product)} "
                    f"qty={trade.get('quantity', position_size)} "
                    f"@ ₹{trade.get('fill_price', entry_price):.2f}\n"
                    f"  Target: ₹{target_price:.2f} | SL: ₹{stop_loss_price:.2f}"
                )
            else:
                await update.message.reply_text(
                    f"Trade recorded but execution failed: {result.error}"
                )
        except Exception as e:
            logger.error("Manual trade failed: %s", e, exc_info=True)
            await update.message.reply_text(f"Trade failed: {e}")

    async def _cmd_holiday(self, update: Any, context: Any) -> None:
        """Handle /holiday — manage NSE holidays.

        /holiday              — list upcoming holidays
        /holiday add 2026-04-14  — add a holiday
        /holiday add 2026-04-14 13:00  — add early close day
        /holiday rm 2026-04-14   — remove a holiday
        """
        import json as _json
        import re as _re
        from datetime import date

        args = context.args or []

        if not args:
            # List holidays
            holidays = sorted(self._ctx.config.market_hours.holidays)
            ec = self._ctx.config.market_hours.early_close_days
            today = date.today().isoformat()
            upcoming = [h for h in holidays if h >= today]
            upcoming_ec = {k: v for k, v in sorted(ec.items()) if k >= today}

            lines = ["<b>NSE Holidays</b>"]
            if upcoming:
                for h in upcoming[:15]:
                    d = date.fromisoformat(h)
                    lines.append(f"  {h} ({d.strftime('%a')})")
                if len(upcoming) > 15:
                    lines.append(f"  ... and {len(upcoming) - 15} more")
            else:
                lines.append("  No upcoming holidays")

            if upcoming_ec:
                lines.append("\n<b>Early Close Days</b>")
                for d_str, t in list(upcoming_ec.items())[:10]:
                    d = date.fromisoformat(d_str)
                    lines.append(f"  {d_str} ({d.strftime('%a')}) closes {t}")

            lines.append(
                "\n<i>/holiday add YYYY-MM-DD</i> — add holiday\n"
                "<i>/holiday add today|tomorrow</i> — shorthand\n"
                "<i>/holiday add YYYY-MM-DD HH:MM</i> — early close\n"
                "<i>/holiday rm YYYY-MM-DD|today|tomorrow</i> — remove"
            )
            await update.message.reply_html("\n".join(lines))
            return

        action = args[0].lower()

        if action == "add" and len(args) >= 2:
            date_str = args[1].lower()
            # Support "today" and "tomorrow" aliases
            from datetime import timedelta
            if date_str == "today":
                date_str = date.today().isoformat()
            elif date_str == "tomorrow":
                date_str = (date.today() + timedelta(days=1)).isoformat()
            if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                await update.message.reply_text("Invalid date. Use YYYY-MM-DD, 'today', or 'tomorrow'.")
                return

            early_close = args[2] if len(args) >= 3 else None
            if early_close and not _re.match(r"^\d{2}:\d{2}$", early_close):
                await update.message.reply_text("Invalid time. Use HH:MM format.")
                return

            if early_close:
                ec = dict(self._ctx.config.market_hours.early_close_days)
                ec[date_str] = early_close
                self._ctx.config.market_hours.early_close_days = ec
                await self._ctx.db.set_config(
                    "market_hours.early_close_days", _json.dumps(ec),
                )
                from yolovest.context import MarketHoursChecker
                self._ctx.market_hours = MarketHoursChecker(self._ctx.config)
                await update.message.reply_html(
                    f"Added early close: <b>{date_str}</b> at {early_close}"
                )
            else:
                holidays = list(self._ctx.config.market_hours.holidays)
                if date_str not in holidays:
                    holidays.append(date_str)
                    holidays.sort()
                self._ctx.config.market_hours.holidays = holidays
                await self._ctx.db.set_config(
                    "market_hours.holidays", _json.dumps(holidays),
                )
                from yolovest.context import MarketHoursChecker
                self._ctx.market_hours = MarketHoursChecker(self._ctx.config)
                d = date.fromisoformat(date_str)
                await update.message.reply_html(
                    f"Added holiday: <b>{date_str}</b> ({d.strftime('%A')})"
                )
            return

        if action == "rm" and len(args) >= 2:
            date_str = args[1].lower()
            from datetime import timedelta
            if date_str == "today":
                date_str = date.today().isoformat()
            elif date_str == "tomorrow":
                date_str = (date.today() + timedelta(days=1)).isoformat()
            removed = False

            holidays = list(self._ctx.config.market_hours.holidays)
            if date_str in holidays:
                holidays.remove(date_str)
                self._ctx.config.market_hours.holidays = holidays
                await self._ctx.db.set_config(
                    "market_hours.holidays", _json.dumps(holidays),
                )
                removed = True

            ec = dict(self._ctx.config.market_hours.early_close_days)
            if date_str in ec:
                del ec[date_str]
                self._ctx.config.market_hours.early_close_days = ec
                await self._ctx.db.set_config(
                    "market_hours.early_close_days", _json.dumps(ec),
                )
                removed = True

            if removed:
                from yolovest.context import MarketHoursChecker
                self._ctx.market_hours = MarketHoursChecker(self._ctx.config)
                await update.message.reply_html(f"Removed: <b>{date_str}</b>")
            else:
                await update.message.reply_text(f"{date_str} not found in holidays.")
            return

        await update.message.reply_text(
            "Usage:\n/holiday — list\n/holiday add YYYY-MM-DD — add\n"
            "/holiday add today|tomorrow — shorthand\n"
            "/holiday add YYYY-MM-DD HH:MM — early close\n"
            "/holiday rm YYYY-MM-DD|today|tomorrow — remove"
        )
