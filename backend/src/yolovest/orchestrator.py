"""Heartbeat orchestrator for YoloVest.

Implements the heartbeat pipeline with error propagation,
heartbeat mutex (skip-on-overrun), and consecutive skip alerting.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, ClassVar, cast

from yolovest.context import AppContext
from yolovest.skills import SKILL_REGISTRY
from yolovest.skills.base import SkillBase, SkillResult

# Callback type for skill completion broadcasting
SkillCallback = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)


class HeartbeatOrchestrator:
    """Orchestrates the heartbeat pipeline.

    Pipeline execution order (market hours):
        health-check -> ingest-data -> market-scan -> generate-signals
          -> [per signal]: risk-check -> llm-review -> trade-execute -> predict-track
          -> position-monitor

    Error propagation policy:
        - health-check fails -> ABORT entire heartbeat
        - ingest-data fails -> SKIP scan+signals, run position-monitor
        - market-scan fails -> SKIP signals, run position-monitor
        - generate-signals fails -> SKIP signal chain, run position-monitor
        - Per-signal failures -> skip that signal, continue others

    Heartbeat mutex:
        If a heartbeat is still running when the next fires, skip and log warning.
        After max_consecutive_skips, alert via Telegram as CRITICAL.
    """

    PIPELINE_SKILLS: ClassVar[list[str]] = [
        "health-check",
        "ingest-data",
        "depth-snapshot",
        "market-scan",
        "generate-signals",
    ]

    SIGNAL_CHAIN_SKILLS: ClassVar[list[str]] = [
        "risk-check",
        "llm-review",
        "trade-execute",
        "predict-track",
    ]

    def __init__(
        self,
        ctx: AppContext,
        skills: dict[str, SkillBase] | None = None,
    ) -> None:
        self._ctx = ctx
        self._lock = asyncio.Lock()
        self._consecutive_skips = 0
        self._max_consecutive_skips = ctx.config.heartbeat.max_consecutive_skips
        self._running = False
        self._on_skill_complete: SkillCallback | None = None
        self._watchdog: Any | None = None
        self._skills: dict[str, SkillBase] = skills if skills is not None else {}
        if skills is None:
            self._init_skills()

    def set_watchdog(self, watchdog: Any) -> None:
        """Set the heartbeat watchdog reference."""
        self._watchdog = watchdog

    def _init_skills(self) -> None:
        """Instantiate all registered skills with context."""
        for name, skill_cls in SKILL_REGISTRY.items():
            self._skills[name] = skill_cls(self._ctx)

    @property
    def consecutive_skips(self) -> int:
        """Number of consecutive heartbeats skipped due to overrun."""
        return self._consecutive_skips

    def _get_skill(self, name: str) -> SkillBase | None:
        """Get an instantiated skill by name."""
        return self._skills.get(name)

    async def run_heartbeat(self, *, source: str = "scheduled") -> dict[str, Any]:
        """Execute one heartbeat cycle.

        Returns a dict with skill results and metadata. SkillResult values are
        keyed by skill name; metadata keys include 'skipped', 'aborted',
        'signal_pipeline', 'consecutive_skips', 'symbol'.

        `source` is "scheduled" for the auto-loop's invocation and
        "manual" when triggered by the user via the heartbeat-pipeline
        skill. Manual triggers that race with a running heartbeat
        skip without bumping `_consecutive_skips` — that counter is
        meant to detect a scheduled cycle overrunning its 15-min
        budget, not a user clicking Run twice.
        """
        # Mutex: skip if already running
        if self._lock.locked():
            if source == "scheduled":
                self._consecutive_skips += 1
                logger.warning(
                    "Heartbeat skipped (still running). Consecutive skips: %d",
                    self._consecutive_skips,
                )
                if self._consecutive_skips >= self._max_consecutive_skips:
                    await self._ctx.notify.send(
                        f"CRITICAL: {self._consecutive_skips} consecutive heartbeats "
                        f"skipped due to overrun.",
                        alert_type="errors",
                    )
            else:
                logger.info(
                    "Heartbeat (%s) skipped — scheduled cycle still running",
                    source,
                )
            return {
                "skipped": True, "source": source,
                "consecutive_skips": self._consecutive_skips,
            }

        async with self._lock:
            self._consecutive_skips = 0
            return await self._execute_pipeline()

    async def _execute_pipeline(self) -> dict[str, Any]:
        """Execute the full heartbeat pipeline with error propagation."""
        results: dict[str, Any] = {}
        await self._broadcast("heartbeat_started", {
            "market_hours": self._ctx.market_hours.is_market_hours(),
        })

        # Sweep abandoned pending trades, then re-anchor the survivors
        # to the latest LTP, before health-check runs. Risk-check counts
        # pending notional + pending count toward exposure /
        # max_open_positions / max_trades_per_day, so a forgotten
        # pending silently locks those budgets and chokes off the day's
        # signals. Both steps run as registered skills now — that keeps
        # the audit-log + skill_completed broadcasts symmetric with
        # the rest of the pipeline and lets the user trigger them on
        # demand from Telegram /run or the dashboard Skills page.
        results["expire-pending-trades"] = await self._run_skill(
            "expire-pending-trades",
        )
        results["reprice-pending-trades"] = await self._run_skill(
            "reprice-pending-trades",
        )

        # --- Step 1: health-check (ABORT on failure) ---
        health_result = await self._run_skill("health-check")
        results["health-check"] = health_result

        if not health_result.success:
            logger.error("health-check failed — ABORTING heartbeat")
            await self._ctx.notify.send(
                "ABORT: health-check failed. Heartbeat aborted.\n"
                f"Error: {health_result.error}",
                alert_type="errors",
            )
            results["aborted"] = True
            return results

        # --- Step 2: ingest-data (SKIP scan+signals on failure) ---
        ingest_result = await self._run_skill("ingest-data")
        results["ingest-data"] = ingest_result

        if not ingest_result.success:
            logger.warning("ingest-data failed — skipping scan+signals")
            # Still run position-monitor
            pm_result = await self._run_skill("position-monitor")
            results["position-monitor"] = pm_result
            await self._alert_position_monitor(pm_result)
            return results

        # --- Step 2b: depth-snapshot (best-effort data collection;
        # never blocks the pipeline — the skill itself returns success
        # with a reason on any failure) ---
        results["depth-snapshot"] = await self._run_skill("depth-snapshot")

        # --- Step 3: market-scan (SKIP signals on failure) ---
        scan_result = await self._run_skill("market-scan")
        results["market-scan"] = scan_result

        if not scan_result.success:
            logger.warning("market-scan failed — skipping signals")
            pm_result = await self._run_skill("position-monitor")
            results["position-monitor"] = pm_result
            await self._alert_position_monitor(pm_result)
            return results

        # --- Step 4: generate-signals (SKIP signal chain on failure) ---
        signals_result = await self._run_skill("generate-signals")
        results["generate-signals"] = signals_result

        if not signals_result.success:
            logger.warning("generate-signals failed — skipping signal chain")
            pm_result = await self._run_skill("position-monitor")
            results["position-monitor"] = pm_result
            await self._alert_position_monitor(pm_result)
            return results

        # --- Step 5: Per-signal chain ---
        # Process highest-conviction signals first. The portfolio-exposure
        # cap is a binding constraint when several signals fire on the
        # same heartbeat — earlier signals consume budget that later
        # ones can't get back. Sorting by confidence descending means
        # the best signals get evaluated first, and a low-conviction
        # signal can't block a high-conviction one purely because of
        # its position in the list.
        signals = signals_result.data.get("signals", [])
        signals = sorted(
            signals,
            key=lambda s: float(
                (s.get("confidence_score") if isinstance(s, dict) else 0) or 0
            ),
            reverse=True,
        )
        signal_pipeline = []
        for i, signal in enumerate(signals):
            signal_results = await self._execute_signal_chain(signal, i)
            signal_pipeline.append(signal_results)
            results.update(signal_results)
        results["signal_pipeline"] = signal_pipeline

        # --- Step 6: position-monitor (always runs) ---
        pm_result = await self._run_skill("position-monitor")
        results["position-monitor"] = pm_result
        await self._alert_position_monitor(pm_result)

        # Persist heartbeat state for cross-restart continuity
        if self._ctx.memory:
            try:
                summary = {
                    "signals_processed": len(signals),
                    "signal_results": [
                        {k: v.success if isinstance(v, SkillResult) else v
                         for k, v in sr.items()}
                        for sr in signal_pipeline
                    ],
                    "position_monitor_ok": pm_result.success,
                }
                await self._ctx.memory.save_heartbeat_state(summary)
            except Exception:
                logger.debug("Failed to persist heartbeat state", exc_info=True)

        # Broadcast heartbeat completion
        skill_results = [
            r for r in results.values() if isinstance(r, SkillResult)
        ]
        await self._broadcast("heartbeat_completed", {
            "skills_run": len(skill_results),
            "skills_succeeded": sum(1 for r in skill_results if r.success),
            "signals_generated": len(signals) if "generate-signals" in results else 0,
        })

        return results

    @staticmethod
    def _today_start() -> str:
        """Return today's start time in UTC ISO format for signal cleanup."""
        from datetime import UTC

        from yolovest.timezone import now_ist
        return now_ist().replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).astimezone(UTC).isoformat()

    async def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish an event to the event bus (bridged to WebSocket)."""
        try:
            from yolovest.events import Event
            await self._ctx.event_bus.publish(Event(event_type=event_type, data=data))
        except Exception:
            logger.debug("Failed to broadcast event %s", event_type, exc_info=True)

    def _signal_symbol(self, signal: object) -> str:
        if isinstance(signal, dict):
            return signal.get("symbol", "")
        return getattr(signal, "symbol", "") or ""

    async def _set_disposition(
        self, signal: object, disposition: str, reason: str | None = None
    ) -> None:
        symbol = self._signal_symbol(signal)
        if not symbol:
            return
        # Pull the post-risk-check position_size off the signal so the
        # signals row stops showing the model's placeholder 1.
        size: int | None = None
        if isinstance(signal, dict):
            raw = signal.get("position_size")
        else:
            raw = getattr(signal, "position_size", None)
        try:
            size = int(raw) if raw else None
        except (TypeError, ValueError):
            size = None
        try:
            await self._ctx.db.update_signal_disposition(
                symbol, disposition, reason,
                position_size=size,
                mode=self._ctx.config.mode,
            )
        except Exception:
            logger.debug("Failed to update signal disposition", exc_info=True)

    async def _execute_signal_chain(
        self, signal: object, index: int
    ) -> dict[str, Any]:
        """Execute the per-signal chain.

        Chain: risk-check -> llm-review -> trade-execute -> predict-track.
        Per-signal failures skip that signal only and continue to the next.
        """
        results: dict[str, Any] = {}
        prefix = f"signal-{index}"
        # Include signal metadata
        if isinstance(signal, dict):
            results["symbol"] = signal.get("symbol", "unknown")
        else:
            results["symbol"] = getattr(signal, "symbol", "unknown")

        # risk-check
        risk_result = await self._run_skill("risk-check", signal=signal)
        results[f"{prefix}/risk-check"] = risk_result
        if not risk_result.success:
            # Skill threw an exception (not a clean rejection). Marking
            # this as 'risk_rejected' would burn the retry cap on a
            # persistent bug; mark it distinctly so the retry-cap math
            # only counts genuine risk decisions.
            logger.info("risk-check failed for signal %d — skipping", index)
            await self._set_disposition(signal, "skill_error", "risk-check skill failed")
            return results

        # Check risk approval and use adjusted signal
        if risk_result.data and not risk_result.data.get("approved", True):
            reason = risk_result.data.get("rejection_reason", "")
            # Time-deferred rejections (currently only the
            # pre-order-window block) shouldn't burn a
            # max_risk_rejected_retries_per_day slot — the underlying
            # condition is "wait N minutes", not "this signal is bad".
            # The `time_blocked` disposition is treated as retryable
            # by the dedup query but excluded from the cap count.
            is_deferred = bool(risk_result.data.get("deferred"))
            disposition = "time_blocked" if is_deferred else "risk_rejected"
            logger.info("risk-check rejected signal %d: %s", index, reason)
            await self._set_disposition(signal, disposition, reason)
            return results
        if risk_result.data and risk_result.data.get("signal"):
            signal = risk_result.data["signal"]  # use risk-adjusted signal (position size)

        # llm-review
        llm_result = await self._run_skill("llm-review", signal=signal)
        results[f"{prefix}/llm-review"] = llm_result
        if not llm_result.success:
            if self._ctx.config.risk.llm_fallback_to_rules:
                logger.info(
                    "llm-review failed for signal %d — auto-approving (fallback to rules)",
                    index,
                )
            else:
                logger.info("llm-review failed for signal %d — skipping", index)
                await self._set_disposition(signal, "llm_rejected", "llm-review skill failed")
                return results

        # Check LLM decision (if it succeeded)
        if llm_result.success:
            approved = llm_result.data.get("approved", True)
            if not approved:
                reason = llm_result.data.get("reasoning", "LLM rejected signal")
                logger.info("LLM rejected signal %d", index)
                await self._set_disposition(signal, "llm_rejected", reason)
                return results
            # Use updated signal from LLM (may have been resized)
            if llm_result.data.get("signal"):
                signal = llm_result.data["signal"]

        # Manual approval mode — queue instead of executing
        if self._ctx.config.execution.transaction_mode == "manual":
            # Dedup: skip if a pending entry already exists for this symbol
            sym_for_dedup = signal.get("symbol", "") if isinstance(signal, dict) else ""
            sig_type_for_dedup = signal.get("signal_type", "") if isinstance(signal, dict) else ""
            if sym_for_dedup:
                existing = await self._ctx.db.get_pending_trade_by_symbol(sym_for_dedup)
                if existing:
                    logger.info(
                        "Manual mode: skipping %s — pending trade already exists (id=%s)",
                        sym_for_dedup, existing.get("id"),
                    )
                    await self._set_disposition(
                        signal, "awaiting_approval", "pending trade already exists"
                    )
                    results[f"{prefix}/pending"] = SkillResult(
                        success=True, skill_name="pending-approval",
                        data={"skipped": True, "reason": "pending_exists", "symbol": sym_for_dedup},
                    )
                    return results

                # Respect user rejection: don't re-queue within the cooldown window
                cooldown_hours = self._ctx.config.execution.rejection_cooldown_hours
                if await self._ctx.db.was_recently_rejected(sym_for_dedup, sig_type_for_dedup, hours=cooldown_hours):
                    logger.info(
                        "Manual mode: skipping %s %s — user rejected recently",
                        sig_type_for_dedup, sym_for_dedup,
                    )
                    await self._set_disposition(
                        signal, "recently_rejected_dedup",
                        f"user rejected within last {cooldown_hours}h",
                    )
                    results[f"{prefix}/pending"] = SkillResult(
                        success=True, skill_name="pending-approval",
                        data={"skipped": True, "reason": "recently_rejected", "symbol": sym_for_dedup},
                    )
                    return results

            # Signals are plain dicts end-to-end in practice (the
            # evaluator emits dicts); narrow for the queue insert.
            signal = cast("dict[str, Any]", signal)
            signal.setdefault("mode", self._ctx.config.mode)
            pending_id = await self._ctx.db.insert_pending_trade(signal)
            await self._set_disposition(
                signal, "awaiting_approval", f"pending_id={pending_id}"
            )
            await self._broadcast("pending_queued", {
                "trade_id": pending_id,
                "symbol": self._signal_symbol(signal),
            })
            symbol = sym_for_dedup or "?"
            sig_type = signal.get("signal_type", "?") if isinstance(signal, dict) else "?"
            conf = signal.get("confidence_score", 0) if isinstance(signal, dict) else 0
            entry = signal.get("entry_price", 0) if isinstance(signal, dict) else 0
            target = signal.get("target_price", 0) if isinstance(signal, dict) else 0
            sl = signal.get("stop_loss_price", 0) if isinstance(signal, dict) else 0
            qty = signal.get("position_size", 0) if isinstance(signal, dict) else 0
            product = signal.get("product", "MIS") if isinstance(signal, dict) else "MIS"
            holding = signal.get("expected_holding_period", "") if isinstance(signal, dict) else ""
            days = signal.get("expected_holding_days", 0) if isinstance(signal, dict) else 0
            investment = round(qty * entry, 2)
            risk = round(qty * abs(entry - sl), 2)
            reward = round(qty * abs(target - entry), 2)
            rr_ratio = round(reward / risk, 2) if risk > 0 else 0

            logger.info(
                "Manual mode: queued %s %s @ %.2f conf=%.0f%% (pending_id=%d)",
                sig_type, symbol, entry, conf * 100, pending_id,
            )
            hold_label = f"{holding} ({days}d)" if days > 0 else holding or "intraday"
            await self._ctx.notify.send(
                f"Pending Entry\n"
                f"{sig_type} <b>{symbol}</b> x{qty} ({product}) — {hold_label}\n"
                f"  Entry: ₹{entry:.2f} | Target: ₹{target:.2f} | SL: ₹{sl:.2f}\n"
                f"  Investment: ₹{investment:,.2f}\n"
                f"  Risk: ₹{risk:,.2f} | Reward: ₹{reward:,.2f} (R:R {rr_ratio}:1)\n"
                f"  Confidence: {conf:.0%}\n"
                f"Approve: /approve {symbol}\n"
                f"Reject: /reject {symbol}",
                alert_type="trade_entry",
            )
            results[f"{prefix}/pending"] = SkillResult(
                success=True, skill_name="pending-approval",
                data={"pending_id": pending_id, "symbol": symbol},
            )
            return results

        # trade-execute (auto mode)
        trade_result = await self._run_skill("trade-execute", signal=signal)
        results[f"{prefix}/trade-execute"] = trade_result
        if not trade_result.success:
            symbol = signal.get("symbol", "?") if isinstance(signal, dict) else "?"
            logger.warning("trade-execute failed for signal %d (%s): %s", index, symbol, trade_result.error)
            # Mark the signal as trade_execute_failed (retryable, see
            # get_todays_signaled_symbols) instead of DELETEing the row.
            # The old DELETE wiped all of today's signal rows for the
            # symbol — losing audit history for prior risk-rejections,
            # adopted positions, etc. that happened to have the same
            # symbol earlier in the day.
            await self._set_disposition(
                signal, "trade_execute_failed",
                trade_result.error or "trade-execute failed",
            )
            await self._ctx.notify.send(
                f"Trade execution failed for {symbol}: {trade_result.error}",
                alert_type="errors",
            )
            return results

        # predict-track — log the prediction with trade linkage
        trade_id = None
        if trade_result.success and trade_result.data:
            trade = trade_result.data.get("trade", {})
            trade_id = trade.get("trade_id") or trade.get("order_id")
            await self._set_disposition(signal, "executed", f"trade_id={trade_id}")
        predict_result = await self._run_skill(
            "predict-track", signal=signal, mode="log", trade_id=trade_id
        )
        results[f"{prefix}/predict-track"] = predict_result

        return results

    async def _run_skill(self, name: str, **kwargs: object) -> SkillResult:
        """Run a skill by name using safe_execute."""
        skill = self._get_skill(name)
        if skill is None:
            logger.error("Skill '%s' not found in registry", name)
            return SkillResult(
                success=False,
                skill_name=name,
                error=f"Skill '{name}' not found in registry",
            )

        if not skill.should_run():
            logger.debug("Skill '%s' should_run() returned False — skipping", name)
            return SkillResult(
                success=True,
                skill_name=name,
                data={"skipped": True, "reason": "should_run() returned False"},
            )

        logger.info("Running skill: %s", name)
        # Broadcast stage start so the dashboard can render a per-skill
        # progress chip instead of just "heartbeat running" for 30-60s.
        # skill_completed event is already broadcast separately when
        # the skill finishes (via _on_skill_complete in main.py).
        try:
            await self._broadcast("heartbeat_stage", {"skill": name, "status": "started"})
        except Exception:
            logger.debug("heartbeat_stage broadcast failed", exc_info=True)

        # Timeout to prevent a hung skill from blocking the entire heartbeat
        _SKILL_TIMEOUT_SEC = 300  # 5 minutes max per skill
        try:
            result = await asyncio.wait_for(
                skill.safe_execute(**kwargs), timeout=_SKILL_TIMEOUT_SEC,
            )
        except TimeoutError:
            logger.error(
                "Skill '%s' TIMED OUT after %ds — force-skipping",
                name, _SKILL_TIMEOUT_SEC,
            )
            result = SkillResult(
                success=False,
                skill_name=name,
                error=f"Timed out after {_SKILL_TIMEOUT_SEC}s",
            )
        logger.info(
            "Skill %s completed: success=%s, duration=%.1fms",
            name,
            result.success,
            result.duration_ms,
        )

        # Log to audit trail
        try:
            await self._ctx.db.log_audit(
                action_type="skill_execution",
                skill_name=name,
                output_summary={
                    "success": result.success,
                    "duration_ms": round(result.duration_ms, 1),
                    "error": result.error,
                },
                duration_ms=result.duration_ms,
            )
        except Exception:
            logger.debug("Failed to log audit for skill %s", name, exc_info=True)

        # Broadcast skill completion to WebSocket clients
        if self._on_skill_complete is not None:
            try:
                await self._on_skill_complete("skill_completed", {
                    "skill": name,
                    "success": result.success,
                    "duration_ms": round(result.duration_ms, 1),
                    "error": result.error,
                    "summary": {k: v for k, v in result.data.items()
                                if isinstance(v, (str, int, float, bool, type(None)))}
                    if result.data else {},
                })
            except Exception:
                logger.debug("Skill completion broadcast failed for %s", name, exc_info=True)

        return result

    async def _alert_position_monitor(self, result: SkillResult) -> None:
        """Alert if position-monitor fails (most dangerous failure)."""
        if not result.success:
            await self._ctx.notify.send(
                "CRITICAL: position-monitor failed. Open positions are unmonitored.\n"
                f"Error: {result.error}",
                alert_type="errors",
            )

    async def start(self) -> None:
        """Start the heartbeat loop. Runs until stopped."""
        self._running = True
        self._stop_event = asyncio.Event()
        logger.info("Heartbeat orchestrator started")

        # Let dashboard and other async services start before first heartbeat
        await asyncio.sleep(2)

        # Boundary case: server started inside the [market.open,
        # order_start) gap. is_market_hours() reads True (so the loop's
        # market-hours branch would normally fire immediately) but
        # is_order_window() is False (every signal would be deferred).
        # Sleep through the gap so the first cycle of the day lines up
        # with order_start.
        if (
            self._ctx.market_hours.is_market_hours()
            and not self._ctx.market_hours.is_order_window()
        ):
            try:
                open_in = self._ctx.market_hours.seconds_until_next_order_window()
            except Exception:
                open_in = 0.0
            if open_in > 0:
                logger.info(
                    "Heartbeat: in market-open / order-start gap at startup, "
                    "deferring first cycle by %ds so it aligns with order_start",
                    int(open_in + 2),
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=open_in + 2,
                    )
                except TimeoutError:
                    pass

        while self._running:
            start = time.monotonic()

            try:
                results = await self.run_heartbeat()
                if results and not results.get("skipped"):
                    skill_results = [
                        r for r in results.values() if isinstance(r, SkillResult)
                    ]
                    succeeded = sum(1 for r in skill_results if r.success)
                    logger.info(
                        "Heartbeat completed: %d/%d skills succeeded",
                        succeeded,
                        len(skill_results),
                    )
            except Exception:
                logger.exception("Unhandled error in heartbeat")
                await self._ctx.notify.send(
                    "CRITICAL: Unhandled heartbeat error", alert_type="errors",
                )

            # Notify watchdog that a heartbeat cycle completed (even if errored)
            if self._watchdog:
                self._watchdog.record_heartbeat()

            # Determine interval based on market hours. Off-hours uses
            # the longer cadence (60-min default) — but we also clamp
            # against "time until next order window" so an 8:00 AM
            # heartbeat doesn't lock the next cycle to 9:00 AM and
            # leave the start of the order window unmonitored. We anchor
            # on order_start (not market.open) because risk-check defers
            # every signal with "Outside order window" before then — a
            # cycle that fires at 09:15 when order_start=09:20 wastes
            # 5 minutes of compute on signals that all get deferred.
            interval: float
            if self._ctx.market_hours.is_market_hours():
                interval = self._ctx.config.heartbeat.market_hours_interval_min * 60
                # Edge case: server was started (or the previous
                # iteration finished) inside the [market.open,
                # order_start) gap — e.g. market opens 09:15 but the
                # user set order_start=09:20 to skip opening
                # volatility. We're in market hours but outside the
                # order window. Shorten the sleep so the next cycle
                # fires AT order_start instead of order_start + 15min.
                if not self._ctx.market_hours.is_order_window():
                    try:
                        open_in = self._ctx.market_hours.seconds_until_next_order_window()
                    except Exception:
                        open_in = float("inf")
                    if open_in > 0 and open_in + 2 < interval:
                        logger.info(
                            "Heartbeat: shortening market-hours sleep from %ds to %ds "
                            "so the next cycle fires at order_start (currently inside "
                            "the market-open / order-start gap)",
                            int(interval), int(open_in + 2),
                        )
                        interval = open_in + 2
            else:
                interval = self._ctx.config.heartbeat.off_hours_interval_min * 60
                try:
                    open_in = self._ctx.market_hours.seconds_until_next_order_window()
                except Exception:
                    open_in = float("inf")
                # +2s of slack so is_order_window() reads True when
                # the next loop iteration runs.
                if open_in > 0 and open_in + 2 < interval:
                    logger.info(
                        "Heartbeat: shortening off-hours sleep from %ds to %ds "
                        "so the first market-hours cycle fires at order_start",
                        int(interval), int(open_in + 2),
                    )
                    interval = open_in + 2

            # Sleep for remaining interval (subtract elapsed time)
            elapsed = time.monotonic() - start
            sleep_time = max(0, interval - elapsed)

            if self._running:
                # Use event wait instead of sleep so stop() can interrupt immediately
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                except TimeoutError:
                    pass  # Normal: timeout means interval elapsed, continue loop

    def stop(self) -> None:
        """Signal the heartbeat loop to stop."""
        self._running = False
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        logger.info("Heartbeat orchestrator stop requested")
