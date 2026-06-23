"""Telegram bot for YoloVest.

Handles:
- Real-time trade alerts
- Kill switch commands: /pause, /stop, /kill, /resume
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
            from telegram import Update
            from telegram.ext import (
                ApplicationBuilder,
                CommandHandler,
                TypeHandler,
            )
        except ImportError:
            logger.warning("python-telegram-bot not installed, Telegram bot disabled")
            return

        self._app = (
            ApplicationBuilder()
            .token(self._cfg.bot_token.get_secret_value())
            .build()
        )

        # Authorization gate — group -1 runs before every command handler.
        # The bot token only authenticates US to Telegram; anyone who finds
        # the bot's username can message it. Without this, a stranger could
        # issue /kill, /trade, /auth or /mode live.
        self._app.add_handler(TypeHandler(Update, self._authorize_update), group=-1)

        # Register command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(CommandHandler("kill", self._cmd_kill))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("auth", self._cmd_auth))
        self._app.add_handler(CommandHandler("dashboard", self._cmd_dashboard))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("approve", self._cmd_approve))
        self._app.add_handler(CommandHandler("reject", self._cmd_reject))
        self._app.add_handler(CommandHandler("trade", self._cmd_trade))
        self._app.add_handler(CommandHandler("clear", self._cmd_clear_signals))
        self._app.add_handler(CommandHandler("review", self._cmd_review))
        self._app.add_handler(CommandHandler("skills", self._cmd_skills))
        self._app.add_handler(CommandHandler("run", self._cmd_run_skill))
        self._app.add_handler(CommandHandler("holiday", self._cmd_holiday))
        self._app.add_handler(CommandHandler("watch", self._cmd_watch))
        self._app.add_handler(CommandHandler("quarantine", self._cmd_quarantine))
        self._app.add_handler(CommandHandler("lock", self._cmd_lock))
        self._app.add_handler(CommandHandler("unlock", self._cmd_unlock))
        self._app.add_handler(CommandHandler("mode", self._cmd_mode))
        self._app.add_handler(CommandHandler("symbol", self._cmd_symbol))
        self._app.add_handler(CommandHandler("rotation", self._cmd_rotation))
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
            except (TimeoutError, Exception):
                logger.warning("Telegram updater stop timed out")
            try:
                await asyncio.wait_for(self._app.stop(), timeout=1.0)
            except (TimeoutError, Exception):
                logger.warning("Telegram app stop timed out")
            try:
                await asyncio.wait_for(self._app.shutdown(), timeout=1.0)
            except (TimeoutError, Exception):
                logger.warning("Telegram app shutdown timed out")

    async def _authorize_update(self, update: Any, context: Any) -> None:
        """Drop any update that isn't from the configured chat_id.

        Fails closed: when chat_id is unset, every command is blocked
        rather than open to the world. Unauthorized senders get no reply
        (replying would confirm the bot is alive to whoever is probing);
        the attempt is logged at WARNING instead.
        """
        from telegram.ext import ApplicationHandlerStop

        allowed = str(self._cfg.chat_id or "").strip()
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        sender_ids = {
            str(getattr(chat, "id", "") or ""),
            str(getattr(user, "id", "") or ""),
        } - {""}
        if allowed and allowed in sender_ids:
            return  # authorized — command handlers may run
        logger.warning(
            "Telegram: dropped update from unauthorized sender %s (%s)",
            sorted(sender_ids) if sender_ids else "<unknown>",
            "chat_id not configured — all commands blocked"
            if not allowed
            else "chat_id mismatch",
        )
        raise ApplicationHandlerStop

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
        positions = await self._ctx.db.get_open_positions(mode=self._ctx.config.mode)
        trades = await self._ctx.db.get_todays_trades(mode=self._ctx.config.mode)
        pending = await self._ctx.db.get_pending_trades()
        total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
        sign = "+" if total_pnl >= 0 else ""

        system_pos = [p for p in positions if p.get("origin") != "adopted"]
        adopted_pos = [p for p in positions if p.get("origin") == "adopted"]

        msg = (
            f"<b>YoloVest</b> — {mode}"
            f"{' | PAUSED' if kill_active else ''}\n"
            f"Positions: {len(system_pos)}"
        )
        if adopted_pos:
            msg += f" (+{len(adopted_pos)} holdings)"
        msg += (
            f" | Trades today: {len(trades)}"
            f" | PnL: {sign}₹{_fmt_inr(total_pnl)}\n"
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
            "/clear — Clear today's signals &amp; regenerate\n"
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

            "<b>Analysis</b>\n"
            "/review — ML review of all holdings\n"
            "/review GAIL TCS — Review specific holdings\n\n"

            "<b>Monitoring</b>\n"
            "/status — System status + integrations\n"
            "/pnl — Today's PnL summary\n"
            "/positions — Open positions\n"
            "/dashboard — Full overview\n\n"

            "<b>Controls</b>\n"
            "/pause — Block new trades only (broker untouched)\n"
            "/stop — Pause + cancel pending orders\n"
            "/kill — Square off everything + pause\n"
            "/resume — Resume trading\n\n"

            "<b>Skills</b>\n"
            "/skills — List all skills\n"
            "/run SKILL — Execute a skill\n\n"

            "<b>Lists &amp; locks</b>\n"
            "/watch — Show user watchlist\n"
            "/watch add SYM [SYM ...] — Add symbols\n"
            "/watch rm SYM [SYM ...] — Remove\n"
            "/lock SYM [SYM ...] — Protect from auto-sell\n"
            "/lock — List locked\n"
            "/unlock SYM — Allow auto-sell again\n"
            "/quarantine — List quarantined symbols\n"
            "/quarantine unblock SYM — Clear quarantine\n"
            "/quarantine replace SYM NEW — Route SYM → NEW\n\n"

            "<b>Symbol info</b>\n"
            "/symbol SYM — Price, recent trades, model attribution, bulk deals\n\n"

            "<b>Rotation</b>\n"
            "/rotation — Show count of cooldowned symbols + threshold\n"
            "/rotation clear — Reset all cooldowns\n"
            "/rotation clear SYM [SYM ...] — Reset specific symbols\n\n"

            "<b>Mode</b>\n"
            "/mode — Show transaction mode\n"
            "/mode auto — Auto-execute approved signals\n"
            "/mode manual — Require /approve per signal\n\n"

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
            logger.debug("Gemini ping failed during /status", exc_info=True)

        broker_ok = False
        try:
            broker_ok = await self._ctx.broker.is_authenticated()
        except Exception:
            logger.debug("Broker auth check failed during /status", exc_info=True)

        market_data_ok = False
        try:
            market_data_ok = await self._ctx.market_data.health_check()
        except Exception:
            logger.debug("Market data health check failed during /status", exc_info=True)

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
        trades = await self._ctx.db.get_todays_trades(mode=self._ctx.config.mode)
        total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
        wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in trades if (t.get("pnl") or 0) < 0)

        sign = "+" if total_pnl >= 0 else ""
        await update.message.reply_html(
            f"<b>Today's PnL ({self._ctx.config.mode.upper()})</b>\n"
            f"Total: {sign}₹{_fmt_inr(total_pnl)}\n"
            f"Trades: {len(trades)} (W:{wins} L:{losses})"
        )

    async def _cmd_positions(self, update: Any, context: Any) -> None:
        """Handle /positions command."""
        positions = await self._ctx.db.get_open_positions(mode=self._ctx.config.mode)
        if not positions:
            await update.message.reply_text("No open positions.")
            return

        system_pos = [p for p in positions if p.get("origin") != "adopted"]
        adopted_pos = [p for p in positions if p.get("origin") == "adopted"]

        lines = []
        if system_pos:
            lines.append(f"<b>Active Trades ({len(system_pos)})</b>")
            for pos in system_pos:
                lines.append(
                    f"  {pos.get('signal_type', '?')} <b>{pos.get('symbol', '?')}</b> "
                    f"x{pos.get('quantity', 0)} @ ₹{_fmt_inr(pos.get('entry_price', 0))}"
                    f"  SL ₹{_fmt_inr(pos.get('stop_loss_price', 0))} → Target ₹{_fmt_inr(pos.get('target_price', 0))}"
                )

        if adopted_pos:
            lines.append(f"\n<b>Adopted Holdings ({len(adopted_pos)})</b>")
            for pos in adopted_pos:
                lines.append(
                    f"  {pos.get('signal_type', '?')} <b>{pos.get('symbol', '?')}</b> "
                    f"x{pos.get('quantity', 0)} @ ₹{_fmt_inr(pos.get('entry_price', 0))}"
                )

        if not lines:
            await update.message.reply_text("No open positions.")
        else:
            await update.message.reply_html("\n".join(lines))

    async def _cmd_pause(self, update: Any, context: Any) -> None:
        """Handle /pause — block new trades without touching broker state."""
        from yolovest.skills.kill_switch import KillSwitchSkill

        skill = KillSwitchSkill(self._ctx)
        await skill.execute(command="pause")

        await update.message.reply_html(
            "<b>PAUSED</b>\n"
            "New trades blocked. Existing orders, GTTs, and positions "
            "are untouched.\n"
            "Use /resume to restart."
        )

    async def _cmd_stop(self, update: Any, context: Any) -> None:
        """Handle /stop — pause + cancel pending orders."""
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
            # Sync token to Kite data provider (paid data plan)
            from yolovest.main import _sync_kite_data_token
            _sync_kite_data_token(self._ctx)
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

    async def _cmd_clear_signals(self, update: Any, context: Any) -> None:
        """Handle /clear — clear today's signals and pending trades to allow regeneration."""
        result = await self._ctx.db.clear_todays_signals()
        sig = result["signals_deleted"]
        pend = result["pending_deleted"]
        await update.message.reply_html(
            f"<b>Cleared</b>\n"
            f"Signals deleted: {sig}\n"
            f"Pending trades deleted: {pend}\n\n"
            f"Next heartbeat will regenerate fresh signals."
        )

    async def _cmd_review(self, update: Any, context: Any) -> None:
        """Handle /review [SYMBOL ...] — ML review of any NSE symbol (held or
        not), or all holdings when none are given.

        Shares the dashboard's review engine (yolovest.review.review_symbols),
        so it works for any symbol — not just the ingested universe — and the
        DB->provider OHLCV fallback lives in exactly one place.
        """
        from yolovest.review import review_symbols

        args = [a.upper() for a in (context.args or [])]
        if args:
            await update.message.reply_text(
                f"Reviewing {len(args)} symbol{'s' if len(args) != 1 else ''}..."
            )
        else:
            await update.message.reply_text("Reviewing your holdings...")

        result = await review_symbols(self._ctx, args or None)
        recs = result.get("recommendations") or []
        if not recs:
            await update.message.reply_text(
                result.get("error")
                or "Usage: /review SYMBOL [SYMBOL ...]\nOr authenticate with Kite to review all holdings."
            )
            return

        # Sector + quarantine context (presentation only) — pulled once and
        # indexed in-memory so each row's formatting is a dict hit.
        sector_map: dict[str, str] = {}
        quarantined_set: set[str] = set()
        try:
            cur = await self._ctx.db.read_conn.execute(
                "SELECT symbol, COALESCE(sector, industry) FROM symbol_sectors"
            )
            for row in await cur.fetchall():
                if row[0] and row[1]:
                    sector_map[row[0].upper()] = row[1]
        except Exception:
            pass
        try:
            quarantined_set = {
                (q.get("symbol") or "").upper()
                for q in await self._ctx.db.get_quarantined_symbols()
            }
        except Exception:
            pass

        icons = {"BUY": "\U0001F7E2", "BUY_MORE": "\U0001F7E2",
                 "SELL": "\U0001F534", "SHORT": "\U0001F534",
                 "TIGHTEN_SL": "\U0001F7E1"}
        lines = []
        for rec in recs[:15]:
            symbol = rec["symbol"]
            action = rec["action"]
            icon = icons.get(action, "⚪")
            qty = rec.get("quantity") or 0
            held_label = f"x{qty}" if qty > 0 else "not held"
            sector = sector_map.get(symbol.upper(), "")
            sector_str = f" · {sector}" if sector else ""
            quarantine_flag = " ⚠️ QUARANTINED" if symbol.upper() in quarantined_set else ""

            ltp = rec.get("last_price") or 0
            entry = rec.get("average_price") or 0
            pnl = rec.get("pnl_pct") or 0
            if rec.get("held") and entry > 0:
                price_line = f"₹{entry:.2f}→₹{ltp:.2f} ({pnl:+.1f}%)"
            else:
                price_line = f"₹{ltp:.2f}" if ltp > 0 else "LTP unavailable"

            ctx_bits: list[str] = []
            if rec.get("day_change_pct") is not None:
                ctx_bits.append(f"day {rec['day_change_pct']:+.1f}%")
            if rec.get("week_change_pct") is not None:
                ctx_bits.append(f"7d {rec['week_change_pct']:+.1f}%")
            if rec.get("vol_ratio") is not None:
                ctx_bits.append(f"vol {rec['vol_ratio']:.1f}×")
            ctx_line = " | ".join(ctx_bits)

            target_line = ""
            if rec.get("target_price") and rec.get("signal_type") != "HOLD":
                tp_pct = rec.get("target_pct")
                sl_pct = rec.get("sl_pct")
                tp_str = f" ({tp_pct:+.1f}%)" if tp_pct is not None else ""
                sl_str = f" ({sl_pct:+.1f}%)" if sl_pct is not None else ""
                target_line = (
                    f"\n    target ₹{rec['target_price']:.2f}{tp_str}"
                    f" / SL ₹{rec['stop_loss_price']:.2f}{sl_str}"
                )

            header = (
                f"{icon} <b>{symbol}</b>{sector_str} ({held_label})"
                f"{quarantine_flag} — {action.replace('_', ' ')} ({rec.get('confidence', 0):.0%})"
            )
            body_lines = [f"    {price_line}"]
            if ctx_line:
                body_lines.append(f"    {ctx_line}")
            if rec.get("reasoning"):
                body_lines.append(f"    {rec['reasoning']}")
            lines.append(header + "\n" + "\n".join(body_lines) + target_line)

        msg = "<b>Symbol Review</b>\n\n" + "\n\n".join(lines)
        await update.message.reply_html(msg)

    async def _cmd_skills(self, update: Any, context: Any) -> None:
        """Handle /skills — list all registered skills."""
        from yolovest.skills import SKILL_REGISTRY

        lines = []
        for name in sorted(SKILL_REGISTRY):
            cls = SKILL_REGISTRY[name]
            trigger = cls.trigger.value
            lines.append(f"<b>{name}</b> ({trigger}) — {cls.description}")

        msg = "<b>Available Skills</b>\n\n" + "\n".join(lines)
        msg += "\n\n<i>/run SKILL_NAME</i> to execute"
        await update.message.reply_html(msg)

    async def _cmd_run_skill(self, update: Any, context: Any) -> None:
        """Handle /run <skill_name> — execute a skill and report result."""
        from yolovest.skills import SKILL_REGISTRY

        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /run SKILL_NAME\nUse /skills to see available skills."
            )
            return

        skill_name = args[0].lower()
        if skill_name not in SKILL_REGISTRY:
            await update.message.reply_text(
                f"Unknown skill: {skill_name}\n"
                f"Available: {', '.join(sorted(SKILL_REGISTRY.keys()))}"
            )
            return

        await update.message.reply_text(f"Running {skill_name}...")

        skill_cls = SKILL_REGISTRY[skill_name]
        skill = skill_cls(self._ctx)
        result = await skill.safe_execute()

        if result.success:
            # Extract key metrics from result data
            summary_parts = []
            if result.data:
                for k, v in result.data.items():
                    if isinstance(v, (str, int, float, bool)) and k not in ("mode",):
                        summary_parts.append(f"{k}: {v}")
            summary = "\n".join(summary_parts[:10]) if summary_parts else "No details"
            await update.message.reply_html(
                f"<b>{skill_name}</b> completed in {result.duration_ms:.0f}ms\n\n{summary}"
            )
        else:
            await update.message.reply_text(
                f"{skill_name} FAILED ({result.duration_ms:.0f}ms):\n{result.error}"
            )

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
        mode = self._ctx.config.mode
        logger.info(
            "Executing approved trade: %s %s (mode=%s)",
            signal.get("signal_type"), signal.get("symbol"), mode,
        )
        result = await skill.safe_execute(signal=signal)

        if result.success:
            trade = result.data.get("trade", {}) if result.data else {}
            exec_mode = result.data.get("mode", mode) if result.data else mode
            # Mark the originating signal as executed so Today's
            # Recommendations stops showing it as AWAITING APPROVAL.
            try:
                await self._ctx.db.update_signal_disposition(
                    signal.get("symbol", ""), "executed",
                    f"trade_id={trade.get('trade_id') or trade.get('order_id')}",
                    position_size=int(trade.get("quantity") or 0) or None,
                )
            except Exception:
                logger.debug("Failed to mark signal executed", exc_info=True)
            try:
                from yolovest.dashboard.ws import broadcast_ws
                await broadcast_ws("pending_approved", {
                    "trade_id": trade_id, "symbol": signal.get("symbol"),
                })
            except Exception:
                logger.debug("pending_approved broadcast failed", exc_info=True)
            msg = (
                f"<b>Executed ({exec_mode.upper()})</b>: "
                f"{trade.get('signal_type')} <b>{trade.get('symbol')}</b> "
                f"{trade.get('product', 'MIS')} qty={trade.get('quantity')} "
                f"@ ₹{trade.get('fill_price', 0):.2f}\n"
                f"  Target: ₹{trade.get('target_price', 0):.2f} | "
                f"SL: ₹{trade.get('stop_loss_price', 0):.2f}\n"
                f"  Order: {trade.get('order_id', 'N/A')} | "
                f"Trade: {trade.get('trade_id', 'N/A')}"
            )
            if override_notes:
                msg += f"\n  [OVERRIDE: {'; '.join(override_notes)}]"
            await update.message.reply_html(msg)
        else:
            logger.error(
                "Trade execution failed for %s: %s",
                signal.get("symbol"), result.error,
            )
            # Revert pending trade back to 'pending' so user can retry
            sym = signal.get("symbol", "?")
            try:
                await self._ctx.db.conn.execute(
                    "UPDATE pending_trades SET status = 'pending', decided_at = NULL, "
                    "decided_by = NULL WHERE id = ? AND status = 'approved'",
                    (trade_id,),
                )
                await self._ctx.db.conn.commit()
                logger.info("Reverted pending trade #%d (%s) back to pending after execution failure", trade_id, sym)
            except Exception:
                logger.debug("Failed to revert pending trade #%d", trade_id, exc_info=True)
            await update.message.reply_text(
                f"FAILED: {sym} execution error:\n{result.error}\n\n"
                f"Trade reverted to pending — /approve {sym} to retry."
            )

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
        try:
            from yolovest.dashboard.ws import broadcast_ws
            await broadcast_ws("pending_rejected", {
                "trade_id": trade["id"], "symbol": symbol,
            })
        except Exception:
            logger.debug("pending_rejected broadcast failed", exc_info=True)
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

        # Pre-trade sanity check: run the ML review and flag a disagreement.
        # Warn, never block — /trade is a deliberate manual command, and a
        # review hiccup must not stop the placement.
        try:
            from yolovest.review import review_symbols
            recos = (await review_symbols(self._ctx, [symbol])).get("recommendations") or []
            reco = recos[0] if recos else None
            if reco:
                model_sig = reco.get("signal_type")
                conf = reco.get("confidence") or 0
                if model_sig in ("BUY", "SELL") and model_sig != signal_type:
                    await update.message.reply_text(
                        f"⚠️ Heads up: the model signals {model_sig} ({conf:.0%}) on "
                        f"{symbol} — the opposite of your {signal_type}. Placing it anyway."
                    )
                elif model_sig not in ("BUY", "SELL"):
                    await update.message.reply_text(
                        f"⚠️ Heads up: the model sees no clear {signal_type} signal on "
                        f"{symbol} ({str(reco.get('action', 'HOLD')).replace('_', ' ')}). "
                        f"Placing it anyway."
                    )
        except Exception:
            logger.debug("trade: pre-trade review check failed", exc_info=True)

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

    # ------------------------------------------------------------------
    # State-mutation commands — close the dashboard-only gaps so the
    # bot is a complete control surface for autonomous operation.
    # ------------------------------------------------------------------

    async def _cmd_watch(self, update: Any, context: Any) -> None:
        """/watch                  — list user watchlist
        /watch add SYM [SYM ...]   — add one or more symbols
        /watch rm SYM [SYM ...]    — remove
        """
        args = (context.args or [])
        if not args:
            items = await self._ctx.db.get_user_watchlist()
            if not items:
                await update.message.reply_text("Watchlist empty. /watch add SYM to add.")
                return
            syms = ", ".join(i["symbol"] for i in items)
            await update.message.reply_html(
                f"<b>Watchlist</b> ({len(items)})\n{syms}",
            )
            return
        action = args[0].lower()
        symbols = [s.strip().upper() for s in args[1:] if s.strip()]
        if action not in ("add", "rm", "remove") or not symbols:
            await update.message.reply_text(
                "Usage:\n/watch — list\n/watch add SYM\n/watch rm SYM",
            )
            return
        results: list[str] = []
        for sym in symbols:
            try:
                if action == "add":
                    await self._ctx.db.add_user_watchlist_symbol(sym, None, None)
                    results.append(f"+ {sym}")
                else:
                    ok = await self._ctx.db.remove_user_watchlist_symbol(sym)
                    results.append(f"- {sym}" if ok else f"  {sym} (not in watchlist)")
            except Exception as e:
                results.append(f"  {sym} failed: {e}")
        await update.message.reply_text("\n".join(results))

    async def _cmd_quarantine(self, update: Any, context: Any) -> None:
        """/quarantine                       — list quarantined
        /quarantine unblock SYM              — clear quarantine for SYM
        /quarantine replace SYM REPLACEMENT  — route SYM to REPLACEMENT
        /quarantine replace SYM clear        — clear replacement mapping
        """
        args = (context.args or [])
        if not args:
            qs = await self._ctx.db.get_quarantined_symbols()
            if not qs:
                await update.message.reply_text("No quarantined symbols.")
                return
            lines = [f"<b>Quarantined</b> ({len(qs)})"]
            for q in qs[:20]:  # avoid massive messages
                sym = q.get("symbol")
                fails = q.get("consecutive_failures")
                repl = q.get("replacement_symbol")
                tail = f" → {repl}" if repl else ""
                lines.append(f"  {sym} ({fails} fails){tail}")
            if len(qs) > 20:
                lines.append(f"  … {len(qs) - 20} more")
            await update.message.reply_html("\n".join(lines))
            return
        action = args[0].lower()
        if action == "unblock" and len(args) >= 2:
            sym = args[1].upper()
            ok = await self._ctx.db.unquarantine_symbol(sym)
            await update.message.reply_text(
                f"Unblocked {sym}" if ok else f"{sym} not quarantined",
            )
            return
        if action == "replace" and len(args) >= 3:
            sym = args[1].upper()
            repl_raw = args[2].strip()
            replacement: str | None = (
                None if repl_raw.lower() == "clear" else repl_raw.upper()
            )
            await self._ctx.db.set_replacement_symbol(sym, replacement)
            await update.message.reply_text(
                f"{sym} → {replacement}" if replacement else f"{sym}: replacement cleared",
            )
            return
        await update.message.reply_text(
            "Usage:\n/quarantine — list\n"
            "/quarantine unblock SYM\n"
            "/quarantine replace SYM REPLACEMENT\n"
            "/quarantine replace SYM clear",
        )

    async def _cmd_lock(self, update: Any, context: Any) -> None:
        """/lock SYM [SYM ...] [-- notes]
        Locked symbols are never auto-sold or auto-adopted.
        """
        args = (context.args or [])
        if not args:
            locks = await self._ctx.db.get_locked_holdings()
            if not locks:
                await update.message.reply_text("No locked holdings. /lock SYM to add.")
                return
            lines = [f"<b>Locked</b> ({len(locks)})"]
            for l in locks[:25]:
                note = l.get("notes")
                lines.append(f"  {l['symbol']}" + (f" — {note}" if note else ""))
            await update.message.reply_html("\n".join(lines))
            return
        symbols = [s.strip().upper() for s in args if s.strip() and not s.startswith("--")]
        if not symbols:
            await update.message.reply_text("Usage: /lock SYM [SYM ...]")
            return
        results: list[str] = []
        for sym in symbols:
            try:
                await self._ctx.db.lock_symbol(sym, None)
                results.append(f"locked {sym}")
            except Exception as e:
                results.append(f"{sym} failed: {e}")
        await update.message.reply_text("\n".join(results))

    async def _cmd_unlock(self, update: Any, context: Any) -> None:
        """/unlock SYM [SYM ...]  — remove lock(s) so YoloVest can manage these again."""
        symbols = [s.strip().upper() for s in (context.args or []) if s.strip()]
        if not symbols:
            await update.message.reply_text("Usage: /unlock SYM [SYM ...]")
            return
        results: list[str] = []
        for sym in symbols:
            try:
                ok = await self._ctx.db.unlock_symbol(sym)
                results.append(f"unlocked {sym}" if ok else f"{sym} not locked")
            except Exception as e:
                results.append(f"{sym} failed: {e}")
        await update.message.reply_text("\n".join(results))

    async def _cmd_mode(self, update: Any, context: Any) -> None:
        """/mode             — show current transaction_mode
        /mode auto           — switch to auto-execute
        /mode manual         — switch to require-approval
        """
        args = (context.args or [])
        cur = self._ctx.config.execution.transaction_mode
        if not args:
            await update.message.reply_html(
                f"<b>Transaction mode:</b> {cur}\n"
                f"Use /mode auto or /mode manual to switch.",
            )
            return
        new_mode = args[0].lower()
        if new_mode not in ("auto", "manual"):
            await update.message.reply_text("Usage: /mode auto|manual")
            return
        if new_mode == cur:
            await update.message.reply_text(f"Already in {cur} mode.")
            return
        # Persist + hot-apply, same shape as the dashboard PUT /api/config path.
        try:
            from yolovest.config import apply_db_config
            db_values = await self._ctx.db.get_all_config()
            db_values["execution.transaction_mode"] = new_mode
            new_config = apply_db_config(self._ctx.config, db_values)
            await self._ctx.db.set_config("execution.transaction_mode", new_mode)
            self._ctx.config = new_config
            if hasattr(self._ctx.notify, "_config"):
                self._ctx.notify._config = self._ctx.config
        except Exception as e:
            await update.message.reply_text(f"Failed to switch mode: {e}")
            return
        await update.message.reply_html(
            f"Transaction mode: <b>{cur}</b> → <b>{new_mode}</b>",
        )

    async def _cmd_symbol(self, update: Any, context: Any) -> None:
        """/symbol SYM — price, recent trades, last attribution, recent bulk deals."""
        args = (context.args or [])
        if not args:
            await update.message.reply_text("Usage: /symbol SYM")
            return
        sym = args[0].upper()
        import json as _json
        # Latest daily close + change vs previous close
        try:
            bars = await self._ctx.db.get_ohlcv(sym, "daily", days=5)
        except Exception:
            bars = []
        last_close = bars[-1].close if bars else None
        prev_close = bars[-2].close if len(bars) >= 2 else None
        change_pct = (
            ((last_close - prev_close) / prev_close * 100)
            if last_close is not None and prev_close
            else None
        )
        # Recent trades on this symbol (mode-scoped)
        try:
            trades = await self._ctx.db.get_symbol_trades(
                sym, limit=5, mode=self._ctx.config.mode,
            )
        except Exception:
            trades = []
        # Latest signal's attribution
        try:
            cursor = await self._ctx.db.read_conn.execute(
                "SELECT signal_type, confidence_score, attribution_json, created_at "
                "FROM signals WHERE symbol = ? AND mode = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (sym, self._ctx.config.mode),
            )
            sig_row = await cursor.fetchone()
        except Exception:
            sig_row = None
        # Bulk deals + delivery
        try:
            bulk = await self._ctx.db.get_bulk_deals_list(
                days=30, symbol=sym, limit=5,
            )
        except Exception:
            bulk = []
        try:
            delivery_avg = await self._ctx.db.get_recent_delivery_pct(sym, lookback_days=5)
        except Exception:
            delivery_avg = None
        # Quarantine status
        try:
            qs = await self._ctx.db.get_quarantined_symbols()
            q_entry = next((q for q in qs if q.get("symbol", "").upper() == sym), None)
        except Exception:
            q_entry = None

        lines: list[str] = [f"<b>{sym}</b>"]
        if last_close is not None:
            cls_part = f"₹{_fmt_inr(last_close)}"
            if change_pct is not None:
                sign = "+" if change_pct >= 0 else ""
                cls_part += f" ({sign}{change_pct:.2f}%)"
            lines.append(cls_part)
        if q_entry:
            repl = q_entry.get("replacement_symbol")
            lines.append(
                f"⚠ QUARANTINED ({q_entry.get('consecutive_failures')} fails)"
                + (f" → routed to {repl}" if repl else ""),
            )
        if delivery_avg is not None:
            lines.append(f"Delivery (5d avg): {delivery_avg:.1f}%")

        if sig_row:
            lines.append(
                f"\n<b>Latest signal</b>: {sig_row[0]} "
                f"@ {(sig_row[1] or 0) * 100:.0f}% conf  ({sig_row[3][:16]})",
            )
            attr_raw = sig_row[2]
            if attr_raw:
                try:
                    attr = _json.loads(attr_raw)
                    for a in (attr or [])[:5]:
                        c = float(a.get("contribution") or 0)
                        arrow = "↑" if c >= 0 else "↓"
                        lines.append(
                            f"  {arrow} {a.get('feature')} ({c:+.3f})",
                        )
                except Exception:
                    pass

        if trades:
            lines.append("\n<b>Recent trades</b>")
            for t in trades:
                pnl = t.get("pnl")
                pnl_part = (
                    f"₹{_fmt_inr(pnl)}" if pnl is not None else "open"
                )
                lines.append(
                    f"  {t.get('signal_type', '?')} {t.get('quantity', '?')}"
                    f" @ ₹{_fmt_inr(t.get('fill_price') or 0)} → {pnl_part}",
                )

        if bulk:
            lines.append("\n<b>Recent bulk deals</b>")
            for d in bulk[:5]:
                bs = d.get("buy_sell") or "?"
                qty = d.get("quantity")
                client = (d.get("client_name") or "")[:32]
                lines.append(
                    f"  {d.get('deal_date')} {bs} {qty:,} — {client}",
                )

        await update.message.reply_html("\n".join(lines))

    async def _cmd_rotation(self, update: Any, context: Any) -> None:
        """/rotation                — show count of symbols in rotation cooldown
        /rotation clear              — clear cooldown for all symbols
        /rotation clear SYM [SYM]    — clear cooldown for specific symbols

        Rotation cooldown sometimes accumulates faster than intended
        (especially after a universe expansion or with aggressive
        thresholds) and can silently bench most of the universe. This
        is the one-shot reset.
        """
        args = (context.args or [])
        cfg = self._ctx.config.scanning
        if not args:
            try:
                in_cooldown = await self._ctx.db.get_rotation_cooldown_symbols()
            except Exception as e:
                await update.message.reply_text(f"Failed: {e}")
                return
            await update.message.reply_html(
                f"<b>Rotation cooldown</b> "
                f"({'enabled' if cfg.rotation_enabled else 'disabled'})\n"
                f"In cooldown: {len(in_cooldown)} symbols\n"
                f"Threshold: {cfg.rotation_no_signal_threshold} consecutive "
                f"no-signal heartbeats\n"
                f"Cooldown: {cfg.rotation_cooldown_hours}h\n\n"
                "Use /rotation clear to reset all, or "
                "/rotation clear SYM [SYM ...] to reset specific symbols.",
            )
            return
        if args[0].lower() != "clear":
            await update.message.reply_text(
                "Usage: /rotation | /rotation clear | /rotation clear SYM",
            )
            return
        symbols = [s.strip().upper() for s in args[1:] if s.strip()]
        try:
            if symbols:
                total = 0
                for sym in symbols:
                    total += await self._ctx.db.clear_rotation_cooldown(sym)
                await update.message.reply_text(
                    f"Cleared rotation cooldown for {total}/{len(symbols)} symbols",
                )
            else:
                n = await self._ctx.db.clear_rotation_cooldown()
                await update.message.reply_text(
                    f"Cleared rotation cooldown for {n} symbols",
                )
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

