"""Skill: position-monitor — Monitor open positions, trail SLs, reconcile.

Trigger: HEARTBEAT during market hours
Pipeline position: Runs continuously alongside generate-signals.

Flow:
1. Fetch current positions from broker
2. Reconcile broker state with local DB state — flag discrepancies
3. For each open position:
   a. Check if target hit → emit exit signal
   b. Check if SL hit → record loss
   c. Check trailing SL logic:
      - If profit >= trailing_sl_trigger_multiple x risk -> move SL to breakeven
      - Continue trailing SL upward in trailing_sl_step_pct increments
   d. Modify SL order on broker if trail triggered
4. Track unrealized PnL for portfolio state
5. Update slippage records
6. Alert on any discrepancies between local and broker state
"""

import asyncio
import logging
from typing import Any

from yolovest.costs import resolve_round_trip_costs
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class PositionMonitorSkill(SkillBase):
    name = "position-monitor"
    description = "Reconcile positions, trail stop-losses, track PnL"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        return bool(self.ctx.market_hours.is_market_hours())

    async def execute(self, **kwargs: Any) -> SkillResult:
        cfg = self.ctx.config.risk
        # Scope to the current mode so paper rows never run through
        # live-broker code paths (and vice versa). Without this filter
        # _check_partial_profit_booking would happily call
        # broker.place_order against a paper trade after a mode toggle.
        local_positions = await self.ctx.db.get_open_positions(
            mode=self.ctx.config.mode,
        )
        broker_positions = await self.ctx.broker.get_positions()

        await self._subscribe_ticker_symbols(local_positions, broker_positions)

        # Reconcile gtt_id / gtt_status against the broker's GTT list.
        # Cleared GTTs (user-cancelled on Kite web, rejected at trigger
        # time, expired, etc.) get their gtt_id wiped from the local row
        # so the downstream loop falls back to client-side detection
        # rather than assuming the broker is still protecting the
        # position. Mutates local_positions in place.
        await self._reconcile_gtts(local_positions)

        # Heal orphan broker-side exits. Positions whose DB row has no
        # gtt_id / target_order_id / sl_order_id but whose symbol has an
        # active GTT or open SL/LIMIT exit at the broker would otherwise
        # cascade through client-side detection and double-place an
        # exit, leaving the broker's resting order to fire afterwards
        # for a duplicate transaction. Pull the broker state once, then
        # either heal the DB row when the orphan can be safely matched
        # back, or flag the position as "broker-partial-protected" so
        # client-side stays quiet until the user investigates.
        await self._reconcile_orphan_broker_exits(local_positions)

        discrepancies = self._reconcile(local_positions, broker_positions)

        # Recover ghost positions: local DB says open, broker says closed.
        # This happens when broker-side SL triggers or manual broker actions.
        recovered = await self._recover_ghost_positions(
            local_positions, broker_positions,
        )

        if discrepancies:
            await self.ctx.notify.send(
                "Position discrepancy detected:\n"
                + "\n".join(discrepancies)
                + (f"\nAuto-recovered: {', '.join(recovered)}" if recovered else ""),
                alert_type="errors",
            )

        trails_modified = 0
        targets_hit: list[dict[str, Any]] = []
        stops_hit: list[dict[str, Any]] = []
        expiry_actions: list[dict[str, Any]] = []
        ltp_failures: list[str] = []

        # Skip positions that were just recovered (already closed in DB)
        recovered_set = set(recovered)

        # Load locked symbols — these should not be auto-sold (target/SL/trail)
        locked_symbols = await self.ctx.db.get_locked_symbols()


        for pos in local_positions:
            trails_modified += await self._monitor_position(
                pos, cfg,
                recovered_set=recovered_set,
                locked_symbols=locked_symbols,
                targets_hit=targets_hit,
                stops_hit=stops_hit,
                expiry_actions=expiry_actions,
                ltp_failures=ltp_failures,
            )

        await self._broadcast_cycle_results(
            local_positions, trails_modified,
            targets_hit=targets_hit, stops_hit=stops_hit,
            expiry_actions=expiry_actions, ltp_failures=ltp_failures,
        )

        target_syms = [h["symbol"] for h in targets_hit]
        stop_syms = [h["symbol"] for h in stops_hit]
        expiry_syms = [ea["symbol"] for ea in expiry_actions]
        logger.info(
            "position-monitor: %d positions — targets_hit=%s, stops_hit=%s, "
            "expiry_actions=%s, trails_modified=%d, discrepancies=%d, "
            "ltp_failures=%d, recovered=%d",
            len(local_positions), target_syms or "none", stop_syms or "none",
            expiry_syms or "none", trails_modified,
            len(discrepancies) if discrepancies else 0,
            len(ltp_failures), len(recovered),
        )

        return SkillResult(
            success=len(ltp_failures) == 0,
            skill_name=self.name,
            error=f"LTP fetch failed for: {', '.join(ltp_failures)}" if ltp_failures else None,
            data={
                "positions_monitored": len(local_positions),
                "trails_modified": trails_modified,
                "targets_hit": target_syms,
                "stops_hit": stop_syms,
                "expiry_actions": expiry_syms,
                "discrepancies": len(discrepancies) if discrepancies else 0,
                "ltp_failures": ltp_failures,
                "ghost_recovered": recovered,
            },
        )

    async def _subscribe_ticker_symbols(
        self,
        local_positions: list[dict[str, Any]],
        broker_positions: list[dict[str, Any]],
    ) -> None:
        """Subscribe the KiteTicker to every symbol the dashboard renders
        live: open positions + holdings + watchlists + today's signals +
        pending trades. Idempotent — already-subscribed tokens are skipped
        inside the ticker, so re-running every heartbeat is cheap."""
        ticker = getattr(self.ctx, "ticker", None)
        if ticker is not None:
            symbols_to_subscribe: set[str] = set()
            symbols_to_subscribe.update(p["symbol"] for p in local_positions)
            for bp in broker_positions:
                sym = bp.get("tradingsymbol") or bp.get("symbol")
                if sym:
                    symbols_to_subscribe.add(sym)
            try:
                holdings = await self.ctx.broker.get_holdings()
                for h in holdings or []:
                    sym = h.get("tradingsymbol") or h.get("symbol")
                    if sym:
                        symbols_to_subscribe.add(sym)
            except Exception:
                logger.debug("ticker subscribe: get_holdings failed", exc_info=True)
            try:
                wl = await self.ctx.db.get_watchlist()
                for w in (wl or [])[:50]:
                    if w.get("symbol"):
                        symbols_to_subscribe.add(w["symbol"])
            except Exception:
                logger.debug("ticker subscribe: get_watchlist failed", exc_info=True)
            try:
                uw = await self.ctx.db.get_user_watchlist()
                for u in uw or []:
                    if u.get("symbol"):
                        symbols_to_subscribe.add(u["symbol"])
            except Exception:
                logger.debug("ticker subscribe: get_user_watchlist failed", exc_info=True)
            # Today's signals + pending-trade symbols. Catches the case
            # where generate-signals was triggered manually (outside the
            # heartbeat) and produced symbols that aren't in the watchlist
            # — without this the RecommendationsPanel + PendingTradesBanner
            # would render a blank LTP column until the next heartbeat.
            try:
                for r in await self.ctx.db.get_todays_recommendations():
                    sym = r.get("symbol")
                    if sym:
                        symbols_to_subscribe.add(sym)
            except Exception:
                logger.debug("ticker subscribe: today's signals failed", exc_info=True)
            try:
                for p in await self.ctx.db.get_pending_trades():
                    sym = p.get("symbol")
                    if sym:
                        symbols_to_subscribe.add(sym)
            except Exception:
                logger.debug("ticker subscribe: pending trades failed", exc_info=True)
            if symbols_to_subscribe:
                try:
                    await ticker.subscribe(sorted(symbols_to_subscribe))
                except Exception:
                    logger.debug("ticker subscribe failed", exc_info=True)

    async def _monitor_position(
        self,
        pos: dict[str, Any],
        cfg: Any,
        *,
        recovered_set: set[str],
        locked_symbols: set[str],
        targets_hit: list[dict[str, Any]],
        stops_hit: list[dict[str, Any]],
        expiry_actions: list[dict[str, Any]],
        ltp_failures: list[str],
    ) -> int:
        """Run one heartbeat's checks for a single open position, in the
        original order: LTP fetch -> locked skip -> partial booking ->
        broker-managed paths (GTT / MIS OCO / partial-protected) ->
        auxiliary exits -> target -> SL -> trailing -> holding expiry ->
        unrealized PnL. Appends outcomes to the shared accumulators and
        returns the number of trailing-SL modifications (0 or 1)."""
        from yolovest.timezone import now_ist

        trails = 0
        symbol = pos["symbol"]
        if symbol in recovered_set:
            return trails

        # Fetch LTP with retry (positions must not go unmonitored)
        current_price = await self._get_ltp_with_retry(symbol)
        if current_price is None:
            ltp_failures.append(symbol)
            logger.error(
                "position-monitor: LTP fetch failed for %s after retries — "
                "position UNMONITORED this cycle",
                symbol,
            )
            return trails

        # PnL math uses the actual broker fill price, not the signal's
        # entry_price — otherwise recorded slippage gets silently erased.
        entry = float(pos.get("fill_price") or pos["entry_price"])
        sl = pos["stop_loss_price"]
        target = pos["target_price"]
        risk_per_share = abs(entry - sl)

        # Locked holdings: track PnL but never auto-close
        if symbol in locked_symbols:
            await self.ctx.db.update_unrealized_pnl(
                pos["trade_id"], current_price,
            )
            return trails

        # Partial profit booking (before target/SL checks)
        partial_booked = await self._check_partial_profit_booking(
            pos, current_price,
        )
        if partial_booked:
            # Update unrealized PnL for remaining position and move on;
            # skip target/SL checks this cycle to let the partial order settle
            await self.ctx.db.update_unrealized_pnl(
                pos["trade_id"], current_price,
            )
            return trails

        # If a broker-side GTT is attached, exit enforcement is at the
        # broker. Skip client-side target/SL detection so we don't
        # double-place an exit order. We still trail the SL by
        # modifying the GTT itself when the trailing condition fires,
        # so winning positions ratchet up their breakeven floor.
        # The ghost-position reconciler catches the case where the
        # GTT fires and the broker position vanishes.
        if pos.get("gtt_id"):
            if cfg.trailing_sl_enabled and risk_per_share > 0:
                await self._maybe_trail_gtt_sl(
                    pos, entry, sl, current_price, risk_per_share,
                )
            await self.ctx.db.update_unrealized_pnl(
                pos["trade_id"], current_price,
            )
            return trails

        # For MIS trades with broker-side OCO orders (resting target
        # LIMIT + SL), broker is in charge of the exit. We just keep
        # OCO honest — cancel the surviving leg when one fills — and
        # skip client-side target/SL detection. Client-side only fires
        # for trades where the broker LIMIT never got placed (e.g.
        # historical rows, or LIMIT placement failed at entry time).
        #
        # Trailing still applies — we lift the broker-side SL order
        # in place via modify_sl_order so the position locks in
        # gains as price moves toward target.
        if pos.get("target_order_id") and pos.get("sl_order_id"):
            await self._enforce_mis_oco(pos)
            if cfg.trailing_sl_enabled and risk_per_share > 0:
                await self._maybe_trail_mis_sl(
                    pos, entry, sl, current_price, risk_per_share, target,
                )
            await self.ctx.db.update_unrealized_pnl(
                pos["trade_id"], current_price,
            )
            return trails

        # Broker has a resting exit we couldn't safely pair up
        # (e.g. an MIS SL exists but the matching target LIMIT was
        # never placed, or the row is missing both order_ids).
        # _reconcile_orphan_broker_exits set this transient marker
        # — defer to the broker so a client-side exit can't
        # double-place, but keep the warning visible in the audit
        # log so the user notices.
        if pos.get("_broker_partial_protected"):
            await self.ctx.db.update_unrealized_pnl(
                pos["trade_id"], current_price,
            )
            return trails

        # Auxiliary exits — time-stop / volume-exhaustion. Only fire
        # for client-side managed positions (no broker GTT, no MIS
        # OCO pair). Broker-managed exits keep their own lifecycle;
        # extending these conditions there would need cancel + market
        # exit and is left for later.
        aux_exit = await self._check_auxiliary_exits(
            pos, current_price, entry, target,
        )
        if aux_exit:
            qty = pos.get("quantity", 0)
            product = pos.get("product", "MIS")
            if pos["signal_type"] == "BUY":
                gross_pnl = (current_price - entry) * qty
            else:
                gross_pnl = (entry - current_price) * qty
            costs, _src, breakdown = await resolve_round_trip_costs(
                self.ctx.broker, symbol=symbol, signal_type=pos["signal_type"],
                entry_price=entry, exit_price=current_price, quantity=qty,
                product=product, cost_config=self.ctx.config.transaction_costs,
            )
            pnl = round(gross_pnl - costs, 2)
            if self.ctx.config.execution.transaction_mode == "manual":
                await self._queue_exit_for_approval(
                    pos, current_price, pnl, aux_exit,
                )
            else:
                await self.ctx.db.close_position(
                    pos["trade_id"], current_price, pnl,
                    realized_costs=breakdown,
                )
            expiry_actions.append({
                "action": "closed", "symbol": symbol, "reason": aux_exit,
                "days_held": 0, "expected_days": 0, "pnl": pnl,
            })
            logger.info(
                "position-monitor: AUX EXIT %s [%s] — exit=%.2f pnl=₹%.2f",
                symbol, aux_exit, current_price, pnl,
            )
            return trails

        # Target hit (with early-exit buffer). Heartbeats are 15 min
        # apart; a price that's within `target_early_exit_pct` of target
        # but never quite touches it would otherwise wait a full cycle
        # and risk reversing.
        buf = self.ctx.config.risk.target_early_exit_pct
        buy_trigger = target * (1 - buf)
        sell_trigger = target * (1 + buf)
        if (pos["signal_type"] == "BUY" and current_price >= buy_trigger) or (
            pos["signal_type"] == "SELL" and current_price <= sell_trigger
        ):
            qty = pos.get("quantity", 0)
            if pos["signal_type"] == "BUY":
                gross_pnl = (current_price - entry) * qty
            else:
                gross_pnl = (entry - current_price) * qty
            product = pos.get("product", "MIS")
            costs, src, breakdown = await resolve_round_trip_costs(
                self.ctx.broker, symbol=symbol, signal_type=pos["signal_type"],
                entry_price=entry, exit_price=current_price, quantity=qty,
                product=product, cost_config=self.ctx.config.transaction_costs,
            )
            pnl = round(gross_pnl - costs, 2)

            if self.ctx.config.execution.transaction_mode == "manual":
                await self._queue_exit_for_approval(
                    pos, current_price, pnl, "target_hit",
                )
            else:
                await self.ctx.db.close_position(
                    pos["trade_id"], current_price, pnl, realized_costs=breakdown,
                )
            targets_hit.append({"symbol": symbol, "pnl": pnl})
            logger.info(
                "position-monitor: TARGET HIT %s — exit=%.2f pnl=₹%.2f (costs=₹%.2f src=%s)",
                symbol, current_price, pnl, costs, src,
            )
            return trails

        # SL hit?
        if (pos["signal_type"] == "BUY" and current_price <= sl) or (
            pos["signal_type"] == "SELL" and current_price >= sl
        ):
            qty = pos.get("quantity", 0)
            if pos["signal_type"] == "BUY":
                gross_pnl = (current_price - entry) * qty
            else:
                gross_pnl = (entry - current_price) * qty
            product = pos.get("product", "MIS")
            costs, src, breakdown = await resolve_round_trip_costs(
                self.ctx.broker, symbol=symbol, signal_type=pos["signal_type"],
                entry_price=entry, exit_price=current_price, quantity=qty,
                product=product, cost_config=self.ctx.config.transaction_costs,
            )
            pnl = round(gross_pnl - costs, 2)

            if self.ctx.config.execution.transaction_mode == "manual":
                await self._queue_exit_for_approval(
                    pos, current_price, pnl, "stop_loss_hit",
                )
            else:
                await self.ctx.db.close_position(
                    pos["trade_id"], current_price, pnl, realized_costs=breakdown,
                )
            stops_hit.append({"symbol": symbol, "pnl": pnl})
            logger.info(
                "position-monitor: STOP LOSS HIT %s — exit=%.2f pnl=₹%.2f (costs=₹%.2f src=%s)",
                symbol, current_price, pnl, costs, src,
            )
            return trails

        # Trailing SL — requires a broker-side SL order to modify.
        # Positions without sl_order_id (adopted, old rows where
        # the SL placement failed, paper-mode shortcuts) skip
        # trailing entirely; their SL is conceptual and updated
        # via update_position_sl only when the client-side
        # detection path closes the trade.
        if (
            cfg.trailing_sl_enabled
            and risk_per_share > 0
            and pos.get("sl_order_id")
        ):
            if pos["signal_type"] == "BUY":
                profit = current_price - entry
            else:
                profit = entry - current_price
            # Threshold is per-bucket target-progress %; resolver
            # converts back to a rupees-of-profit value the
            # comparison can use directly.
            target_distance = abs(target - entry) if target > 0 else 0.0
            trigger_profit = cfg.resolve_trailing_trigger(
                holding_period=pos.get("expected_holding_period", "")
                or pos.get("holding_period", ""),
                risk_per_share=risk_per_share,
                target_distance=target_distance,
            )

            if profit >= trigger_profit:
                # Calculate new trailing SL. Tighten the step when
                # we're already close to target so a final pullback
                # can't surrender the gain.
                step_pct = cfg.trailing_sl_step_pct
                tweaks = cfg.exit_tweaks
                if tweaks.tighten_trailing_enabled:
                    target_progress = self._target_progress_pct(
                        pos["signal_type"], entry, target, current_price,
                    )
                    step_pct *= self._trailing_step_multiplier(
                        target_progress, tweaks,
                    )
                step = current_price * step_pct
                if pos["signal_type"] == "BUY":
                    new_sl = max(entry, current_price - step)  # at least breakeven
                else:
                    new_sl = min(entry, current_price + step)

                if self._is_better_sl(pos["signal_type"], new_sl, sl):
                    await self.ctx.broker.modify_sl_order(pos["sl_order_id"], new_sl)
                    await self.ctx.db.update_position_sl(pos["trade_id"], new_sl)
                    trails += 1

        # Holding period expiry check
        expiry_result = await self._check_holding_expiry(
            pos, current_price, entry, now_ist(),
        )
        if expiry_result:
            expiry_actions.append(expiry_result)
            if expiry_result["action"] == "closed":
                return trails  # position already closed, skip PnL update

        # Update unrealized PnL
        await self.ctx.db.update_unrealized_pnl(pos["trade_id"], current_price)

        return trails

    async def _broadcast_cycle_results(
        self,
        local_positions: list[dict[str, Any]],
        trails_modified: int,
        *,
        targets_hit: list[dict[str, Any]],
        stops_hit: list[dict[str, Any]],
        expiry_actions: list[dict[str, Any]],
        ltp_failures: list[str],
    ) -> None:
        """Telegram + WebSocket fan-out for the cycle's exits and the
        unmonitored-position warning."""
        # Notify holding period expiry actions
        for ea in expiry_actions:
            if ea["action"] == "closed":
                await self.broadcast("trade_exit", {
                    "symbol": ea["symbol"], "reason": "holding_expiry",
                })
                await self.ctx.notify.send_exit_alert(
                    ea["symbol"],
                    f"Holding period expired ({ea['days_held']}d/{ea['expected_days']}d) — {ea['reason']}",
                    ea.get("pnl", 0),
                )
            elif ea["action"] == "tightened":
                await self.ctx.notify.send(
                    f"Holding period expired for {ea['symbol']} "
                    f"({ea['days_held']}d/{ea['expected_days']}d): "
                    f"in profit — SL tightened to {ea['new_sl']:.2f}",
                    alert_type="info",
                )

        # Broadcast and notify target/stop hits
        for hit in targets_hit:
            await self.broadcast("trade_exit", {
                "symbol": hit["symbol"], "reason": "target_hit",
            })
            await self.ctx.notify.send_exit_alert(
                hit["symbol"], "Target hit", hit["pnl"],
            )
        for hit in stops_hit:
            await self.broadcast("trade_exit", {
                "symbol": hit["symbol"], "reason": "stop_loss_hit",
            })
            await self.ctx.notify.send_exit_alert(
                hit["symbol"], "Stop loss hit", hit["pnl"],
            )

        # Broadcast portfolio PnL summary
        if local_positions:
            await self.broadcast("portfolio_pnl", {
                "positions": len(local_positions),
                "targets_hit": len(targets_hit),
                "stops_hit": len(stops_hit),
                "trails_modified": trails_modified,
            })

        # Alert on LTP fetch failures (positions were unmonitored)
        if ltp_failures:
            await self.ctx.notify.send(
                f"WARNING: LTP fetch failed for {len(ltp_failures)} positions "
                f"({', '.join(ltp_failures)}). These positions were NOT monitored "
                f"this cycle — SL/target checks skipped.",
                alert_type="errors",
            )


    async def _get_ltp_with_retry(
        self, symbol: str, max_retries: int = 3, base_delay: float = 1.0,
    ) -> float | None:
        """Fetch LTP with exponential backoff retries.

        Prefers the KiteTicker cached price (sub-second, fresh within
        5s) when available — that's the killer-feature payoff of the
        WebSocket integration. Falls back to the REST market_data path
        if the ticker isn't running, the cache is stale, or the symbol
        was never subscribed.
        """
        ticker = getattr(self.ctx, "ticker", None)
        if ticker is not None:
            cached = ticker.get_ltp(symbol)
            if cached and cached > 0:
                return cached

        for attempt in range(max_retries):
            try:
                price = await self.ctx.market_data.get_ltp(symbol)
                if price is not None and price > 0:
                    return price
                logger.warning(
                    "LTP for %s returned invalid value: %s (attempt %d/%d)",
                    symbol, price, attempt + 1, max_retries,
                )
            except Exception as e:
                logger.warning(
                    "LTP fetch failed for %s: %s (attempt %d/%d)",
                    symbol, e, attempt + 1, max_retries,
                )
            if attempt < max_retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        return None

    def _reconcile(self, local: list[dict[str, Any]], broker: list[dict[str, Any]]) -> list[str]:
        """Compare local DB positions with broker positions."""
        discrepancies = []

        # Build lookup by symbol for broker positions. Filter out the
        # noise rows Kite's positions endpoint includes that don't
        # represent real open exposure:
        #   - CNC with qty < 0 = a delivery sale that just happened
        #     today; the user sold N shares from holdings and Kite
        #     surfaces the sell-side as a position row. Indian retail
        #     can't short CNC so this is never a tradeable short.
        #     Skipping it stops the "ONGC: on broker (qty=-5) but
        #     not in local DB" alert from firing on every heartbeat
        #     after a manual holdings sale.
        #   - Round-tripped intraday positions where buy and sell
        #     quantities net to zero AND no overnight balance: those
        #     are closed for the day, nothing to manage.
        broker_by_symbol: dict[str, dict[str, Any]] = {}
        for bp in broker:
            sym = bp.get("tradingsymbol") or bp.get("symbol", "")
            qty = bp.get("quantity", bp.get("net_quantity", 0)) or 0
            product = (bp.get("product") or "").upper()
            if product == "CNC" and qty < 0:
                logger.debug(
                    "reconcile: skipping CNC sell artefact %s qty=%d "
                    "(delivery sale, not a real position)", sym, qty,
                )
                continue
            buy_q = int(bp.get("buy_quantity") or 0)
            sell_q = int(bp.get("sell_quantity") or 0)
            overnight = int(bp.get("overnight_quantity") or 0)
            if qty == 0 and buy_q > 0 and buy_q == sell_q and overnight == 0:
                logger.debug(
                    "reconcile: skipping round-tripped intraday %s "
                    "(buy=%d sell=%d net=0)", sym, buy_q, sell_q,
                )
                continue
            broker_by_symbol[sym] = bp

        # Check each local position against broker
        local_symbols = set()
        for pos in local:
            symbol = pos.get("symbol", "")
            local_symbols.add(symbol)

            if symbol not in broker_by_symbol:
                # Paper mode positions won't be on broker
                if pos.get("mode") != "paper":
                    discrepancies.append(
                        f"{symbol}: in local DB but not on broker"
                    )
                continue

            bp = broker_by_symbol[symbol]
            broker_qty = bp.get("quantity", bp.get("net_quantity", 0))
            local_qty = pos.get("quantity", 0)
            # Kite's positions API returns net_quantity SIGNED — short
            # positions come back negative. We store quantity as a
            # positive int + a separate signal_type ("BUY" or "SELL").
            # Translate to the broker's sign convention before
            # comparing so a 440-share SELL doesn't spuriously look
            # like a mismatch with broker_qty=-440.
            direction = -1 if pos.get("signal_type", "BUY") == "SELL" else 1
            local_qty_signed = int(local_qty) * direction
            if broker_qty != local_qty_signed:
                discrepancies.append(
                    f"{symbol}: qty mismatch (local={local_qty_signed}, broker={broker_qty})"
                )

        # Check for broker positions not in local DB
        for sym, bp in broker_by_symbol.items():
            qty = bp.get("quantity", bp.get("net_quantity", 0))
            if sym not in local_symbols and qty != 0:
                discrepancies.append(
                    f"{sym}: on broker (qty={qty}) but not in local DB"
                )

        return discrepancies

    async def _recover_ghost_positions(
        self,
        local_positions: list[dict[str, Any]],
        broker_positions: list[dict[str, Any]],
    ) -> list[str]:
        """Auto-close local positions that no longer exist on the broker.

        When the broker closes a position (e.g. SL-M triggered server-side,
        manual exit via Kite web), the local DB still shows it as open.
        This method detects those "ghost" positions and closes them using
        the best available exit price.

        Returns list of symbols that were recovered.
        """
        if self.ctx.config.mode == "paper":
            return []

        broker_by_symbol: dict[str, dict[str, Any]] = {}
        for bp in broker_positions:
            sym = bp.get("tradingsymbol") or bp.get("symbol", "")
            qty = bp.get("quantity", bp.get("net_quantity", 0))
            broker_by_symbol[sym] = {"qty": qty, "data": bp}

        recovered: list[str] = []

        # Pull today's broker trades once so each ghost can recover its actual
        # exit fill instead of falling back to LTP (which drifts after the
        # close fires server-side or the user exits manually on Kite web).
        try:
            broker_trades = await self.ctx.broker.get_executed_trades()
        except Exception as e:
            logger.debug("get_executed_trades failed: %s", e)
            broker_trades = []

        for pos in local_positions:
            if pos.get("mode") == "paper":
                continue
            symbol = pos["symbol"]
            broker_info = broker_by_symbol.get(symbol)

            # Position gone from broker entirely, or broker shows qty=0
            is_ghost = (
                broker_info is None
                or broker_info["qty"] == 0
            )
            if not is_ghost:
                continue

            # Resolve exit price in priority order:
            #   1. average price of the closing fills from kite.trades()
            #   2. live LTP (drifts but better than entry)
            #   3. recorded stop-loss price (last-resort, when broker offline)
            exit_side = "SELL" if pos["signal_type"] == "BUY" else "BUY"
            exit_price, exit_source = self._find_closing_fill_price(
                broker_trades, symbol, exit_side, pos.get("quantity", 0),
            )
            if exit_price is None:
                exit_price = await self._get_ltp_with_retry(symbol)
                exit_source = "ltp"
            if exit_price is None:
                exit_price = pos["stop_loss_price"]
                exit_source = "sl_price"
                logger.warning(
                    "Ghost position %s: broker trades + LTP unavailable, "
                    "using SL price %.2f as exit estimate",
                    symbol, exit_price,
                )

            entry = float(pos.get("fill_price") or pos["entry_price"])
            qty = pos.get("quantity", 0)
            if pos["signal_type"] == "BUY":
                gross_pnl = (exit_price - entry) * qty
            else:
                gross_pnl = (entry - exit_price) * qty

            product = pos.get("product", "MIS")
            costs, _src, breakdown = await resolve_round_trip_costs(
                self.ctx.broker, symbol=symbol, signal_type=pos["signal_type"],
                entry_price=entry, exit_price=exit_price, quantity=qty,
                product=product, cost_config=self.ctx.config.transaction_costs,
            )
            pnl = round(gross_pnl - costs, 2)

            await self.ctx.db.close_position(
                pos["trade_id"], exit_price, pnl, realized_costs=breakdown,
            )
            recovered.append(symbol)

            # Cancel any still-open broker exit legs. When the broker's
            # SL fires server-side, Kite SHOULD postback the SL COMPLETE
            # so our postback handler cancels the resting target LIMIT
            # — but Kite postbacks are best-effort with no retry, and
            # when one is dropped the target stays open at the broker
            # ready to fire on a price spike. Same risk in reverse if
            # the target LIMIT fills and the postback is lost. Issue a
            # cancel here as a backstop; broker treats
            # already-cancelled / already-filled orders as no-op so
            # double-cancelling is harmless.
            for oid_key, label in (
                ("target_order_id", "target"),
                ("sl_order_id", "SL"),
            ):
                oid = pos.get(oid_key)
                if not oid:
                    continue
                try:
                    await self.ctx.broker.cancel_order(str(oid))
                    if oid_key == "target_order_id":
                        await self.ctx.db.set_trade_target_order_id(
                            pos["trade_id"], None,
                        )
                    else:
                        await self.ctx.db.set_trade_sl_order_id(
                            pos["trade_id"], None,
                        )
                    logger.info(
                        "Ghost recovery: cancelled dangling %s order %s for %s",
                        label, oid, symbol,
                    )
                except Exception as e:
                    logger.warning(
                        "Ghost recovery: cancel %s %s for %s failed: %s",
                        label, oid, symbol, e,
                    )

            logger.warning(
                "GHOST POSITION RECOVERED: %s — closed in DB with exit=%.2f "
                "(source=%s) pnl=₹%.2f",
                symbol, exit_price, exit_source, pnl,
            )

            await self.ctx.notify.send_exit_alert(
                symbol, "Broker-side close (auto-recovered)", pnl,
            )

            # Audit trail
            try:
                await self.ctx.db.log_audit(
                    action_type="ghost_position_recovery",
                    skill_name=self.name,
                    input_summary={
                        "symbol": symbol, "trade_id": pos["trade_id"],
                        "entry_price": entry, "exit_price": exit_price,
                    },
                    output_summary={"pnl": pnl, "costs": costs},
                )
            except Exception:
                pass

        return recovered

    async def _check_holding_expiry(
        self,
        pos: dict[str, Any],
        current_price: float,
        entry: float,
        now: Any,
    ) -> dict[str, Any] | None:
        """Check if a position has exceeded its expected holding period and act.

        Returns None if no action needed, or a dict describing the action taken.
        """
        from datetime import datetime

        cfg = self.ctx.config.risk.holding_expiry
        if not cfg.enabled or cfg.action == "ignore":
            return None

        expected_days = pos.get("expected_holding_days")
        if not expected_days or expected_days <= 0:
            return None  # intraday or no holding period set (handled by square-off)

        # Calculate trading days held
        created_at_str = pos.get("created_at", "")
        if not created_at_str:
            return None
        try:
            created_at = datetime.fromisoformat(str(created_at_str))
        except (ValueError, TypeError):
            return None

        # Calculate trading days held using the holiday-aware counter
        # so a position that spans Diwali / Holi / Independence Day
        # weeks isn't counted as expired prematurely. Falls back to
        # the legacy 5/7 approximation only when the start date can't
        # be normalised — should never trigger in practice.
        created_at_date = created_at.date()
        now_date = now.date()
        try:
            trading_days_held = self.ctx.market_hours.trading_days_missing_after(
                created_at_date, now_date,
            )
        except Exception:
            calendar_days = (
                now.replace(tzinfo=None) - created_at.replace(tzinfo=None)
            ).days
            trading_days_held = max(0, int(calendar_days * 5 / 7))

        # Cap at max_holding_days
        effective_expiry = min(expected_days, cfg.max_holding_days)

        if trading_days_held < effective_expiry:
            return None  # not yet expired

        symbol = pos["symbol"]
        qty = pos.get("quantity", 0)

        # Calculate unrealized PnL %
        if pos["signal_type"] == "BUY":
            pnl_pct = (current_price - entry) / entry * 100 if entry else 0
        else:
            pnl_pct = (entry - current_price) / entry * 100 if entry else 0

        result_base = {
            "symbol": symbol,
            "days_held": trading_days_held,
            "expected_days": expected_days,
            "pnl_pct": round(pnl_pct, 2),
        }

        if cfg.action == "force_close":
            # Close regardless of P&L
            pnl = await self._close_expired_position(pos, current_price, entry, qty)
            return {**result_base, "action": "closed", "reason": "force_close", "pnl": pnl}

        # tighten_or_close logic
        if pnl_pct > cfg.breakeven_buffer_pct:
            # In profit — tighten SL to breakeven + buffer
            buffer = entry * cfg.breakeven_buffer_pct / 100
            if pos["signal_type"] == "BUY":
                new_sl = entry + buffer
            else:
                new_sl = entry - buffer

            if self._is_better_sl(pos["signal_type"], new_sl, pos["stop_loss_price"]):
                await self.ctx.broker.modify_sl_order(pos.get("sl_order_id"), new_sl)
                await self.ctx.db.update_position_sl(pos["trade_id"], new_sl)
                logger.info(
                    "position-monitor: HOLDING EXPIRY %s — in profit (%.1f%%), "
                    "SL tightened to %.2f",
                    symbol, pnl_pct, new_sl,
                )
                return {**result_base, "action": "tightened", "new_sl": new_sl}
            return None  # SL already tighter than breakeven

        # At a loss or near breakeven — close the position
        reason = "at_loss" if pnl_pct < cfg.loss_threshold_pct else "near_breakeven"
        pnl = await self._close_expired_position(pos, current_price, entry, qty)
        logger.info(
            "position-monitor: HOLDING EXPIRY %s — %s (%.1f%%), closed at %.2f pnl=₹%.2f",
            symbol, reason, pnl_pct, current_price, pnl,
        )
        return {**result_base, "action": "closed", "reason": reason, "pnl": pnl}

    async def _queue_exit_for_approval(
        self,
        pos: dict[str, Any],
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        """Queue an exit as a pending trade instead of auto-closing (manual mode)."""
        symbol = pos.get("symbol", "?")
        qty = pos.get("quantity", 0)
        product = pos.get("product", "CNC")
        entry = float(pos.get("fill_price") or pos.get("entry_price") or 0)
        exit_side = "SELL" if pos["signal_type"] == "BUY" else "BUY"

        existing = await self.ctx.db.get_pending_trade_by_symbol(symbol)
        if existing:
            logger.info(
                "position-monitor: %s %s — pending exit already queued (id=%s)",
                reason.upper(), symbol, existing.get("id"),
            )
            return

        signal = {
            "symbol": symbol,
            "signal_type": exit_side,
            "entry_price": exit_price,
            "target_price": exit_price,
            "stop_loss_price": exit_price,
            "position_size": qty,
            "confidence_score": 1.0,
            "product": product,
            "model_version": f"exit_{reason}",
            "mode": self.ctx.config.mode,
        }
        pending_id = await self.ctx.db.insert_pending_trade(signal)
        invested = round(qty * entry, 2)
        current_val = round(qty * exit_price, 2)
        pnl_pct = round((pnl / invested) * 100, 2) if invested > 0 else 0

        logger.info(
            "position-monitor: queued %s exit for %s (reason=%s, pending_id=%d, pnl=₹%.2f)",
            exit_side, symbol, reason, pending_id, pnl,
        )
        logger.info(
            "position-monitor: %s %s — queued for approval (manual mode)",
            reason.upper(), symbol,
        )
        await self.ctx.notify.send(
            f"Pending Exit — {reason.upper().replace('_', ' ')}\n"
            f"{exit_side} <b>{symbol}</b> x{qty} ({product})\n"
            f"  Entry: ₹{entry:.2f} → LTP: ₹{exit_price:.2f}\n"
            f"  Invested: ₹{invested:,.2f} | Current: ₹{current_val:,.2f}\n"
            f"  PnL: ₹{pnl:,.2f} ({pnl_pct:+.2f}%)\n"
            f"  SL: ₹{pos.get('stop_loss_price', 0):.2f} | Target: ₹{pos.get('target_price', 0):.2f}\n"
            f"Approve: /approve {symbol}\n"
            f"Reject: /reject {symbol}",
            alert_type="trade_entry",
        )

    async def _close_expired_position(
        self,
        pos: dict[str, Any],
        current_price: float,
        entry: float,
        qty: int,
    ) -> float:
        """Close a position due to holding period expiry."""
        if pos["signal_type"] == "BUY":
            gross_pnl = (current_price - entry) * qty
        else:
            gross_pnl = (entry - current_price) * qty
        product = pos.get("product", "MIS")
        costs, _src, breakdown = await resolve_round_trip_costs(
            self.ctx.broker, symbol=pos["symbol"], signal_type=pos["signal_type"],
            entry_price=entry, exit_price=current_price, quantity=qty,
            product=product, cost_config=self.ctx.config.transaction_costs,
        )
        pnl = round(gross_pnl - costs, 2)

        if self.ctx.config.execution.transaction_mode == "manual":
            await self._queue_exit_for_approval(pos, current_price, pnl, "holding_expiry")
        else:
            await self.ctx.db.close_position(
                pos["trade_id"], current_price, pnl, realized_costs=breakdown,
            )
        return pnl

    @staticmethod
    def _find_closing_fill_price(
        broker_trades: list[dict[str, Any]],
        symbol: str,
        side: str,
        quantity: int,
    ) -> tuple[float | None, str]:
        """Return the volume-weighted fill price of the side-matching trades
        for `symbol`, or (None, "no_match") if nothing fits.

        Matches all trades for the symbol whose transaction_type equals `side`
        and sums them up. If the total filled quantity matches `quantity` we
        return the VWAP and source "broker_trades_exact"; if it differs we
        still return the VWAP but flag the source so logs can pick up partial
        or extra fills.
        """
        matches: list[tuple[float, float]] = []  # (qty, avg_price)
        for tr in broker_trades:
            sym = tr.get("tradingsymbol") or tr.get("symbol", "")
            ttype = (tr.get("transaction_type") or "").upper()
            if sym != symbol or ttype != side.upper():
                continue
            try:
                q = float(tr.get("quantity") or 0)
                p = float(tr.get("average_price") or 0)
            except (TypeError, ValueError):
                continue
            if q > 0 and p > 0:
                matches.append((q, p))
        if not matches:
            return None, "no_match"
        total_qty = sum(q for q, _ in matches)
        if total_qty <= 0:
            return None, "no_match"
        vwap = sum(q * p for q, p in matches) / total_qty
        source = (
            "broker_trades_exact" if int(total_qty) == int(quantity)
            else "broker_trades_partial"
        )
        return round(vwap, 2), source

    # Kite GTT lifecycle states that mean "still protecting the position":
    _GTT_LIVE_STATES = {"active", "scheduled"}
    # States that mean "no longer protecting — fall back to client side":
    _GTT_DEAD_STATES = {
        "triggered", "cancelled", "rejected", "expired", "disabled", "deleted",
    }

    @staticmethod
    def _classify_exit_order(
        order: dict[str, Any], signal_type: str,
    ) -> str | None:
        """Classify a broker open order as 'sl', 'target', or None for
        the purpose of orphan-exit reconciliation. An "exit" order is
        one whose transaction_type opposes the position's entry side —
        SELL for a BUY position, BUY for a SELL position — and whose
        order_type matches the leg pattern we place at entry time
        (SL/SL-M for the stop, LIMIT for the target).
        """
        order_type = (order.get("order_type") or "").upper()
        side = (order.get("transaction_type") or "").upper()
        expected_exit_side = "SELL" if signal_type == "BUY" else "BUY"
        if side != expected_exit_side:
            return None
        if order_type in ("SL", "SL-M"):
            return "sl"
        if order_type == "LIMIT":
            return "target"
        return None

    async def _reconcile_orphan_broker_exits(
        self, positions: list[dict[str, Any]],
    ) -> None:
        """For positions whose DB row claims no broker-side exit is
        attached, look at the broker's current GTT list and pending
        orders. If we find a resting exit for the symbol, either heal
        the DB row (when we can match it cleanly) or flag the in-memory
        row as `_broker_partial_protected` so the client-side exit
        path stays quiet for this cycle.

        Paper mode is a no-op (paper broker doesn't manage broker-side
        exits).
        """
        if not positions or self.ctx.config.mode == "paper":
            return
        candidates = [
            p for p in positions
            if not p.get("gtt_id")
            and not (p.get("target_order_id") and p.get("sl_order_id"))
        ]
        if not candidates:
            return

        try:
            gtts = await self.ctx.broker.get_gtts()
        except Exception:
            logger.debug("orphan reconcile: get_gtts failed", exc_info=True)
            gtts = []
        try:
            pending = await self.ctx.broker.get_pending_orders()
        except Exception:
            logger.debug("orphan reconcile: get_pending_orders failed", exc_info=True)
            pending = []

        gtts_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for g in gtts or []:
            status = (g.get("status") or "").lower()
            if status not in self._GTT_LIVE_STATES:
                continue
            cond = g.get("condition") or {}
            sym = cond.get("tradingsymbol") or g.get("tradingsymbol")
            if sym:
                gtts_by_symbol.setdefault(str(sym), []).append(g)

        pending_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for o in pending or []:
            sym = o.get("tradingsymbol")
            if sym:
                pending_by_symbol.setdefault(str(sym), []).append(o)

        for pos in candidates:
            symbol = pos.get("symbol") or ""
            sig_type = pos.get("signal_type", "BUY")
            product = (pos.get("product") or "").upper()

            # --- CNC: heal from broker GTT list ---
            if product == "CNC":
                hits = gtts_by_symbol.get(symbol, [])
                if hits:
                    gtt_id = 0
                    for g in hits:
                        try:
                            gtt_id = int(g.get("id") or g.get("trigger_id") or 0)
                            if gtt_id:
                                break
                        except (TypeError, ValueError):
                            continue
                    if gtt_id:
                        logger.warning(
                            "position-monitor: orphan broker GTT %d for %s — "
                            "healing local row to skip client-side exit",
                            gtt_id, symbol,
                        )
                        pos["gtt_id"] = gtt_id
                        try:
                            await self.ctx.db.set_trade_gtt(pos["trade_id"], gtt_id)
                        except Exception:
                            logger.debug(
                                "orphan reconcile: set_trade_gtt heal failed",
                                exc_info=True,
                            )
                        continue

            # --- MIS: heal full pair, or flag partial ---
            if product == "MIS":
                orders = pending_by_symbol.get(symbol, [])
                target_oid: str | None = None
                sl_oid: str | None = None
                for o in orders:
                    kind = self._classify_exit_order(o, sig_type)
                    oid = o.get("order_id")
                    if kind == "target" and not target_oid and oid:
                        target_oid = str(oid)
                    elif kind == "sl" and not sl_oid and oid:
                        sl_oid = str(oid)
                if target_oid and sl_oid:
                    logger.warning(
                        "position-monitor: orphan broker MIS OCO pair for %s "
                        "(target=%s, sl=%s) — healing local row",
                        symbol, target_oid, sl_oid,
                    )
                    pos["target_order_id"] = target_oid
                    pos["sl_order_id"] = sl_oid
                    try:
                        await self.ctx.db.set_trade_target_order_id(
                            pos["trade_id"], target_oid,
                        )
                        await self.ctx.db.set_trade_sl_order_id(
                            pos["trade_id"], sl_oid,
                        )
                    except Exception:
                        logger.debug(
                            "orphan reconcile: MIS heal failed", exc_info=True,
                        )
                    continue
                if target_oid or sl_oid:
                    logger.warning(
                        "position-monitor: orphan partial broker exit for %s "
                        "(target=%s, sl=%s) — deferring client-side exit to "
                        "avoid duplicates; investigate at broker",
                        symbol, target_oid or "—", sl_oid or "—",
                    )
                    pos["_broker_partial_protected"] = True

    async def _reconcile_gtts(self, positions: list[dict[str, Any]]) -> None:
        """For each open position with a `gtt_id`, look up the GTT at the
        broker and update `gtt_status`. Wipe `gtt_id` when the GTT is no
        longer in a state that protects the position so the rest of the
        monitor loop falls back to client-side detection.

        Mutates `positions` in place so downstream checks see the
        post-reconcile state without another DB read.
        """
        attached = [p for p in positions if p.get("gtt_id")]
        if not attached:
            return
        try:
            gtts = await self.ctx.broker.get_gtts()
        except Exception as e:
            logger.debug("GTT reconcile: get_gtts failed: %s", e)
            return

        # Index by trigger_id for O(1) lookup
        by_id: dict[int, dict[str, Any]] = {}
        for g in gtts or []:
            try:
                by_id[int(g.get("id") or g.get("trigger_id") or 0)] = g
            except (TypeError, ValueError):
                continue

        for pos in attached:
            try:
                gid = int(pos["gtt_id"])
            except (TypeError, ValueError):
                continue
            g = by_id.get(gid)
            if g is None:
                # GTT vanished entirely — broker forgot it or it was deleted
                # outside our system. Clear locally so client-side takes over.
                status = "missing"
                clear_id = True
            else:
                status = (g.get("status") or "").lower() or "unknown"
                clear_id = status in self._GTT_DEAD_STATES

            # Always cache the latest status so the UI badge stays fresh
            if pos.get("gtt_status") != status:
                try:
                    await self.ctx.db.set_trade_gtt_status(pos["trade_id"], status)
                except Exception:
                    logger.debug("set_trade_gtt_status failed", exc_info=True)
                await self.ctx.db.log_gtt_event(
                    trade_id=pos["trade_id"], gtt_id=gid, symbol=pos.get("symbol"),
                    event_type="status_change", status=status,
                    details={"previous": pos.get("gtt_status")},
                )
                pos["gtt_status"] = status

            if clear_id:
                logger.warning(
                    "GTT reconcile: trade %s GTT %d now '%s' — clearing "
                    "gtt_id so client-side exit detection resumes",
                    pos["trade_id"], gid, status,
                )
                try:
                    await self.ctx.db.set_trade_gtt(pos["trade_id"], None)
                except Exception:
                    logger.debug("set_trade_gtt(None) failed", exc_info=True)
                pos["gtt_id"] = None

    async def _maybe_trail_gtt_sl(
        self,
        pos: dict[str, Any],
        entry: float,
        current_sl: float,
        current_price: float,
        risk_per_share: float,
    ) -> None:
        """If the position has a broker-side GTT and the trailing-SL
        condition is met, modify the GTT to raise the stoploss leg.

        Without this, GTT-attached positions silently skip trailing
        because the previous trailing path called `modify_sl_order`,
        which only works on plain SL orders, not GTT legs.
        """
        cfg = self.ctx.config.risk
        gtt_id = pos.get("gtt_id")
        if not (gtt_id and hasattr(self.ctx.broker, "modify_gtt")):
            return

        signal_type = pos["signal_type"]
        if signal_type == "BUY":
            profit = current_price - entry
        else:
            profit = entry - current_price
        target = float(pos.get("target_price") or 0.0)
        target_distance = abs(target - entry) if target > 0 else 0.0
        # trades table only carries `expected_holding_days`; derive the
        # bucket directly. 0 days == intraday by definition.
        holding_bucket = (
            "intraday" if int(pos.get("expected_holding_days") or 0) == 0
            else "swing"
        )
        trigger_profit = cfg.resolve_trailing_trigger(
            holding_period=holding_bucket,
            risk_per_share=risk_per_share,
            target_distance=target_distance,
        )
        if profit < trigger_profit:
            return
        profit_multiple = profit / risk_per_share if risk_per_share > 0 else 0.0

        # Mirror the client-side trailing-SL tightening near target.
        step_pct = cfg.trailing_sl_step_pct
        tweaks = cfg.exit_tweaks
        if tweaks.tighten_trailing_enabled:
            target_progress = self._target_progress_pct(
                signal_type, entry, float(pos.get("target_price") or 0.0),
                current_price,
            )
            step_pct *= self._trailing_step_multiplier(target_progress, tweaks)
        step = current_price * step_pct
        if signal_type == "BUY":
            new_sl = max(entry, current_price - step)  # at least breakeven
        else:
            new_sl = min(entry, current_price + step)

        if not self._is_better_sl(signal_type, new_sl, current_sl):
            return

        # Re-supply both legs (Kite's modify_gtt requires it). Target
        # stays at original; only SL trigger / SL limit move.
        exit_side = "SELL" if signal_type == "BUY" else "BUY"
        tgt = float(pos["target_price"])
        buf = 0.005
        if exit_side == "SELL":
            sl_limit = new_sl * (1 - buf)
            tgt_limit = tgt * (1 - buf * 0.5)
        else:
            sl_limit = new_sl * (1 + buf)
            tgt_limit = tgt * (1 + buf * 0.5)

        try:
            await self.ctx.broker.modify_gtt(
                gtt_id=int(gtt_id),
                symbol=pos["symbol"],
                side=exit_side,
                quantity=int(pos["quantity"]),
                stoploss_trigger=new_sl,
                stoploss_limit=sl_limit,
                target_trigger=tgt,
                target_limit=tgt_limit,
                last_price=float(current_price),
            )
            await self.ctx.db.update_position_sl(pos["trade_id"], new_sl)
            await self.ctx.db.log_gtt_event(
                trade_id=pos["trade_id"], gtt_id=int(gtt_id),
                symbol=pos.get("symbol"),
                event_type="modified", status="active",
                details={
                    "reason": "trailing_sl",
                    "sl_trigger": new_sl, "sl_limit": sl_limit,
                    "target_trigger": tgt, "target_limit": tgt_limit,
                    "profit_multiple": round(profit_multiple, 3),
                },
            )
            logger.info(
                "trailing SL via GTT: %s SL %.2f → %.2f (profit %.2fR, gtt=%d)",
                pos["symbol"], current_sl, new_sl, profit_multiple, gtt_id,
            )
        except Exception:
            logger.exception(
                "trailing SL via GTT failed for %s (gtt=%d)",
                pos["symbol"], gtt_id,
            )

    async def _maybe_trail_mis_sl(
        self,
        pos: dict[str, Any],
        entry: float,
        current_sl: float,
        current_price: float,
        risk_per_share: float,
        target: float,
    ) -> None:
        """If a MIS position has a broker-side SL order and the
        trailing condition is met, lift the SL trigger via
        modify_sl_order (same order_id; trigger lifted in place).

        Mirrors the client-side trailing path and the GTT path so
        MIS OCO positions get the same lock-in behaviour. Without
        this the broker-side SL stays at the original level forever
        and a near-target pullback can wipe out the gains.
        """
        cfg = self.ctx.config.risk
        sl_oid = pos.get("sl_order_id")
        if not sl_oid:
            return
        signal_type = pos["signal_type"]
        if signal_type == "BUY":
            profit = current_price - entry
        else:
            profit = entry - current_price
        target_distance = abs(target - entry) if target > 0 else 0.0
        # trades table only carries `expected_holding_days`; derive the
        # bucket directly. 0 days == intraday by definition.
        holding_bucket = (
            "intraday" if int(pos.get("expected_holding_days") or 0) == 0
            else "swing"
        )
        trigger_profit = cfg.resolve_trailing_trigger(
            holding_period=holding_bucket,
            risk_per_share=risk_per_share,
            target_distance=target_distance,
        )
        if profit < trigger_profit:
            return

        step_pct = cfg.trailing_sl_step_pct
        tweaks = cfg.exit_tweaks
        if tweaks.tighten_trailing_enabled:
            target_progress = self._target_progress_pct(
                signal_type, entry, target, current_price,
            )
            step_pct *= self._trailing_step_multiplier(target_progress, tweaks)
        step = current_price * step_pct
        if signal_type == "BUY":
            new_sl = max(entry, current_price - step)  # at least breakeven
        else:
            new_sl = min(entry, current_price + step)

        if not self._is_better_sl(signal_type, new_sl, current_sl):
            return

        try:
            await self.ctx.broker.modify_sl_order(sl_oid, new_sl)
            await self.ctx.db.update_position_sl(pos["trade_id"], new_sl)
            profit_multiple = profit / risk_per_share if risk_per_share > 0 else 0.0
            logger.info(
                "trailing SL via MIS modify: %s SL %.2f → %.2f (profit %.2fR)",
                pos.get("symbol"), current_sl, new_sl, profit_multiple,
            )
        except Exception:
            logger.exception(
                "trailing SL via MIS modify failed for %s (sl_order_id=%s)",
                pos.get("symbol"), sl_oid,
            )

    async def _enforce_mis_oco(self, pos: dict[str, Any]) -> None:
        """Keep MIS OCO honest: when one of the two broker-side exit orders
        (target LIMIT or SL) fills, cancel the other.

        DB-side close happens via ghost-position reconciliation on the
        next cycle — once the broker position vanishes, that path picks
        the actual fill price from `kite.trades()` and closes the row.
        """
        target_oid = pos.get("target_order_id")
        sl_oid = pos.get("sl_order_id")
        try:
            target_status = await self.ctx.broker.get_order_status(target_oid)
            sl_status = await self.ctx.broker.get_order_status(sl_oid)
        except Exception as e:
            logger.debug("OCO status fetch failed for %s: %s", pos.get("symbol"), e)
            return

        def is_filled(s: dict[str, Any]) -> bool:
            return (s.get("status") or "").upper() in {"COMPLETE", "FILLED"}

        target_filled = is_filled(target_status)
        sl_filled = is_filled(sl_status)

        if target_filled and not sl_filled:
            try:
                await self.ctx.broker.cancel_order(sl_oid)
            except Exception as e:
                logger.warning("OCO: failed to cancel SL %s: %s", sl_oid, e)
            await self.ctx.db.set_trade_sl_order_id(pos["trade_id"], None)
            logger.info(
                "OCO: target filled for %s — cancelled SL %s",
                pos.get("symbol"), sl_oid,
            )
            return

        if sl_filled and not target_filled:
            try:
                await self.ctx.broker.cancel_order(target_oid)
            except Exception as e:
                logger.warning("OCO: failed to cancel target %s: %s", target_oid, e)
            await self.ctx.db.set_trade_target_order_id(pos["trade_id"], None)
            logger.info(
                "OCO: SL filled for %s — cancelled target LIMIT %s",
                pos.get("symbol"), target_oid,
            )
            return

        if target_filled and sl_filled:
            # Both filled in the same window (violent reversal). Nothing
            # to cancel; ghost recovery will close the DB row next cycle.
            # Log it so a post-mortem doesn't look at the trade and ask
            # "where did the OCO cancel go?".
            logger.info(
                "OCO: both legs filled for %s in same window — no cancel needed",
                pos.get("symbol"),
            )

    @staticmethod
    def _trailing_step_multiplier(progress: float, cfg: Any) -> float:
        """Step-up curve. progress < start → 1.0 (no tighten). At
        start, first bucket of decay applies; every step_size of
        additional progress applies another bucket. Floored at
        min_multiplier so the SL doesn't crawl to zero.
        """
        if not cfg.tighten_trailing_enabled:
            return 1.0
        if progress < cfg.tighten_start_at_target_pct:
            return 1.0
        excess = progress - cfg.tighten_start_at_target_pct
        # +1 so bucket 1 applies at the start threshold (some tighten
        # at the trigger, rather than zero). 1e-9 epsilon to avoid
        # floating-point drift at exact 0.10 multiples (without it,
        # 0.60 - 0.50 = 0.0999... and floors to bucket 1 instead of 2).
        buckets = int(excess / cfg.tighten_step_size + 1e-9) + 1
        return max(
            cfg.tighten_min_multiplier,
            1.0 - buckets * cfg.tighten_step_decay,
        )

    @staticmethod
    def _target_progress_pct(
        signal_type: str, entry: float, target: float, current_price: float,
    ) -> float:
        """Fraction of the entry-to-target distance already covered, in
        [0, 1]+. Returns 0 if target is unset or geometry is invalid.
        Goes >1 when current_price has already crossed target.
        """
        if target <= 0 or entry <= 0:
            return 0.0
        total = abs(target - entry)
        if total <= 0:
            return 0.0
        if signal_type == "BUY":
            covered = current_price - entry
        else:
            covered = entry - current_price
        return max(0.0, covered / total)

    async def _check_auxiliary_exits(
        self,
        pos: dict[str, Any],
        current_price: float,
        entry: float,
        target: float,
    ) -> str | None:
        """Return a reason string when a time-stop or volume-exhaustion
        exit should fire, else None. Only fires for client-side
        managed positions (no GTT, no MIS OCO pair).

        Time-stop: intraday positions that have been open longer than
        `intraday_stop_after_min` without crossing
        `intraday_stop_progress_threshold` of target progress get
        exited at market.

        Volume-exhaustion: when the last 5-min bar's volume drops below
        `volume_exit_min_ratio` × average of the previous N bars AND
        the position is in 0.5R-2R profit (the "trend is dying" zone),
        exit at market.
        """
        if pos.get("gtt_id") or (
            pos.get("target_order_id") and pos.get("sl_order_id")
        ):
            return None
        tweaks = self.ctx.config.risk.exit_tweaks
        if not tweaks.time_stop_enabled and not tweaks.volume_exit_enabled:
            return None

        signal_type = pos.get("signal_type", "BUY")
        progress = self._target_progress_pct(
            signal_type, entry, target, current_price,
        )

        # Time-stop: intraday only — swing rows already have
        # holding-expiry covering them.
        if (
            tweaks.time_stop_enabled
            and pos.get("expected_holding_period") == "intraday"
        ):
            from datetime import datetime as _dt

            from yolovest.timezone import IST as _IST
            from yolovest.timezone import now_ist as _now_ist
            created_at_str = pos.get("created_at") or ""
            try:
                created_at = _dt.fromisoformat(str(created_at_str))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=_IST)
                age_min = (
                    _now_ist() - created_at.astimezone(_IST)
                ).total_seconds() / 60
                if (
                    age_min >= tweaks.intraday_stop_after_min
                    and progress < tweaks.intraday_stop_progress_threshold
                ):
                    return "time_stop"
            except (ValueError, TypeError):
                logger.debug("time-stop: bad created_at on %s",
                             pos.get("symbol"), exc_info=True)

        # Volume-exhaustion: last 5-min bar volume vs lookback average.
        if tweaks.volume_exit_enabled:
            sl = float(pos.get("stop_loss_price") or 0)
            risk_per_share = abs(entry - sl)
            if risk_per_share > 0:
                if signal_type == "BUY":
                    profit_R = (current_price - entry) / risk_per_share
                else:
                    profit_R = (entry - current_price) / risk_per_share
                if 0.5 <= profit_R <= 2.0:
                    try:
                        bars = await self.ctx.db.get_ohlcv(
                            pos["symbol"], "5minute", days=1,
                        )
                    except Exception:
                        bars = []
                    needed = tweaks.volume_exit_lookback_bars + 1
                    if len(bars) >= needed:
                        latest = bars[-1]
                        history = bars[-needed:-1]
                        avg_vol = sum(b.volume for b in history) / len(history)
                        if avg_vol > 0:
                            ratio = latest.volume / avg_vol
                            if ratio < tweaks.volume_exit_min_ratio:
                                return "volume_exhaustion"
        return None

    def _is_better_sl(self, signal_type: str, new_sl: float, current_sl: float) -> bool:
        """Check if new SL is tighter (more protective) than current."""
        if signal_type == "BUY":
            return new_sl > current_sl
        return new_sl < current_sl

    async def _check_partial_profit_booking(
        self,
        pos: dict[str, Any],
        current_price: float,
    ) -> bool:
        """Book partial profits at an intermediate target level.

        Returns True if a partial booking was executed this cycle (caller
        should skip target/SL checks to let the order settle), False otherwise.
        """
        cfg = self.ctx.config.risk.partial_profit
        if not cfg.enabled:
            return False

        position_id = pos["trade_id"]

        # Check if this position already had a partial booking
        already_booked = await self.ctx.db.get_system_state(
            f"partial_booked_{position_id}",
        )
        if already_booked:
            return False

        entry = float(pos.get("fill_price") or pos["entry_price"])
        target = pos["target_price"]
        signal_type = pos["signal_type"]

        # Calculate intermediate target
        if signal_type == "BUY":
            intermediate_target = entry + (target - entry) * cfg.first_target_pct
            crossed = current_price >= intermediate_target
        else:
            intermediate_target = entry - (entry - target) * cfg.first_target_pct
            crossed = current_price <= intermediate_target

        if not crossed:
            return False

        qty = pos.get("quantity", 0)
        close_qty = round(qty * cfg.first_close_pct)
        if close_qty <= 0:
            return False

        # In manual mode, skip auto partial profit — user controls all exits
        if self.ctx.config.execution.transaction_mode == "manual":
            logger.info(
                "position-monitor: partial profit target crossed for %s but skipping (manual mode)",
                pos["symbol"],
            )
            return False

        # Place the partial exit order
        try:
            if signal_type == "BUY":
                await self.ctx.broker.place_order(
                    symbol=pos["symbol"],
                    side="SELL",
                    quantity=close_qty,
                    price=current_price,
                    order_type="MARKET",
                    product=pos.get("product", "MIS"),
                    tag="yv-partial",
                )
            else:
                await self.ctx.broker.place_order(
                    symbol=pos["symbol"],
                    side="BUY",
                    quantity=close_qty,
                    price=current_price,
                    order_type="MARKET",
                    product=pos.get("product", "MIS"),
                    tag="yv-partial",
                )
        except Exception:
            logger.exception(
                "position-monitor: PARTIAL PROFIT order failed for %s",
                pos["symbol"],
            )
            return False

        # Move SL to breakeven if configured
        remaining_qty = qty - close_qty
        if cfg.move_sl_to_breakeven:
            try:
                sl_order_id = pos.get("sl_order_id")
                if sl_order_id:
                    await self.ctx.broker.modify_sl_order(sl_order_id, entry)
                    await self.ctx.db.update_position_sl(position_id, entry)
            except Exception:
                logger.exception(
                    "position-monitor: Failed to move SL to breakeven for %s",
                    pos["symbol"],
                )

        # If a broker-side OCO GTT protects this CNC position, resize it
        # to match the remaining quantity. Without this, when either leg
        # fires Kite will reject the order because we no longer have the
        # original qty. SL trigger moves to breakeven too when configured.
        gtt_id = pos.get("gtt_id")
        if gtt_id and remaining_qty > 0 and hasattr(self.ctx.broker, "modify_gtt"):
            exit_side = "SELL" if signal_type == "BUY" else "BUY"
            new_sl = entry if cfg.move_sl_to_breakeven else float(pos["stop_loss_price"])
            tgt = float(pos["target_price"])
            limit_buf = 0.005
            if exit_side == "SELL":
                sl_limit = new_sl * (1 - limit_buf)
                tgt_limit = tgt * (1 - limit_buf * 0.5)
            else:
                sl_limit = new_sl * (1 + limit_buf)
                tgt_limit = tgt * (1 + limit_buf * 0.5)
            try:
                await self.ctx.broker.modify_gtt(
                    gtt_id=int(gtt_id),
                    symbol=pos["symbol"],
                    side=exit_side,
                    quantity=int(remaining_qty),
                    stoploss_trigger=new_sl,
                    stoploss_limit=sl_limit,
                    target_trigger=tgt,
                    target_limit=tgt_limit,
                    last_price=float(current_price),
                )
                await self.ctx.db.log_gtt_event(
                    trade_id=pos["trade_id"], gtt_id=int(gtt_id),
                    symbol=pos["symbol"],
                    event_type="modified", status="active",
                    details={
                        "reason": "partial_booking_resize",
                        "quantity": remaining_qty,
                        "sl_trigger": new_sl, "sl_limit": sl_limit,
                        "target_trigger": tgt, "target_limit": tgt_limit,
                    },
                )
                logger.info(
                    "position-monitor: resized GTT %d for %s to qty=%d (SL=%.2f)",
                    gtt_id, pos["symbol"], remaining_qty, new_sl,
                )
            except Exception:
                logger.exception(
                    "position-monitor: GTT resize after partial booking failed for %s",
                    pos["symbol"],
                )

        # Mark as partially booked
        await self.ctx.db.set_system_state(
            f"partial_booked_{position_id}", "true",
        )

        logger.info(
            "position-monitor: PARTIAL PROFIT BOOKED %s — "
            "closed %d/%d shares at %.2f (intermediate target %.2f), "
            "SL moved to breakeven=%s",
            pos["symbol"],
            close_qty,
            qty,
            current_price,
            intermediate_target,
            cfg.move_sl_to_breakeven,
        )

        return True
