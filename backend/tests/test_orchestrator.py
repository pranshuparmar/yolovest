"""Tests for HeartbeatOrchestrator in orchestrator.py.

Tests the heartbeat pipeline happy path, error propagation policy,
heartbeat mutex, and consecutive skip tracking.

The orchestrator instantiates skills from SKILL_REGISTRY internally,
so we mock the registry skills via monkeypatch.
"""

import asyncio
from typing import Any

import pytest

from yolovest.context import AppContext, MarketHoursChecker
from yolovest.events import EventBus
from yolovest.orchestrator import HeartbeatOrchestrator
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger


class StubSkill(SkillBase):
    """A configurable stub skill for testing."""

    name = "stub"
    description = "Test stub"
    trigger = SkillTrigger.HEARTBEAT

    def __init__(self, context: Any, *, succeed: bool = True, data: dict | None = None) -> None:
        super().__init__(context)
        self._succeed = succeed
        self._data = data or {}

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        return SkillResult(
            success=self._succeed,
            skill_name=self.name,
            data=self._data,
        )


def _make_stub(ctx: Any, name: str, succeed: bool = True, data: dict | None = None) -> StubSkill:
    skill = StubSkill(ctx, succeed=succeed, data=data)
    skill.name = name
    return skill


@pytest.fixture
def orchestrator_context(
    sample_config, mock_broker, mock_llm, mock_db, mock_market_data, mock_notify,
):
    market_hours = MarketHoursChecker(sample_config)
    return AppContext(
        config=sample_config,
        db=mock_db,
        broker=mock_broker,
        llm=mock_llm,
        market_data=mock_market_data,
        notify=mock_notify,
        market_hours=market_hours,
        event_bus=EventBus(),
    )


def _build_orchestrator(
    ctx: AppContext, overrides: dict[str, StubSkill] | None = None,
) -> HeartbeatOrchestrator:
    """Build an orchestrator and replace its internal skills with stubs."""
    orch = HeartbeatOrchestrator(ctx)

    # Replace all skills with stubs
    pipeline_names = [
        "health-check", "ingest-data", "market-scan", "generate-signals",
        "risk-check", "llm-review", "trade-execute", "predict-track",
        "position-monitor",
    ]
    for name in pipeline_names:
        data = {"signals": []} if name == "generate-signals" else {}
        if name == "risk-check":
            data = {"approved": True, "signal": {}}
        orch._skills[name] = _make_stub(ctx, name, succeed=True, data=data)

    if overrides:
        orch._skills.update(overrides)

    return orch


class TestHappyPath:
    async def test_all_skills_succeed(self, orchestrator_context):
        ctx = orchestrator_context
        orch = _build_orchestrator(ctx)
        result = await orch.run_heartbeat()

        assert "health-check" in result
        assert result["health-check"].success is True
        assert "ingest-data" in result
        assert "market-scan" in result
        assert "generate-signals" in result
        assert "position-monitor" in result

    async def test_happy_path_with_signals(self, orchestrator_context):
        ctx = orchestrator_context
        gen_signals = _make_stub(
            ctx, "generate-signals", succeed=True,
            data={"signals": [{"symbol": "RELIANCE"}, {"symbol": "TCS"}]},
        )
        risk = _make_stub(ctx, "risk-check", succeed=True, data={"approved": True, "signal": {}})
        llm = _make_stub(ctx, "llm-review", succeed=True, data={"decision": "APPROVE"})
        trade = _make_stub(ctx, "trade-execute", succeed=True, data={})
        predict = _make_stub(ctx, "predict-track", succeed=True, data={})

        orch = _build_orchestrator(ctx, {
            "generate-signals": gen_signals,
            "risk-check": risk,
            "llm-review": llm,
            "trade-execute": trade,
            "predict-track": predict,
        })
        result = await orch.run_heartbeat()

        # Per-signal results are keyed as "signal-{i}/skill-name"
        assert "signal-0/risk-check" in result
        assert "signal-1/risk-check" in result
        assert "signal-0/trade-execute" in result
        assert "signal-1/trade-execute" in result


class TestHealthCheckFails:
    async def test_health_check_fails_aborts_heartbeat(self, orchestrator_context):
        ctx = orchestrator_context
        health = _make_stub(ctx, "health-check", succeed=False)
        orch = _build_orchestrator(ctx, {"health-check": health})
        result = await orch.run_heartbeat()

        assert result["health-check"].success is False
        # Other skills should NOT be in results
        assert "ingest-data" not in result
        assert "market-scan" not in result
        assert "generate-signals" not in result

    async def test_health_check_fails_sends_notification(self, orchestrator_context):
        ctx = orchestrator_context
        health = _make_stub(ctx, "health-check", succeed=False)
        orch = _build_orchestrator(ctx, {"health-check": health})
        await orch.run_heartbeat()

        ctx.notify.send.assert_called()


class TestIngestDataFails:
    async def test_ingest_fails_skips_scan_and_signals(self, orchestrator_context):
        ctx = orchestrator_context
        ingest = _make_stub(ctx, "ingest-data", succeed=False)
        orch = _build_orchestrator(ctx, {"ingest-data": ingest})
        result = await orch.run_heartbeat()

        assert result["ingest-data"].success is False
        assert "market-scan" not in result
        assert "generate-signals" not in result

    async def test_ingest_fails_still_runs_position_monitor(self, orchestrator_context):
        ctx = orchestrator_context
        ingest = _make_stub(ctx, "ingest-data", succeed=False)
        orch = _build_orchestrator(ctx, {"ingest-data": ingest})
        result = await orch.run_heartbeat()

        assert "position-monitor" in result
        assert result["position-monitor"].success is True


class TestMarketScanFails:
    async def test_market_scan_fails_skips_signals(self, orchestrator_context):
        ctx = orchestrator_context
        scan = _make_stub(ctx, "market-scan", succeed=False)
        orch = _build_orchestrator(ctx, {"market-scan": scan})
        result = await orch.run_heartbeat()

        assert result["market-scan"].success is False
        assert "generate-signals" not in result

    async def test_market_scan_fails_runs_position_monitor(self, orchestrator_context):
        ctx = orchestrator_context
        scan = _make_stub(ctx, "market-scan", succeed=False)
        orch = _build_orchestrator(ctx, {"market-scan": scan})
        result = await orch.run_heartbeat()

        assert "position-monitor" in result
        assert result["position-monitor"].success is True


class TestPerSignalRiskCheckFails:
    async def test_risk_check_fails_skips_signal_continues_others(self, orchestrator_context):
        ctx = orchestrator_context
        gen = _make_stub(
            ctx, "generate-signals", succeed=True,
            data={"signals": [{"symbol": "BAD"}, {"symbol": "GOOD"}]},
        )

        class ConditionalRiskStub(StubSkill):
            async def execute(self, **kwargs):
                signal = kwargs.get("signal", {})
                symbol = signal.get("symbol", "") if isinstance(signal, dict) else ""
                if symbol == "BAD":
                    return SkillResult(
                        success=False, skill_name="risk-check",
                        data={"approved": False, "rejection_reason": "test rejection"},
                    )
                return SkillResult(
                    success=True, skill_name="risk-check",
                    data={"approved": True, "signal": signal},
                )

        risk = ConditionalRiskStub(ctx, succeed=True)
        risk.name = "risk-check"

        orch = _build_orchestrator(ctx, {"generate-signals": gen, "risk-check": risk})
        result = await orch.run_heartbeat()

        # First signal's risk-check failed -> no trade-execute for signal 0
        assert result["signal-0/risk-check"].success is False
        assert "signal-0/trade-execute" not in result
        # Second signal should proceed through
        assert result["signal-1/risk-check"].success is True
        assert "signal-1/trade-execute" in result


class TestGenerateSignalsFails:
    async def test_generate_signals_fails_skips_signal_chain(self, orchestrator_context):
        ctx = orchestrator_context
        gen = _make_stub(ctx, "generate-signals", succeed=False)
        orch = _build_orchestrator(ctx, {"generate-signals": gen})
        result = await orch.run_heartbeat()

        assert result["generate-signals"].success is False
        assert "signal_pipeline" not in result
        assert "position-monitor" in result

    async def test_generate_signals_fails_runs_position_monitor(self, orchestrator_context):
        ctx = orchestrator_context
        gen = _make_stub(ctx, "generate-signals", succeed=False)
        orch = _build_orchestrator(ctx, {"generate-signals": gen})
        result = await orch.run_heartbeat()

        assert "position-monitor" in result
        assert result["position-monitor"].success is True


class TestLLMReviewFallback:
    async def test_llm_review_fails_with_fallback_continues(self, orchestrator_context):
        """If LLM is down and llm_fallback_to_rules=True, auto-approve."""
        ctx = orchestrator_context
        ctx.config.risk.llm_fallback_to_rules = True

        gen = _make_stub(
            ctx, "generate-signals", succeed=True,
            data={"signals": [{"symbol": "RELIANCE"}]},
        )
        llm = _make_stub(ctx, "llm-review", succeed=False)

        orch = _build_orchestrator(ctx, {"generate-signals": gen, "llm-review": llm})
        result = await orch.run_heartbeat()

        # LLM failed but fallback to rules means trade-execute should still run
        assert "signal-0/llm-review" in result
        assert result["signal-0/llm-review"].success is False
        assert "signal-0/trade-execute" in result

    async def test_llm_review_fails_without_fallback_skips(self, orchestrator_context):
        """If LLM is down and llm_fallback_to_rules=False, skip signal."""
        ctx = orchestrator_context
        ctx.config.risk.llm_fallback_to_rules = False

        gen = _make_stub(
            ctx, "generate-signals", succeed=True,
            data={"signals": [{"symbol": "RELIANCE"}]},
        )
        llm = _make_stub(ctx, "llm-review", succeed=False)

        orch = _build_orchestrator(ctx, {"generate-signals": gen, "llm-review": llm})
        result = await orch.run_heartbeat()

        # LLM failed and no fallback means signal should be skipped
        assert "signal-0/llm-review" in result
        assert result["signal-0/llm-review"].success is False
        assert "signal-0/trade-execute" not in result


class TestHeartbeatMutex:
    async def test_concurrent_heartbeat_is_skipped(self, orchestrator_context):
        ctx = orchestrator_context

        class SlowSkill(StubSkill):
            async def execute(self, **kwargs):
                await asyncio.sleep(0.2)
                return SkillResult(success=True, skill_name=self.name, data={})

        slow_health = SlowSkill(ctx)
        slow_health.name = "health-check"
        orch = _build_orchestrator(ctx, {"health-check": slow_health})

        # Start two heartbeats concurrently
        results = await asyncio.gather(
            orch.run_heartbeat(),
            orch.run_heartbeat(),
        )

        # One should have run, one should be skipped
        skipped = [r for r in results if r.get("skipped")]
        ran = [r for r in results if not r.get("skipped")]
        assert len(skipped) == 1
        assert len(ran) == 1

    async def test_consecutive_skip_tracking(self, orchestrator_context):
        ctx = orchestrator_context
        orch = _build_orchestrator(ctx)

        # Manually acquire lock to simulate a running heartbeat
        await orch._lock.acquire()
        try:
            r1 = await orch.run_heartbeat()
            assert r1.get("skipped") is True
            assert orch._consecutive_skips == 1

            r2 = await orch.run_heartbeat()
            assert r2.get("skipped") is True
            assert orch._consecutive_skips == 2

            r3 = await orch.run_heartbeat()
            assert r3.get("skipped") is True
            assert orch._consecutive_skips == 3
        finally:
            orch._lock.release()

    async def test_consecutive_skips_trigger_critical_alert(self, orchestrator_context):
        ctx = orchestrator_context
        orch = _build_orchestrator(ctx)

        await orch._lock.acquire()
        try:
            for _ in range(3):
                await orch.run_heartbeat()

            ctx.notify.send.assert_called()
            call_args = ctx.notify.send.call_args[0][0]
            assert "CRITICAL" in call_args
        finally:
            orch._lock.release()

    async def test_successful_heartbeat_resets_skip_counter(self, orchestrator_context):
        ctx = orchestrator_context
        orch = _build_orchestrator(ctx)

        # Simulate some skips
        await orch._lock.acquire()
        await orch.run_heartbeat()
        assert orch._consecutive_skips == 1
        orch._lock.release()

        # Run a successful heartbeat
        await orch.run_heartbeat()
        assert orch._consecutive_skips == 0
