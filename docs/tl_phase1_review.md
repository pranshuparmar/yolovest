# TL Review — Phase 1: Foundation & Data Pipeline

**Reviewer:** Tech Lead
**Date:** 2026-03-22
**Files reviewed:** 14 source files, 5 test files, 1 migration, pyproject.toml
**Tests:** 89 Phase 1 tests, all passing

## Summary

**Status: APPROVED WITH NOTES**

Phase 1 delivers a solid foundation. The database layer, market data providers with fallback chain, feature engineering, Zerodha broker (paper + live), and Gemini LLM are all implemented correctly and match the plan. Code quality is good: clean async patterns, proper use of `asyncio.to_thread` for blocking calls, consistent error handling. All 89 Phase 1 tests pass. There are a handful of issues (2 major, 5 minor) that should be addressed before Phase 2 begins.

---

## Component Reviews

### Database Layer (`data/db.py`)

**Status: OK — Minor Issues**

Well-structured async SQLite implementation. Good choices:
- WAL mode enabled at initialization
- `PRAGMA foreign_keys=ON` enabled
- Migration runner is clean and idempotent (checks `schema_version` before applying)
- Proper `ON CONFLICT` upsert for OHLCV data
- `row_factory = aiosqlite.Row` enables dict-style access
- IST timestamps for audit log

Issues found:

1. **[Major — M1] Migration atomicity broken by `executescript`.** The migration runner uses `conn.executescript(sql)` (line 91), which runs its own implicit transactions and ignores the connection's transaction state. If a migration SQL contains multiple statements and one fails partway through, the earlier statements will have already committed. The `schema_version` insert on line 92-96 runs in a separate transaction, so a partial migration failure leaves the DB in an inconsistent state (partially applied DDL, no version record). Fix: Either (a) wrap each migration in an explicit `BEGIN`/`COMMIT` within the SQL file itself and use `executescript`, or (b) use `conn.execute()` per statement after splitting on `;` inside an explicit transaction. Note: SQLite DDL is not transactional for all operations, but `CREATE TABLE` and `CREATE INDEX` are, so option (b) would work for current migrations.

2. **[Minor — m1] `get_ohlcv` staleness uses SQLite `datetime('now')` (UTC) for comparison.** The query `timestamp >= datetime('now', ? || ' days')` compares against UTC, but some providers store timestamps without timezone info (naive datetimes). This works consistently only if all providers write UTC timestamps, which they do not (jugaad uses `datetime.combine(date, min.time())` producing midnight-local, yfinance strips tzinfo). This could cause `get_ohlcv(days=1)` to return or miss data depending on timezone. Not a blocker since the ingester's staleness check (in-memory) is the real gate, but worth documenting.

3. **[Minor — m2] `upsert_watchlist` uses DELETE + INSERT (non-atomic window).** Between the DELETE and the INSERT loop, a concurrent reader could see an empty watchlist. A single transaction wrapping both would be safer. Currently, the individual `conn.commit()` at the end commits all the inserts, but the DELETE already auto-committed (aiosqlite autocommit behavior). Consider wrapping in `async with conn.execute("BEGIN"): ...`.

4. **[Minor — m3] No `log_audit` commit batching.** Every audit log entry issues its own `conn.commit()`. For high-frequency logging (e.g., during a heartbeat with multiple skill runs), this could be a performance issue. Consider batching or deferring commits.

### Migration Schema (`migrations/001_initial.sql`)

**Status: OK**

Schema matches the plan exactly. All 11 tables present (excluding `schema_version`, which is created by the migration runner). Good choices:
- Proper `UNIQUE` constraints on ohlcv (symbol, interval, timestamp)
- Indexes on high-traffic lookup patterns (ohlcv by symbol/interval/timestamp, audit_log by timestamp, sentiment by symbol)
- `REFERENCES` on predictions table for data integrity
- `DEFAULT (datetime('now'))` for timestamp columns

One observation:
- **[Nit — n1]** The `trades` table has no index on `status`, but `get_open_positions()` filters by `status IN ('open', 'partially_filled')`. This is fine at low volume but Phase 3 should add an index when trade volume increases.

### Market Data Providers

#### JugaadDataProvider (`data/jugaad.py`)

**Status: OK — Minor Issue**

Clean implementation. Good: lazy import of `jugaad_data` inside the thread function, `asyncio.Semaphore(3)` for concurrent request limiting, sorted output.

- **[Minor — m4] Volume column name.** Line 86 falls back to `NO OF TRADES` if `VOLUME` is not present. This is the correct column name for jugaad-data's NSE scraper, but `NO OF TRADES` is not the same as volume (it's trade count, not share volume). The actual volume column in jugaad-data is `TOTTRDQTY` for total traded quantity. If `VOLUME` is not present, the fallback produces misleading data. Verify the actual column names returned by `stock_df`.

#### YFinanceProvider (`data/yfinance_provider.py`)

**Status: OK**

Solid fallback provider. Good: `.NS` suffix handling, rate limiting via semaphore + 0.5s delay, timezone stripping for consistency, interval mapping.

One observation:
- The 0.5s delay inside `get_ohlcv` is unconditional (even when the semaphore is not contended). This is conservative but acceptable for a fallback provider.

#### TVDatafeedProvider (`data/tvfeed.py`)

**Status: OK**

Good: lazy client initialization, semaphore(1) for sequential access, free-tier day cap (`min(days, 15)`), bar count estimation.

- **[Nit — n2]** The `_get_client()` method is called from within `asyncio.to_thread`, meaning the TvDatafeed constructor (which may do network I/O) runs correctly in a thread. However, `self._tv` is shared mutable state accessed from threads without locking. In practice, the semaphore(1) ensures sequential access, so this is safe. Worth a comment.

### Market Data Ingester (`data/ingester.py`)

**Status: OK**

The fallback chain logic is clean and correct:
- Tries providers in order, falls back on exception or stale data
- Data quality validation catches `high < low`, `close` outside range, `open` outside range
- Staleness check uses configurable threshold
- Health check returns true if any provider is up
- Intraday routing correctly directs to the intraday provider

One observation:
- **[Minor — m5] Staleness check uses `datetime.now()` (naive) vs bar timestamps (also naive).** This works as long as all providers return timestamps in the same timezone. The jugaad provider returns midnight-local, yfinance strips timezone. Since daily data only needs day-level granularity (threshold is 2 days for daily), this is acceptable. For intraday, the threshold is `stale_threshold_minutes` and both yfinance and tvdatafeed return local-exchange timestamps, so this works. But this should be documented.

### Feature Engineering (`data/features.py`)

**Status: OK**

All FR-4.1 indicators implemented as pure functions. Good: no side effects, proper insufficient-data guards, toggleable via `IndicatorConfig`.

Correctness verified:
- RSI: Wilder's smoothing (correct), handles all-gains (100.0) and all-losses properly
- MACD: EMA-based (correct), signal line computed from MACD line
- Bollinger Bands: Population variance (divides by N, not N-1). This is the standard financial formula.
- VWAP: Typical price * volume / total volume (correct)
- ATR: True Range with Wilder smoothing (correct)
- OBV: Cumulative volume with direction (correct)
- SuperTrend: Simplified to single-bar comparison (acceptable for feature engineering; full SuperTrend tracks band history)
- EMA: Standard multiplier formula (correct)

One observation:
- **[Nit — n3]** The SuperTrend implementation is simplified compared to the full indicator (which tracks upper/lower band carryover across bars). The current version computes bands from the latest bar only. This is documented in the code comment ("Simplified") and is acceptable for Phase 1, but Phase 2 should consider a full implementation for signal generation accuracy.

### Zerodha Broker (`broker/zerodha.py`)

**Status: OK — Minor Issue**

Full `BrokerBase` implementation. Paper mode is well-designed with simulated slippage. Live mode properly uses `asyncio.to_thread` for the synchronous Kite SDK.

Good:
- Semaphore(8) rate limiter stays safely under Kite's 10 req/s
- Exponential backoff retry (2s, 4s, 8s) matches FR-6.6
- Paper mode correctly simulates: market fills with slippage, limit orders stay open, cancel works

Issues:
- **[Major — M2] `authenticate()` is not in `BrokerBase` ABC.** The `ZerodhaBroker.authenticate(request_token)` method is not declared in `BrokerBase` or `BrokerProtocol`. This means it can only be called via type narrowing (checking `isinstance(broker, ZerodhaBroker)`). This is acceptable for now since only `auth-broker` skill calls it, but the ABC should include `authenticate()` for any future broker implementations (e.g., a different broker would also need daily auth). Alternatively, keep it off the ABC if authentication flows are broker-specific, but document the pattern.

- **[Nit — n4]** Paper mode `get_margins` returns hardcoded `100_000` cash. This should ideally track capital from config (`capital.initial_amount`) and deduct from filled orders. Not needed for Phase 1 but Phase 3 will require accurate paper margin tracking.

- **[Nit — n5]** The `_retry_api_call` uses a lambda (`lambda: self._kite.place_order(...)`) which captures variables by reference. This is correct in this context but using `functools.partial` would be clearer.

### Gemini LLM (`llm/gemini.py`)

**Status: OK**

All 7 `LLMBase` methods implemented. Good:
- Pro model for complex analysis, Flash for routine (ping, sentiment) per NFR-6
- Structured JSON output via `response_mime_type`
- Exponential backoff retry on all API calls
- Lazy client initialization
- Prompts include schema examples for structured output

Observations:
- **[Minor — m6] No JSON parse error handling.** If Gemini returns invalid JSON (which happens occasionally with LLMs), `json.loads(text)` will raise `JSONDecodeError` with no retry or fallback. This should either retry with a "please respond with valid JSON" re-prompt, or wrap in try/except with a meaningful error message. This is an expected edge case in production.

- **[Nit — n6]** `summarize_with_web_grounding` does not actually use Gemini's native search grounding feature (which requires specific API parameters like `tools=[genai.Tool(google_search=...)]`). It just prompts the model to "search the web." The model may hallucinate sources. Phase 2 should use the actual grounding API.

- **[Nit — n7]** Prompts are inline strings. For maintainability, consider moving them to a separate module or using template strings as the prompts grow in Phase 2+.

### Main Wiring (`main.py`)

**Status: OK**

Clean wiring logic. Good:
- Stubs remain as fallbacks when API keys are not configured (check for unexpanded `${VAR}` pattern)
- Database always created (not stubbed) since it only needs a file path
- Graceful shutdown via signal handlers
- DB cleanup in `finally` block
- CLI override for mode

Observations:
- The `_StubDB` does not implement the full `DatabaseProtocol` (missing `upsert_ohlcv`, `get_ohlcv`, `set_system_state`, `get_system_state`, `log_audit`). Since `_build_db` always returns a real `Database` (line 138), this is not a runtime issue, but the stub is incomplete for type checking purposes. Consider removing or completing it.

### Test Coverage

**Status: OK — Gaps Noted**

89 Phase 1 tests all pass. Coverage is good for the core paths:

**Well-tested:**
- Database: migration system, CRUD operations, upsert idempotency, system state, kill switch, audit log
- Ingester: fallback chain (primary/fallback/all-fail), staleness, data quality, intraday routing, quote fallback, health check, integration with DB
- Features: All 9 indicators tested individually + combined `compute_features`, edge cases (insufficient data, all-gains RSI, constant-price BB, zero-volume VWAP)
- Broker: Full paper mode coverage (auth, place, cancel, positions, margins, slippage)
- LLM: All 7 methods tested with mocked responses, retry behavior

**Test gaps to address:**
1. No tests for live broker mode (even with mocked Kite SDK). The plan called for "Mocked Kite API: auth flow, place_order, rate limiting" but only paper mode is tested.
2. No tests for individual providers (jugaad, yfinance, tvdatafeed) with mocked HTTP. The plan called for `test_jugaad.py`, `test_yfinance.py`, `test_tvfeed.py` but these files were not created. Provider logic is indirectly tested through the ingester tests with mock providers, but the actual data transformation (DataFrame to OHLCVBar) is untested.
3. No test for Gemini JSON parse failure (what happens when the LLM returns non-JSON).
4. No test for the `_build_*` functions in `main.py` (wiring logic).
5. No test for `Database` with a missing migrations directory (line 80 returns early with a warning).

---

## Issues Found

### Critical
(None)

### Major
- **M1** — Migration runner uses `executescript` which breaks atomicity guarantees. A partially failed migration leaves the database in an inconsistent state with no version record. (`data/db.py` line 91)
- **M2** — `authenticate()` method exists on `ZerodhaBroker` but not on `BrokerBase` ABC or `BrokerProtocol`. Document the intended pattern. (`broker/zerodha.py` line 51, `broker/base.py`)

### Minor
- **m1** — `get_ohlcv` SQL timestamp comparison uses UTC (`datetime('now')`) but providers store timestamps in local time. (`data/db.py` line 193)
- **m2** — `upsert_watchlist` DELETE + INSERT loop has a non-atomic window where watchlist appears empty. (`data/db.py` line 215)
- **m3** — Every `log_audit` call issues a separate `conn.commit()`. Consider batching. (`data/db.py` line 281)
- **m4** — jugaad provider falls back to `NO OF TRADES` (trade count) instead of actual volume (`TOTTRDQTY`) when `VOLUME` column is missing. (`data/jugaad.py` line 86)
- **m5** — Staleness check uses naive `datetime.now()` with no timezone awareness. Works in practice but fragile. (`data/ingester.py` line 133)
- **m6** — No handling of `JSONDecodeError` from malformed LLM responses. (`llm/gemini.py`, all methods calling `json.loads`)

### Nit
- **n1** — Missing index on `trades.status` for `get_open_positions()` query. Add when trade volume increases.
- **n2** — `TVDatafeedProvider._tv` is shared mutable state accessed from threads (safe due to semaphore, but worth documenting).
- **n3** — SuperTrend is simplified (single-bar). Full implementation needed for Phase 2 signal accuracy.
- **n4** — Paper mode `get_margins` returns hardcoded 100K, not tracking actual paper capital.
- **n5** — `_retry_api_call` uses lambda; `functools.partial` would be clearer.
- **n6** — `summarize_with_web_grounding` does not use Gemini's native search grounding API.
- **n7** — LLM prompts are inline strings; consider extracting for maintainability.

### Test Gaps
- Missing: mocked live broker tests, individual provider tests (jugaad/yfinance/tvfeed), JSON parse failure test for LLM, wiring logic tests, missing-migrations-dir test.

---

## Recommendations for Phase 2

1. **Fix M1 before Phase 2.** Migration atomicity is critical when Phase 2 adds new migration files (e.g., for news/sentiment tables). A partial migration could leave the schema in an unrecoverable state.

2. **Add JSON error handling to GeminiLLM (m6).** Phase 2 will invoke the LLM heavily for sentiment analysis and watchlist validation. Malformed JSON responses will cause unhandled exceptions in the pipeline. Add retry-with-reprompt or structured output enforcement.

3. **Verify jugaad-data column names (m4).** Before Phase 2 relies on volume-based signals (OBV, volume profile, VWAP), confirm that the volume column is correctly mapped. Incorrect volume data will silently corrupt all volume-dependent indicators.

4. **Add provider-level tests with mocked HTTP.** Phase 2 will add more data sources (news scrapers, NSE APIs). Establishing the pattern of mocked-HTTP provider tests now will make Phase 2 testing easier.

5. **Consider timezone standardization.** Define a convention: all `OHLCVBar.timestamp` values should be IST (or UTC with explicit conversion). Currently, each provider handles timezones differently. This will bite when mixing data from different providers in the same analysis pipeline.

6. **SuperTrend full implementation.** Phase 2's signal generation will need accurate SuperTrend with band carryover. The simplified version is fine for feature engineering POC but not for trading signals.

7. **Architect the web grounding call.** Phase 2 includes Gemini web grounding for pre-market summaries (FR-2.11). Use the actual `google_search` tool parameter in the Gemini API rather than prompt-based "search the web."
