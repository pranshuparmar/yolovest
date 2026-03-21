"""Heartbeat orchestrator for YoloVest.

Implements the heartbeat pipeline with error propagation per FR-1.3,
heartbeat mutex (skip-on-overrun), and consecutive skip alerting.
"""

import asyncio
import logging
from datetime import datetime

from yolovest.context import AppContext
from yolovest.skills import SKILL_REGISTRY
from yolovest.skills.base import SkillBase, SkillResult

logger = logging.getLogger(__name__)


class HeartbeatOrchestrator:
    """Orchestrates the heartbeat pipeline.

    Pipeline execution order (market hours):
        health-check -> ingest-data -> market-scan -> generate-signals
          -> [per signal]: risk-check -> llm-review -> trade-execute -> predict-track
          -> position-monitor

    Error propagation policy (FR-1.3):
        - health-check fails -> ABORT entire heartbeat
        - ingest-data fails -> SKIP scan+signals, run position-monitor
        - market-scan fails -> SKIP signals, run position-monitor
        - generate-signals fails -> SKIP signal chain, run position-monitor
        - Per-signal failures -> skip that signal, continue others

    Heartbeat mutex:
        If a heartbeat is still running when the next fires, skip and log warning.
        After max_consecutive_skips, alert via Telegram as CRITICAL.
    """

    PIPELINE_SKILLS = [
        "health-check",
        "ingest-data",
        "market-scan",
        "generate-signals",
    ]

    SIGNAL_CHAIN_SKILLS = [
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
        if skills is not None:
            self._skills = skills
        else:
            self._skills: dict[str, SkillBase] = {}
            self._init_skills()

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

    async def run_heartbeat(self) -> dict[str, SkillResult]:
        """Execute one heartbeat cycle.

        Returns a dict of skill_name -> SkillResult for all skills that ran.
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
                    f"skipped due to overrun."
                )
            return {"skipped": True, "consecutive_skips": self._consecutive_skips}

        async with self._lock:
            self._consecutive_skips = 0
            return await self._execute_pipeline()

    async def _execute_pipeline(self) -> dict[str, SkillResult]:
        """Execute the full heartbeat pipeline with error propagation."""
        results: dict[str, SkillResult] = {}

        # --- Step 1: health-check (ABORT on failure) ---
        health_result = await self._run_skill("health-check")
        results["health-check"] = health_result

        if not health_result.success:
            logger.error("health-check failed — ABORTING heartbeat")
            await self._ctx.notify.send(
                "ABORT: health-check failed. Heartbeat aborted.\n"
                f"Error: {health_result.error}"
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

        return results

    async def _execute_signal_chain(
        self, signal: object, index: int
    ) -> dict[str, SkillResult]:
        """Execute the per-signal chain: risk-check -> llm-review -> trade-execute -> predict-track.

        Per-signal failures skip that signal only and continue to the next.
        """
        results: dict[str, SkillResult] = {}
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
            decision = llm_result.data.get("decision", "APPROVE")
            if decision == "REJECT":
                logger.info("LLM rejected signal %d", index)
                return results

        # trade-execute
        trade_result = await self._run_skill("trade-execute", signal=signal)
        results[f"{prefix}/trade-execute"] = trade_result
        if not trade_result.success:
            logger.warning("trade-execute failed for signal %d", index)
            await self._ctx.notify.send(
                f"Trade execution failed for signal {index}: {trade_result.error}"
            )
            # Continue to next signal (don't run predict-track for failed trade)
            return results

        # predict-track
        predict_result = await self._run_skill("predict-track", signal=signal)
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
        return result

    async def _alert_position_monitor(self, result: SkillResult) -> None:
        """Alert if position-monitor fails (most dangerous failure)."""
        if not result.success:
            await self._ctx.notify.send(
                "CRITICAL: position-monitor failed. Open positions are unmonitored.\n"
                f"Error: {result.error}"
            )

    async def start(self) -> None:
        """Start the heartbeat loop. Runs until stopped."""
        self._running = True
        logger.info("Heartbeat orchestrator started")

        while self._running:
            start = datetime.now()

            try:
                results = await self.run_heartbeat()
                if results:
                    succeeded = sum(1 for r in results.values() if r.success)
                    logger.info(
                        "Heartbeat completed: %d/%d skills succeeded",
                        succeeded,
                        len(results),
                    )
            except Exception:
                logger.exception("Unhandled error in heartbeat")
                await self._ctx.notify.send("CRITICAL: Unhandled heartbeat error")

            # Determine interval based on market hours
            if self._ctx.market_hours.is_market_hours():
                interval = self._ctx.config.heartbeat.market_hours_interval_min * 60
            else:
                interval = self._ctx.config.heartbeat.off_hours_interval_min * 60

            # Sleep for remaining interval (subtract elapsed time)
            elapsed = (datetime.now() - start).total_seconds()
            sleep_time = max(0, interval - elapsed)

            if self._running:
                await asyncio.sleep(sleep_time)

    def stop(self) -> None:
        """Signal the heartbeat loop to stop."""
        self._running = False
        logger.info("Heartbeat orchestrator stop requested")
