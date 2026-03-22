# PM Review — Phase 2: Intelligence Layer Plan

**Reviewer:** Product Manager
**Date:** 2026-03-22
**Plan version:** v1

## Summary

**Status: APPROVED WITH CHANGES**

The plan covers the core Phase 2 scope well — news aggregation, pre-market ingestion, market scanning, ML signal generation, backtesting, and model retraining are all addressed. However, there are 7 gaps where specific FR requirements are missing or under-specified, and 3 cross-functional findings (C5, H10, H19, H20) from Section 13 that need explicit mitigation. The P0/P1 split needs minor adjustment — FR-2.6 (economic calendar) and FR-3.4 (dynamic watchlist refresh) are missing from the plan entirely.

---

## Requirements Coverage Matrix

| FR | Requirement | Plan Step | Status |
|----|------------|-----------|--------|
| FR-2.1 | Data source abstraction + providers | Phase 1 (done) | **Covered** |
| FR-2.1d | Seed data (NSE Bhavcopy CSVs) | Not mentioned | **Missing** |
| FR-2.2 | NSE/BSE official data (corp actions, bulk deals, FII/DII, delivery) | Step 1b, 3a | **Covered** |
| FR-2.3 | News from MoneyControl, ET Markets, LiveMint | Step 1b | **Covered** |
| FR-2.4 | Fundamentals from Screener.in | Step 3d | **Covered (P1)** |
| FR-2.5 | Technicals from Trendlyne | Step 3e | **Covered (P1)** |
| FR-2.6 | Economic calendar events (RBI, Fed, GDP, earnings) | — | **Missing** |
| FR-2.7 | Gemini sentiment analysis per symbol | Step 3 (execute flow) | **Covered** |
| FR-2.8 | Store all ingested data with timestamps | Step 5 | **Covered** |
| FR-2.9 | Rate-limit all data fetching | Step 1b (per-scraper semaphore) | **Covered** |
| FR-2.10 | Pre-market data (GIFT Nifty, global indices) | Step 2 | **Covered** |
| FR-2.11 | Gemini web grounding for real-time news | Step 2e | **Covered** |
| FR-2.12 | Google Finance for broader sentiment | Step 1b | **Covered** |
| FR-2.13 | News deduplication across sources | Step 1c | **Covered** |
| FR-3.1 | Pre-market scan of NSE universe | Step 4 | **Covered** |
| FR-3.2 | Weighted ranking algorithm | Step 4c | **Covered** |
| FR-3.3 | Filter illiquid, F&O banned, corp action stocks | Step 4a | **Covered** |
| FR-3.4 | Dynamic watchlist updates throughout day | — | **Missing** |
| FR-3.5 | Sector rotation tracking | Step 4b | **Covered (P2 in spec)** |
| FR-3.6 | Gemini cross-validation of shortlist | Step 4 (execute flow) | **Covered** |
| FR-4.1 | Feature engineering (all indicators) | Phase 1 (done) + Step 9 | **Covered** |
| FR-4.2 | XGBoost/LightGBM models with confidence scores | Step 6b | **Covered** |
| FR-4.3 | Separate intraday + swing models | Step 6b, 7a | **Covered** |
| FR-4.4 | Gemini trade review before every trade | Phase 3 (deferred) | **Correct deferral** |
| FR-4.5 | Signal fields (entry, target, SL, size, confidence) | Step 7b | **Covered** |
| FR-4.6 | Backtesting engine (Sharpe, drawdown) | Step 6c | **Covered** |
| FR-4.7 | Walk-forward validation (no lookahead) | Step 6c, 8a | **Covered** |
| FR-4.8 | Min confidence score filter | Step 7b | **Covered** |
| FR-7.4 | Scheduled model retraining | Step 8a | **Covered** |
| FR-7.5 | A/B testing / shadow mode | Step 8b | **Covered** |
| FR-7.6 | Gemini failure analysis | Step 8 (execute flow) | **Covered** |
| FR-7.7 | Model versioning with metrics | Step 6b, 8a | **Covered** |
| FR-9.2 | Transaction cost modeling in backtesting | — | **Missing** |

---

## Gaps Found

### G1: FR-2.6 (Economic Calendar) — Missing

The plan does not mention economic calendar events (RBI policy, US Fed decisions, GDP data, earnings dates). This is P1 in the spec. These events cause significant market moves and are important context for the LLM's trade review and pre-market summary.

**Recommendation:** Add as a P1 item. Can be a simple JSON calendar file initially (manually maintained), upgraded to a scraper later. Store in DB and include in `TradeContext.premarket`.

### G2: FR-2.1d (Bhavcopy Seed Data) — Missing

The plan doesn't mention NSE Bhavcopy CSV importing for deep historical backtesting (2013+). This is P1 in the spec and important for training models with sufficient history.

**Recommendation:** Add as a Step 6 sub-task. A one-time import script (`data/bhavcopy.py`) that reads downloaded CSVs and persists to the OHLCV table. The spec's project structure already has `data/bhavcopy.py` listed.

### G3: FR-3.4 (Dynamic Watchlist Refresh) — Missing

The plan mentions producing a watchlist in `market-scan` but doesn't address FR-3.4's requirement for the watchlist to update throughout the day as new data arrives. The skill runs on HEARTBEAT, which should handle this — but the plan should explicitly note that each heartbeat re-scores and updates the watchlist (not just the pre-market scan).

**Recommendation:** Clarify in Step 4 that `market-scan` runs on every heartbeat during market hours and re-scores/updates the watchlist each time.

### G4: FR-9.2 (Transaction Costs in Backtesting) — Missing

The plan's backtester description does not mention transaction cost modeling. FR-9.2 requires including brokerage (₹20 or 0.03%), STT, stamp duty, GST, and exchange fees in backtesting. Without this, backtest results are unrealistically optimistic.

**Recommendation:** Add transaction cost modeling to Step 6c (backtester). Compute per-trade costs: `brokerage + STT + stamp_duty + GST + exchange_turnover`. This is ~0.1% round-trip for intraday, ~0.15% for delivery.

### G5: H15 (Minimum Training Data Size) — Not Addressed

Cross-functional finding H15 flagged that model retraining with insufficient data produces garbage. The plan's `model-retrain` step doesn't specify a minimum data threshold.

**Recommendation:** Add a guard in Step 8: if training data has fewer than N trades (suggest 200 minimum), skip retraining and log a warning.

### G6: Confidence Score Calibration (H6) — Under-specified

The plan mentions confidence scores but doesn't address H6's concern about calibration. FR-4.2 requires "calibrated probability [0.0, 1.0], validated during each model retraining cycle."

**Recommendation:** Add to Step 6b: after training, run Platt scaling or isotonic regression to calibrate model probabilities. Validate calibration during each retraining in Step 8.

### G7: Sub-score Computation Details — Under-specified

Step 4c describes sub-score dimensions (technical, volume, sentiment, fundamental) but doesn't specify the normalization strategy. Scores from different sources have different ranges (RSI: 0-100, PE: 5-80, sentiment: 0-1). Without normalization, weights are meaningless.

**Recommendation:** Add explicit normalization: min-max scale each sub-score to [0, 1] before applying weights. Define how each raw value maps to a 0-1 score.

---

## Risks & Concerns

### R1: Web Scraping Fragility (HIGH — Section 13, H19)

MoneyControl, ET Markets, and LiveMint actively block scraping. The plan's scraper approach will likely fail in production.

**Mitigation:**
- Use RSS feeds where available (MoneyControl has RSS, ET Markets has RSS)
- Add rotating User-Agent headers and respect robots.txt
- Implement graceful degradation — if a scraper fails, continue with available sources
- Consider Google News RSS as a fallback aggregator
- Gemini web grounding (FR-2.11) serves as the ultimate fallback for news intelligence

### R2: FR-2.7/FR-2.3 Priority Mismatch (MEDIUM — Section 13, H20)

Gemini sentiment analysis (P0) depends on news fetching (plan has some sources as P1). If P1 scrapers aren't built, sentiment has no input data.

**Mitigation:** The plan correctly marks MoneyControl and ET Markets as P0. Ensure at least 2 news sources are functional before sentiment runs. Gemini web grounding can serve as minimum viable news source even without dedicated scrapers.

### R3: `ingest-data` Complexity (MEDIUM — Section 13, C5)

C5 flagged that `ingest-data` is a mega-skill covering 12 sub-requirements. The plan's Step 3 wires 5 methods into one skill.

**Mitigation:** The plan's architecture already mitigates this by using `NewsAggregator` and individual scrapers as composable units. The skill itself orchestrates but delegates to focused modules. This is acceptable for Phase 2. Monitor complexity and decompose if testing becomes unwieldy.

### R4: NSE Universe Scan Performance (MEDIUM — Section 13, H10)

H10 flagged scanning ~2000 stocks is unrealistic on a 15-minute heartbeat with free data sources.

**Mitigation:** The plan should specify the pre-filter strategy. Use Nifty 500 (not all NSE) as the default universe. The config already has `scanning.seed_symbols` as fallback. Add `scanning.universe: "nifty500"` config key.

### R5: LLM Cost (LOW)

Phase 2 increases Gemini API usage significantly: sentiment per symbol per heartbeat, watchlist validation, web grounding, failure analysis.

**Mitigation:** The plan correctly uses Flash model for sentiment (routine) and Pro for complex analysis. Consider caching sentiment results for the same headlines (dedup should help). Rate-limit LLM calls per heartbeat.

---

## Config Coverage

| Config Key (from Section 10) | In plan? | Notes |
|------------------------------|----------|-------|
| `scanning.seed_symbols` | Yes | Step 4 |
| `scanning.shortlist_size` | Yes | Step 4 |
| `scanning.min_avg_daily_volume` | Yes | Step 4a |
| `scanning.weights.*` | Yes | Step 4c |
| `strategy.ema_periods` | Yes | Step 7 |
| `strategy.indicators.*` | Yes | Step 7 |
| `strategy.default_trade_type` | Yes | Step 7a |
| `strategy.backtest_min_sharpe` | Yes | Step 6c |
| `strategy.backtest_max_drawdown_pct` | Yes | Step 6c |
| `risk.min_confidence_score` | Yes | Step 7b |
| `retraining.schedule_cron` | Yes | Step 8 |
| `retraining.shadow_mode_days` | Yes | Step 8b |
| `market_data.bhavcopy_dir` | **No** | Needed for G2 (Bhavcopy import) |
| `market_data.cache_ttl_minutes` | **No** | Should be used by news scrapers |

**Missing config keys to add:**
- `scanning.universe` — "nifty500" / "nifty50" / "all" (R4 mitigation)
- `ml.model_type` — "xgboost" / "lightgbm" (plan mentions but not in config)
- `ml.min_training_samples` — minimum data points for retraining (G5)

---

## Prioritization Feedback

The P0/P1 split is mostly correct. Adjustments:

1. **FR-4.7 (walk-forward)** is correctly P0 — H8 flagged that backtest without walk-forward has guaranteed lookahead bias. Good.

2. **FR-3.4 (dynamic watchlist)** should be explicit in P0 — it's the difference between a one-time scan and a living watchlist. The skill already runs on HEARTBEAT, just needs the plan to state this explicitly.

3. **FR-2.6 (economic calendar)** can stay P1, but add a stub that returns empty so the pipeline doesn't break.

4. **Transaction cost modeling (FR-9.2)** should be P0 for backtesting — without it, backtest results are meaningless for deployment decisions.

---

## Recommendations

1. **Add FR-2.6** (economic calendar) as a P1 step with an empty stub initially.

2. **Add FR-2.1d** (Bhavcopy import) as a sub-task in Step 6 for model training data.

3. **Clarify FR-3.4** in Step 4 — state that `market-scan` re-scores on every heartbeat.

4. **Add transaction cost modeling** to Step 6c backtester — include brokerage, STT, GST, stamp duty.

5. **Add minimum training data guard** to Step 8 — skip retraining if < 200 samples.

6. **Add confidence calibration** to Step 6b — Platt scaling or isotonic regression after training.

7. **Add score normalization** to Step 4c — min-max scale sub-scores to [0, 1] before weighting.

8. **Add `scanning.universe` config key** — default "nifty500" to address H10 performance concern.

9. **Add anti-scraping mitigations** to Step 1b — RSS preference, User-Agent rotation, graceful fallback.

10. **Add `ml.min_training_samples` config key** — guard against garbage models.

11. **Specify the project structure** follows REQUIREMENTS.md Section 9 — ML code goes in `src/yolovest/strategy/` (not `src/yolovest/ml/`). The spec lists `strategy/ml_signal.py` and `strategy/backtest.py`.

---

## TL Phase 1 Recommendations Check

| TL Rec | Addressed in Plan? | Notes |
|--------|-------------------|-------|
| #1: Fix M1 (migration atomicity) | **Yes** — fixed before Phase 2 | Done in TL fixes commit |
| #2: JSON error handling in GeminiLLM | **Yes** — fixed | Done in TL fixes commit |
| #3: Verify jugaad-data volume columns | **Yes** — fixed (m4) | Done in TL fixes commit |
| #4: Provider-level tests with mocked HTTP | **Yes** — Step 1 tests | Plan includes mocked HTTP tests per scraper |
| #5: Timezone standardization | **Yes** — fixed (m1, m5) | Done in TL fixes commit |
| #6: SuperTrend full implementation | **Yes** — Step 9 | Explicitly in plan |
| #7: Web grounding API usage | **Not addressed** | Plan still uses prompt-based "search the web". Should use Gemini's `google_search` tool parameter. |

**Action needed:** Address TL rec #7 — update Step 2e to use Gemini's native search grounding API (`tools=[genai.Tool(google_search=...)]`) instead of prompt-based web search.
