# PM Review: Phase 1 Implementation Plan

**Reviewer:** Product Manager
**Date:** 2026-03-22
**Document reviewed:** `plan.md` (Phase 1: Foundation & Data Pipeline)
**Source of truth:** `REQUIREMENTS.md` v1.0, existing Phase 0 codebase

---

## Executive Summary

The plan covers the core data pipeline deliverables for Phase 1 (database, market data providers, feature engineering) and is well-structured with clear implementation order. However, it omits two items that REQUIREMENTS.md Section 6 explicitly assigns to Phase 1 — the Broker abstraction (Zerodha implementation) and the LLM abstraction (Gemini implementation) — and it includes database tables and sentiment logic that belong to later phases. Several cross-functional findings (C13, H3, H14) are addressed, but the plan does not account for H10 (NSE universe scan feasibility), H19 (news scraping fragility), or H20 (FR-2.7/FR-2.3 priority mismatch), leaving Phase 2 with unresolved blockers.

---

## Requirement Coverage Matrix

### Phase 1 Scope per REQUIREMENTS.md Section 6

| Item from Section 6 | Plan Status | Notes |
|---|---|---|
| Database schema design + migration setup (FR-10.1) | COVERED | Step 1 — comprehensive schema |
| Broker abstraction + Zerodha implementation | MISSING | Section 6 says "Broker abstraction + Zerodha implementation" is Phase 1. Plan omits it entirely. |
| LLM abstraction + Gemini implementation | MISSING | Section 6 says "LLM abstraction + Gemini implementation" is Phase 1. Plan omits it entirely. |
| Market data abstraction + free providers | COVERED | Steps 2a-2d |
| Feature engineering (technical indicators) | COVERED | Step 3 |
| Project scaffold, config system | PARTIALLY COVERED | Config change is minor (database.path). Scaffold already done in Phase 0. |

### Functional Requirements Relevant to Phase 1

| FR | Description | Priority | Status | Notes |
|---|---|---|---|---|
| FR-2.1 | MarketDataBase ABC + fallback chain | P0 | COVERED | ABC exists from Phase 0; plan adds concrete providers + ingester |
| FR-2.1a | jugaad-data provider (daily/EOD) | P0 | COVERED | Step 2b |
| FR-2.1b | yfinance fallback | P0 | COVERED | Step 2c |
| FR-2.1c | tvDatafeed intraday | P1 | COVERED | Step 2d |
| FR-2.1d | Bhavcopy seed data import | P1 | MISSING | Not mentioned in plan. Needed for backtesting (Phase 2). |
| FR-2.2 | NSE/BSE official data ingestion | P0 | MISSING | No mention of corporate announcements, bulk/block deals, FII/DII |
| FR-2.8 | Store all ingested data in SQLite | P0 | COVERED | Database schema includes ohlcv, watchlist, sentiment, etc. |
| FR-2.9 | Rate-limit all data fetching | P0 | PARTIALLY COVERED | Plan mentions "built into jugaad-data" but no explicit rate limiter for yfinance/tvDatafeed |
| FR-4.1 | Feature engineering (indicators) | P0 | COVERED | Step 3 |
| FR-10.1 | SQLite schema versioned via migrations | P0 | PARTIALLY COVERED | `schema_version` table exists but no migration runner/scripts described |
| FR-10.4 | Data staleness validation | P0 | COVERED | Mentioned in ingester design |

### Non-Functional Requirements

| NFR | Description | Status | Notes |
|---|---|---|---|
| NFR-3 | Data integrity — SQLite WAL mode | COVERED | Explicitly mentioned |
| NFR-8 | Testability — every component testable | PARTIALLY COVERED | Tests planned for DB, ingester, features, providers — but no integration tests |

---

## Issues Found

### BLOCKER

**1. Broker abstraction + Zerodha implementation is missing (Section 6 Phase 1 scope)**

REQUIREMENTS.md Section 6 explicitly lists "Broker abstraction + Zerodha implementation" under Phase 1. The plan omits this entirely. Phase 0 only has `broker/base.py` (ABC) and a `_StubBroker` in `main.py`. Without a concrete Zerodha broker, Phase 3 (Risk & Execution) cannot proceed, and integration testing of the heartbeat pipeline is impossible. The `BrokerBase` ABC already exists — Phase 1 needs to deliver `broker/zerodha.py`.

**2. LLM abstraction + Gemini implementation is missing (Section 6 Phase 1 scope)**

Same issue. Section 6 lists "LLM abstraction + Gemini implementation" under Phase 1. Phase 0 built `llm/base.py` (ABC with all 6 methods) and `_StubLLM` in `main.py`. Phase 1 must deliver `llm/gemini.py` — the concrete Gemini implementation. Without it, Phase 2 (Intelligence Layer) cannot do sentiment analysis, watchlist validation, or web grounding. This is the longest-lead-time item in the plan and its absence is a delivery risk.

### HIGH

**3. Scope creep — sentiment, premarket, llm_reviews tables belong to Phase 2+**

The plan's database schema (Step 1) includes tables for `sentiment`, `premarket`, and `llm_reviews`. These are tied to FR-2.7 (sentiment analysis, Phase 2), FR-2.10-2.11 (premarket, explicitly deferred by the plan itself), and FR-5.11 (LLM review, Phase 3). Creating empty tables is low-risk, but the plan also defines DB methods like `upsert_sentiment()`, `upsert_premarket()`, `get_latest_premarket()` — implementing and testing these adds scope without Phase 1 consumers.

**Recommendation:** Define the tables in the migration (forward-compatible) but defer the corresponding DB methods to the phase that needs them.

**4. No migration runner or versioning mechanism**

The plan has a `schema_version` table but describes no migration runner, no migration file structure, and no upgrade/downgrade logic. FR-10.1 requires "schema versioned via migration scripts." A single `initialize()` method that creates all tables is not a migration system — it cannot handle schema changes between releases.

**5. FR-2.2 (NSE/BSE official data) is P0 but not addressed**

FR-2.2 requires ingesting corporate announcements, bulk/block deals, FII/DII activity, and delivery data. The plan does not mention this anywhere. The `ingest-data` skill stub already references `_fetch_nse_data()` (NotImplementedError). This is a P0 requirement that feeds into market-scan scoring (fundamental component).

**6. Rate limiting strategy is incomplete**

FR-2.9 (P0) requires rate-limiting all data fetching. The plan notes jugaad-data has built-in caching but provides no rate-limiting strategy for yfinance (known fragile rate limits per REQUIREMENTS.md Section 11) or tvDatafeed. A shared rate limiter or per-provider throttle is needed to avoid IP bans.

**7. `DatabaseProtocol` expansion creates tight coupling**

Step 4 says "Expand DatabaseProtocol in context.py to include all DB methods needed by skills." This risks bloating the protocol with methods only some skills use (e.g., `upsert_sentiment` is only used by `ingest-data`). Consider keeping the protocol minimal (health_check, kill_switch, positions) and having skills access the Database class directly where needed.

### MEDIUM

**8. No audit log table**

NFR-5 and FR-8.8 require an audit log for "every decision point." The BA proposed acceptance criteria (Section 13.5) specify fields: `timestamp_ist, action_type, skill_name, input_summary, output_summary, duration_ms`. The plan's schema has no `audit_log` table. This is needed early because retrofitting audit logging after multiple phases is painful.

**9. Bhavcopy seed data importer missing (FR-2.1d, P1)**

FR-2.1d specifies a one-time NSE Bhavcopy CSV import for deep historical backtesting data (2013+). The project structure in REQUIREMENTS.md Section 9 shows `data/bhavcopy.py`. This is needed before Phase 2's backtesting engine. The plan should include at least a basic CSV importer.

**10. Feature engineering has no caching or incremental computation**

Step 3 describes pure functions that take `list[OHLCVBar]` and return features. With 25 watchlist stocks computed every 15-minute heartbeat, recalculating 200-period EMAs from scratch each time is wasteful. Consider caching intermediate results or computing incrementally.

**11. Tests do not cover schema migration scenarios**

Step 5 lists `test_db.py` covering "CRUD for all tables, upsert idempotency" but no tests for schema migrations (upgrade from v1 to v2, handling existing data). Given FR-10.1's emphasis on versioned migrations, this is a gap.

**12. pyproject.toml `requires-python` fix is good but incomplete**

The Python version has been standardized to `>=3.11` across all config, tooling, Dockerfile, and documentation (Q2 from TL review — resolved). The new dependencies (`jugaad-data`, `yfinance`, `tvdatafeed`, `ta`) should be pinned with version ranges for reproducibility.

### LOW

**13. File naming inconsistency: `yfinance_provider.py` vs REQUIREMENTS.md's `yfinance.py`**

REQUIREMENTS.md Section 9 project structure shows `data/yfinance.py`. The plan names it `data/yfinance_provider.py`. Minor inconsistency that could confuse developers referencing the requirements. The plan's name is arguably better (avoids shadowing the `yfinance` package), but should be a documented decision.

**14. No `data/__init__.py` exports defined**

The plan creates 6 new files under `data/` but does not mention updating `data/__init__.py` with public exports. This matters for import ergonomics.

---

## Cross-Functional Finding Coverage

| Finding | Description | Addressed? | Notes |
|---|---|---|---|
| **C13** | No SQLite schema design | YES | Comprehensive schema in Step 1 |
| **H3** | No staleness check for market data | YES | `stale_threshold_minutes` validation in ingester |
| **H10** | FR-3.1 scans ~2000 NSE stocks — unrealistic on 15min heartbeat | NO | Plan builds market data providers but does not address the feasibility of scanning the full NSE universe with rate-limited free APIs. Phase 2's market-scan will inherit this problem. Plan should document the pre-filter strategy (e.g., start with Nifty 500, not full NSE). |
| **H14** | FR-2.8 "store all data" — no schema clarity | PARTIALLY | Schema defines what is stored (OHLCV bars, watchlist scores, sentiment) but does not clarify what is NOT stored (e.g., raw HTML from scrapers, parsed article text). Should document storage scope. |
| **H19** | News scraping targets actively block scraping | NO | Plan defers news to Phase 2 (correct), but does not flag that FR-2.3 targets (MoneyControl, ET, LiveMint) are known to block scrapers. Phase 2 planning should explore API alternatives or RSS feeds. |
| **H20** | FR-2.7 (P0 sentiment) depends on FR-2.3 (news scraping) | NO | Plan defers both to Phase 2, which resolves the immediate priority conflict. But the dependency remains — Phase 2 plan must sequence news fetching before sentiment. |

---

## Dependencies Check

| Phase 1 assumes... | Status |
|---|---|
| `MarketDataBase` ABC exists | OK — `data/base.py` exists with `get_ohlcv`, `get_quote`, `health_check` |
| `OHLCVBar` schema exists | OK — defined in `models/schemas.py` |
| `AppContext` and protocols exist | OK — `context.py` has all protocols |
| `EventBus` exists | OK — `events.py` exists |
| Orchestrator and heartbeat loop exist | OK — `orchestrator.py` exists |
| `DatabaseProtocol` can be expanded | OK — currently minimal (3 methods) |

| Later phases assume Phase 1 provides... | Covered? |
|---|---|
| Working database with OHLCV storage | YES |
| Working market data providers with fallback | YES |
| Feature engineering functions | YES |
| Concrete Zerodha broker | NO — Phase 3 needs this |
| Concrete Gemini LLM | NO — Phase 2 needs this |
| Migration system for schema updates | PARTIAL — no runner |
| Bhavcopy historical data import | NO — Phase 2 backtesting needs this |
| Audit log infrastructure | NO — Phase 5 needs this, easier to add early |

---

## Recommendations

1. **Add Zerodha broker implementation (`broker/zerodha.py`) to the plan.** This is explicitly Phase 1 scope per Section 6. Even a minimal implementation (auth, place_order, get_positions) unblocks Phase 3 and enables end-to-end paper trading integration tests.

2. **Add Gemini LLM implementation (`llm/gemini.py`) to the plan.** Also explicitly Phase 1 scope. Without it, Phase 2 has no LLM to run sentiment analysis or watchlist validation. Start with `review_trade` and `analyze_sentiment` — the two most critical methods.

3. **Build a real migration runner, not just `CREATE TABLE IF NOT EXISTS`.** Even a simple sequential runner (read `migrations/001_initial.sql`, `002_add_audit.sql`, etc., apply in order, track in `schema_version`) satisfies FR-10.1. Without this, every schema change in Phases 2-5 requires manual intervention.

4. **Add an `audit_log` table to the initial schema.** NFR-5 and FR-8.8 require comprehensive audit logging. Adding the table now and starting to log in Phase 1 skills avoids a painful retrofit later.

5. **Document a pre-filter strategy for NSE universe scanning (H10).** Even though market-scan is Phase 2, the data providers built in Phase 1 need to know the expected data volume. Recommend starting with Nifty 500 (not full NSE ~2000) and documenting this as a config option.

6. **Add explicit rate limiters for yfinance and tvDatafeed.** A simple `asyncio.Semaphore` or token-bucket per provider prevents IP bans during development and testing.

7. **Trim Phase 2+ DB methods from Step 1 scope.** Keep tables in the schema (forward-compatible) but defer `upsert_sentiment()`, `upsert_premarket()`, and `get_latest_premarket()` implementations until their consuming skills are built.

---

## Final Verdict

### CONDITIONAL APPROVE

The plan is solid for the data pipeline core (database, providers, feature engineering) and demonstrates good understanding of the requirements. However, it omits two items that REQUIREMENTS.md explicitly assigns to Phase 1 — the Zerodha broker and Gemini LLM concrete implementations. These are not optional additions; they are listed in Section 6 Phase 1 scope and are critical-path dependencies for Phases 2 and 3.

**Conditions for approval:**

1. Add `broker/zerodha.py` (at minimum: auth flow, place_order, get_positions, get_margins) to the plan
2. Add `llm/gemini.py` (at minimum: ping, review_trade, analyze_sentiment) to the plan
3. Add a migration runner mechanism (does not need to be complex — sequential SQL files suffice)
4. Add `audit_log` table to the initial schema

Once these four items are incorporated, the plan covers Phase 1 requirements and provides the foundation later phases need.
