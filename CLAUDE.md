# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is a fully autonomous AI-driven Indian stock trading platform. It uses OpenClaw for agent orchestration, Google Gemini for LLM reasoning, XGBoost/LightGBM for ML signals, and Zerodha Kite Connect (free tier) for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed).

**Current state:** Phase 0 complete (TL-approved). Phase 1 (Foundation & Data Pipeline) in progress. See `plan.md` for the implementation plan and `docs/` for review reports.

## What's Built (Phase 0 — Complete)

- **Config system** (`config.py`) — Nested Pydantic v2 models, env var expansion, all Section 10 config keys, time-safe validators
- **Schemas** (`models/schemas.py`) — All 9 inter-skill Pydantic contracts: Signal, Trade, Position, PortfolioState, TradeContext, TradeReview, SentimentResult, OHLCVBar, Prediction + 5 LLM output types
- **Context** (`context.py`) — `AppContext` dataclass with Protocol types for all pluggable backends, `MarketHoursChecker` with timezone-aware checks (ZoneInfo)
- **Orchestrator** (`orchestrator.py`) — Full heartbeat pipeline with FR-1.3 error propagation, mutex (skip-on-overrun), consecutive skip alerting
- **Event bus** (`events.py`) — Async pub/sub for inter-skill communication
- **Notifier** (`notify.py`) — Console backend + Telegram-ready interface
- **ABCs** — `BrokerBase` (7 methods), `LLMBase` (7 methods), `MarketDataBase` (3 methods)
- **Skill stubs** — All 15 skills registered in `SKILL_REGISTRY` with documented flows
- **Tests** — 141 passing (schemas, config, orchestrator, context, events, notify)

## Architecture

### Three Abstraction Layers (ABCs)

- **`BrokerBase`** (`broker/base.py`) → `ZerodhaBroker` — execution only (free tier, no market data)
- **`LLMBase`** (`llm/base.py`) → `GeminiLLM` — 7 methods: `ping`, `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`
- **`MarketDataBase`** (`data/base.py`) → `JugaadDataProvider` (primary) → `YFinanceProvider` (fallback) → `TVDatafeedProvider` (intraday)

### Skill System

15 skills extend `SkillBase` (in `src/yolovest/skills/base.py`). Each skill has:
- `async execute(**kwargs) -> SkillResult`
- `should_run() -> bool`
- A trigger type: `HEARTBEAT`, `CRON`, `EVENT`, or `MANUAL`
- Access to shared context via `self.ctx` (config, db, broker, llm, market_data, etc.)

Skills are registered in `SKILL_REGISTRY` dict in `src/yolovest/skills/__init__.py`.

### Heartbeat Pipeline (market hours, every 15min)

```
health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → trade-execute → predict-track
  → position-monitor
```

Error propagation: if `ingest-data` fails, skip scan+signals but always run `position-monitor`. If `health-check` fails, abort entire heartbeat. Full policy table in REQUIREMENTS.md FR-1.3.

### Inter-Skill Data Contracts

All data exchange between skills uses typed Pydantic models in `src/yolovest/models/schemas.py`: `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`.

## Key Files

- **`REQUIREMENTS.md`** — Complete specification (950+ lines). Sections 1-11 are functional requirements, Section 13 is the cross-functional review (CEO/PM/BA findings), Section 14 is the glossary. Always consult this before making architectural decisions.
- **`plan.md`** — Phase 1 implementation plan (v2, PM-approved)
- **`docs/tl_phase0_review.md`** — TL review of Phase 0 (all issues resolved)
- **`docs/pm_phase1_review.md`** — PM cross-check of Phase 1 plan
- **`src/yolovest/orchestrator.py`** — Heartbeat pipeline, error propagation, mutex
- **`src/yolovest/context.py`** — `AppContext`, Protocol types, `MarketHoursChecker`
- **`src/yolovest/config.py`** — All config models with validators
- **`src/yolovest/models/schemas.py`** — All Pydantic data contracts

## Implementation Phases

- **Phase 0** (complete): Skill infrastructure — context, schemas, orchestrator, event bus, heartbeat, config, ABCs, Telegram
- **Phase 1** (in progress): Database + migrations, market data providers (jugaad/yfinance/tvDatafeed) with fallback chain, feature engineering, Zerodha broker, Gemini LLM
- Phase 2: Intelligence — news aggregation, sentiment, dynamic scanner, ML signal models, backtesting
- Phase 3: Risk & execution — risk manager, LLM trade review gate, order executor, position tracking
- Phase 4: Self-learning — prediction tracking, model retraining, A/B testing
- Phase 5: Dashboard & reporting

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL-M** = Stop-Loss Market order. **GIFT Nifty** = offshore Nifty futures (pre-market indicator).
- Market hours: 9:15 AM - 3:30 PM IST. Square-off at 3:15 PM. ~15 NSE holidays/year.
- Kite API rate limit: 10 req/s aggregate. Daily re-auth required (user pastes request_token via Telegram).
- All risk parameters are configurable via `config.yaml` under `risk.*` section. See REQUIREMENTS.md Section 10.

## Conventions

- Python 3.12+, async throughout
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`
- Config via YAML + Pydantic validation. Secrets via environment variables (never in config files).
- SQLite with WAL mode. Schema versioned via numbered migration scripts in `migrations/`.
- Paper trading mode by default — live trading requires explicit `mode: live` in config.
- Tests use pytest-asyncio with `asyncio_mode = "auto"`. Run with `python -m pytest tests/ -v`.
