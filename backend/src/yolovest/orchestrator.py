"""Heartbeat orchestrator for YoloVest.

Implements the heartbeat pipeline with error propagation,
heartbeat mutex (skip-on-overrun), and consecutive skip alerting.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any, ClassVar

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

    async def run_heartbeat(self) -> dict[str, Any]:
        """Execute one heartbeat cycle.

        Returns a dict with skill results and metadata. SkillResult values are
        keyed by skill name; metadata keys include 'skipped', 'aborted',
        'signal_pipeline', 'consecutive_skips', 'symbol'.
        """
        # Mutex: skip if already running
        if self._lock.locked():
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
            return {"skipped": True, "consecutive_skips": self._consecutive_skips}

        async with self._lock:
            self._consecutive_skips = 0
            return await self._execute_pipeline()

    async def _execute_pipeline(self) -> dict[str, Any]:
        """Execute the full heartbeat pipeline with error propagation."""
        results: dict[str, Any] = {}
        await self._broadcast("heartbeat_started", {
            "market_hours": self._ctx.market_hours.is_market_hours(),
        })

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
        signals = signals_result.data.get("signals", [])
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
        from yolovest.timezone import UTC, now_ist
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
            logger.info("risk-check failed for signal %d — skipping", index)
            return results

        # Check risk approval and use adjusted signal
        if risk_result.data and not risk_result.data.get("approved", True):
            logger.info(
                "risk-check rejected signal %d: %s",
                index, risk_result.data.get("rejection_reason"),
            )
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
                return results

        # Check LLM decision (if it succeeded)
        if llm_result.success:
            approved = llm_result.data.get("approved", True)
            if not approved:
                logger.info("LLM rejected signal %d", index)
                return results
            # Use updated signal from LLM (may have been resized)
            if llm_result.data.get("signal"):
                signal = llm_result.data["signal"]

        # Manual approval mode — queue instead of executing
        if self._ctx.config.execution.transaction_mode == "manual":
            pending_id = await self._ctx.db.insert_pending_trade(signal)
            symbol = signal.get("symbol", "?") if isinstance(signal, dict) else "?"
            sig_type = signal.get("signal_type", "?") if isinstance(signal, dict) else "?"
            conf = signal.get("confidence_score", 0) if isinstance(signal, dict) else 0
            entry = signal.get("entry_price", 0) if isinstance(signal, dict) else 0
            logger.info(
                "Manual mode: queued %s %s @ %.2f conf=%.0f%% (pending_id=%d)",
                sig_type, symbol, entry, conf * 100, pending_id,
            )
            await self._ctx.notify.send(
                f"Pending approval: {sig_type} {symbol} @ ₹{entry:.2f} "
                f"(conf {conf:.0%})\n"
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
            # Remove signal from DB so it's not blocked by already_signaled dedup
            # and can be regenerated on the next heartbeat
            try:
                await self._ctx.db.conn.execute(
                    "DELETE FROM signals WHERE symbol = ? AND created_at >= ?",
                    (symbol, self._today_start()),
                )
                await self._ctx.db.conn.commit()
                logger.info("Removed failed signal for %s so it can be retried next heartbeat", symbol)
            except Exception:
                logger.debug("Failed to remove signal for %s", symbol, exc_info=True)
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
        result = await skill.safe_execute(**kwargs)
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

            # Determine interval based on market hours
            if self._ctx.market_hours.is_market_hours():
                interval = self._ctx.config.heartbeat.market_hours_interval_min * 60
            else:
                interval = self._ctx.config.heartbeat.off_hours_interval_min * 60

            # Sleep for remaining interval (subtract elapsed time)
            elapsed = time.monotonic() - start
            sleep_time = max(0, interval - elapsed)

            if self._running:
                # Use event wait instead of sleep so stop() can interrupt immediately
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                except asyncio.TimeoutError:
                    pass  # Normal: timeout means interval elapsed, continue loop

    def stop(self) -> None:
        """Signal the heartbeat loop to stop."""
        self._running = False
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        logger.info("Heartbeat orchestrator stop requested")
