# CEO Phase 0 Strategic Guidance

**Date:** 2026-03-21
**Author:** CEO
**Audience:** SSE (Senior Software Engineer), QA Lead, Tech Lead
**Status:** ACTIVE -- governs all Phase 0 work

---

## 1. Phase 0 Scope: Confirmed Deliverables

Phase 0 is **infrastructure only**. No market data fetching, no ML models, no broker calls, no real trading logic. Everything external is mocked. The goal is a skeleton that Phase 1 can build on without rework.

### 1.1 Exact Deliverables (from REQUIREMENTS.md Section 6, validated against C11-C17)

| # | Deliverable | Source | Notes |
|---|------------|--------|-------|
| D1 | **Pydantic data schemas** (`src/yolovest/models/schemas.py`) | Section 6, C12 | All 9 inter-skill contracts: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`. Plus `PremarketContext` and LLM response types (`WebGroundingResult`, `WatchlistValidation`, `MarketDaySummary`, `FailureAnalysis`). |
| D2 | **Shared context object** (`self.ctx`) | C11 | Must expose: `config`, `db`, `broker`, `llm`, `market_data`, `event_bus`, `state`. Every skill receives this at init. |
| D3 | **Skill registry + orchestrator** | Section 6, C14 | Orchestrator loads skills from `SKILL_REGISTRY`, manages heartbeat pipeline ordering, dispatches events. |
| D4 | **Event bus** | Section 6, C11 | In-process async event bus. Skills emit events (e.g., `signal_generated`), orchestrator routes to subscribers. |
| D5 | **Heartbeat loop** | Section 6 | Implements the full pipeline ordering (health-check -> ingest-data -> market-scan -> generate-signals -> [per signal chain] -> position-monitor). Includes error propagation policy (Section 1, FR-1.3 table) and mutex (skip-on-overrun, max 3 consecutive skips). |
| D6 | **LLM abstraction** (`LLMBase` ABC) | Section 6, C17 | All 6 methods: `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`. Mock implementation that returns plausible defaults. |
| D7 | **Config validation** | Section 6, Section 13.6 | Pydantic validators for all config sections. Include the 6 missing validations identified by PM (weights sum to 1.0, risk percentages in (0,1), time ordering, mode enum, etc.). |
| D8 | **Telegram messaging integration** | Section 6 | Abstracted messaging interface. Mock implementation for Phase 0 (logs messages instead of sending). Must support: trade alerts, error notifications, daily summaries, kill-switch commands. |
| D9 | **Skill stubs: `kill-switch`, `health-check`, `auth-broker`** | Section 6 | These three skills get functional stubs (not just empty classes). `health-check` checks system state. `kill-switch` sets a global halt flag. `auth-broker` manages the token-paste flow (with timeout, per H17). |

### 1.2 Explicitly Out of Scope for Phase 0

- No real broker connections (mock `BrokerBase`)
- No real market data fetching (mock `MarketDataBase`)
- No ML models or feature engineering
- No SQLite schema or migrations (use in-memory or mock DB interface)
- No dashboard or FastAPI
- No real Telegram bot (mock messaging layer)
- **No live or paper trading execution** -- the orchestrator runs the pipeline but skills return mock results

---

## 2. Priority Ordering and Critical Path

### 2.1 Critical Path (strict sequential dependency)

```
[1] Pydantic schemas (D1)
 |
 +--> [2] Config validation (D7)
 |
 +--> [3] Shared context object (D2)
       |
       +--> [4] Event bus (D4)
       |
       +--> [5] LLM abstraction + mock (D6)
       |
       +--> [6] Telegram messaging interface + mock (D8)
             |
             +--> [7] Orchestrator + skill registry (D3)
                   |
                   +--> [8] Heartbeat loop with error propagation (D5)
                         |
                         +--> [9] Skill stubs: health-check, kill-switch, auth-broker (D9)
```

### 2.2 Rationale

1. **Schemas first** because every other component depends on typed data contracts. Without `Signal`, `Trade`, `SkillResult`, etc., nothing can pass data. This also forces us to resolve ambiguities early (e.g., what "total_capital" means per H5).

2. **Config validation second** because the context object needs validated config, and many design decisions flow from config shape (risk parameters, heartbeat intervals, mode flags).

3. **Context object third** because it is the dependency injection backbone. Every skill, the orchestrator, and the event bus all need it. This addresses C11 directly.

4. **Event bus and abstractions (LLM, messaging) can be parallelized** once the context object exists. These are independent of each other.

5. **Orchestrator after abstractions** because it needs to instantiate skills with context and route events.

6. **Heartbeat loop after orchestrator** because it is the orchestrator's main execution mode.

7. **Skill stubs last** because they are consumers of everything above. They validate that the infrastructure works end-to-end.

### 2.3 Suggested Sprint Allocation (2 engineers, 2 weeks)

| Week | Engineer A | Engineer B |
|------|-----------|-----------|
| W1 Days 1-2 | D1: Schemas | D7: Config validation |
| W1 Days 3-4 | D2: Context object | D4: Event bus |
| W1 Day 5 | D6: LLM abstraction + mock | D8: Messaging interface + mock |
| W2 Days 1-3 | D3: Orchestrator + registry | D5: Heartbeat loop + error policy |
| W2 Days 4-5 | D9: Skill stubs (all 3) | Integration tests + QA |

---

## 3. Risk Flags

### 3.1 Technical Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Over-engineering schemas.** Temptation to model every field now, then constant rework as understanding deepens. | HIGH | Define only the fields specified in REQUIREMENTS.md Section 8. Use `model_config = ConfigDict(extra="forbid")` to catch drift early. Add fields only when a skill implementation demands them. |
| **Event bus complexity creep.** Building a full pub/sub system when a simple async callback registry suffices. | HIGH | Phase 0 event bus is in-process only. No persistence, no replay, no ordering guarantees. A dict of `event_name -> list[callback]` with `asyncio.gather` is sufficient. |
| **Heartbeat error propagation ambiguity.** The policy table in FR-1.3 is clear, but edge cases (e.g., partial success in `ingest-data`) are not. | MEDIUM | Define success/failure as binary per skill (`SkillResult.success`). Partial success = success. Log warnings for partial issues but do not abort downstream skills. |
| **Config validation blocking progress.** Trying to validate every config path before any code exists. | MEDIUM | Validate structure and types only. Do not validate business logic constraints (e.g., "square_off must be between order_end and close") until those skills exist. Mark such validators as `# TODO: Phase 1`. |
| **Mock implementations too thin.** Mocks that return empty dicts instead of realistic data make integration tests meaningless. | HIGH | Every mock must return valid Pydantic model instances with plausible default values. `MockLLM.review_trade()` returns `TradeReview(decision="APPROVE", reasoning="Mock approval", adjusted_size=None)`, not `{}`. |

### 3.2 Process Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Scope creep into Phase 1.** Someone starts implementing real broker calls or ML features "while they're in there." | CRITICAL | Hard rule: no external API calls, no real data, no ML code. PR reviews must enforce this. If it calls the internet, it is rejected. |
| **Skipping tests to "move faster."** | CRITICAL | Phase 0 is infrastructure. Untested infrastructure becomes Phase 1's worst enemy. Every deliverable needs unit tests. Target: 90%+ coverage on Phase 0 code. |
| **Ignoring C-findings.** The cross-functional review surfaced 17 critical issues. Phase 0 must address C11, C12, C14, C17 directly. Others (C1-C10, C13, C15, C16) are deferred but must not be made worse. | HIGH | Maintain a "C-finding tracker" in the repo. Each finding gets a status: RESOLVED, DEFERRED-TO-PHASE-N, or WON'T-FIX with justification. |

---

## 4. Strategic Constraints (Non-Negotiable)

### 4.1 Paper Trading Mode Only

- The `mode` field in config defaults to `"paper"`. Phase 0 code must not contain any path that places a real order.
- The `BrokerBase` mock must raise `NotImplementedError("Live trading not available in Phase 0")` if `mode == "live"` is somehow set.
- Config validation must reject `mode: live` during Phase 0.

### 4.2 Mock Everything External

- `BrokerBase` -> `MockBroker`: simulates order placement, returns fake order IDs, tracks positions in memory.
- `MarketDataBase` -> `MockMarketData`: returns hardcoded or fixture-based OHLCV data.
- `LLMBase` -> `MockLLM`: returns deterministic, plausible responses for all 6 methods.
- `TelegramMessenger` -> `MockMessenger`: logs messages to stdout/file instead of sending.
- No network calls. Tests must run offline and complete in under 30 seconds total.

### 4.3 Testable Infrastructure

- Every component must be independently testable with injected mocks.
- The context object is the DI container. No global state, no singletons, no module-level side effects.
- All async code must be testable with `pytest-asyncio`.
- Heartbeat loop must be testable without real timers (inject a mock clock or run single iterations).

### 4.4 All Risk Parameters Configurable

- No hardcoded magic numbers for risk thresholds, position sizes, capital limits, or time windows.
- Every risk parameter lives in `config.yaml` under the `risk.*` namespace.
- Config model must have sensible defaults that match REQUIREMENTS.md Section 10.
- Changing a risk parameter must require only a config file edit, not a code change.

### 4.5 Async Throughout

- All skill `execute()` methods are `async`.
- The orchestrator and heartbeat loop are `async`.
- The event bus dispatches callbacks with `asyncio`.
- No blocking I/O anywhere in the skill pipeline.

---

## 5. Acceptance Criteria for Phase 0 Completion

Phase 0 is complete when ALL of the following are true:

1. **Schemas compile and validate.** All 9+ Pydantic models from Section 8 are implemented. Round-trip serialization tests pass (`model.model_dump()` -> `Model(**data)` -> identical).

2. **Config loads and validates.** A sample `config.yaml` loads through Pydantic validators without error. Invalid configs (bad types, missing required fields, weights not summing to 1.0) raise clear `ValidationError` messages.

3. **Context object is injectable.** A `Context` can be constructed with all mock components and passed to any skill's `__init__`.

4. **Orchestrator runs a full heartbeat.** A single heartbeat iteration executes the full pipeline with mock skills and returns without error. Skill ordering matches the specification.

5. **Error propagation works.** When `health-check` mock returns `success=False`, the heartbeat aborts. When `ingest-data` fails, `market-scan` and `generate-signals` are skipped but `position-monitor` runs. All 7 error policies from FR-1.3 are tested.

6. **Heartbeat mutex works.** A slow heartbeat (simulated) causes the next interval to be skipped. After 3 consecutive skips, a critical alert is emitted.

7. **Event bus dispatches.** An event emitted by one skill is received by subscribed handlers. Unhandled events are logged, not silently dropped.

8. **LLM abstraction has all 6 methods.** `MockLLM` implements all 6 and returns valid Pydantic response models.

9. **Kill-switch halts the pipeline.** Setting the kill flag stops new heartbeats. Existing in-progress heartbeat completes its current skill then stops.

10. **All tests pass offline.** `pytest` runs with zero network calls, zero external dependencies, in under 30 seconds.

---

## 6. What NOT to Do

1. **Do not build SQLite schema.** Database design is Phase 1 (per C13, the schema needs careful thought). Phase 0 uses a mock DB interface that stores data in memory.

2. **Do not implement real skill logic.** Skills are stubs. `IngestDataSkill.execute()` returns `SkillResult(success=True, data={"bars_ingested": 0})`. It does not fetch data.

3. **Do not optimize for performance.** No caching layers, no connection pools, no async batching. Get correctness first.

4. **Do not add dependencies beyond what is needed.** Phase 0 needs: `pydantic`, `pyyaml`, `pytest`, `pytest-asyncio`. That is likely the complete list. No `aiohttp`, no `sqlalchemy`, no ML libraries.

5. **Do not resolve findings C1-C10, C13, C15, C16.** These are real issues but they belong to later phases. Acknowledge them in code comments where relevant (e.g., `# TODO C3: gap risk management - Phase 3`) but do not implement.

6. **Do not build the Telegram bot.** Build the messaging *interface* (abstract class + mock). The real Telegram integration comes later.

---

## 7. Resolved Cross-Functional Findings (Phase 0 Scope)

| Finding | Resolution in Phase 0 |
|---------|----------------------|
| **C11** (No context object) | Build it. This is D2. |
| **C12** (No typed schemas) | Build them. This is D1. |
| **C14** (Phase 5 must become Phase 0) | Done. Phase 0 now includes orchestrator, context, event bus, heartbeat. |
| **C17** (LLMBase only has 2 methods, needs 6) | Build the full 6-method ABC. This is D6. |
| **C7** (kill-switch has no phase) | Included as D9 stub. Full implementation in Phase 3. |
| **C10** (Config conflict: two paths control LLM review) | Resolve during D7. Use single config path `risk.llm_review_enabled`. Remove `llm.review_every_trade`. |
| **H17** (auth-broker waiting state has no timeout) | Address in D9 stub. Add `auth.token_wait_timeout_sec` config with default 300. `SkillResult` stays as-is; use `data.status = "awaiting_token"` pattern. |
| **Section 13.6** (6 missing config validations) | All 6 included in D7 scope. |

---

## 8. Success Metrics

| Metric | Target |
|--------|--------|
| Unit test count | 40+ tests across all Phase 0 deliverables |
| Test coverage (Phase 0 code) | 90%+ line coverage |
| Schema count | 13+ Pydantic models (9 core + 4 LLM response types) |
| Config validations | All fields typed, all 6 PM-identified validations passing |
| Time to run full test suite | Under 30 seconds, fully offline |
| Heartbeat error policy tests | 7 (one per row in FR-1.3 error propagation table) |
| External network calls | Zero |

---

## 9. Handoff to Phase 1

When Phase 0 is complete, Phase 1 inherits:

- A working orchestrator that can run heartbeats with real skills swapped in one at a time.
- Typed data contracts that enforce correctness at skill boundaries.
- A config system that validates all parameters before the system starts.
- Mock implementations that serve as reference for real implementations.
- A test suite that catches regressions as real components replace mocks.

Phase 1 begins with: project scaffold hardening, SQLite schema design (C13), broker abstraction + Zerodha implementation, real LLM (Gemini) implementation, and market data providers.

---

*This document is the governing directive for Phase 0. All architectural decisions, PR reviews, and sprint planning must align with the constraints and priorities defined here. Questions or proposed deviations should be escalated to the CEO.*
