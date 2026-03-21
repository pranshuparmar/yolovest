# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is a fully autonomous AI-driven Indian stock trading platform. It uses OpenClaw for agent orchestration, Google Gemini for LLM reasoning, XGBoost/LightGBM for ML signals, and Zerodha Kite Connect (free tier) for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed).

**Current state:** Early stage — only skill stubs exist under `src/yolovest/skills/`. The full project structure, config system, database, and abstraction layers are yet to be built. REQUIREMENTS.md is the source of truth for all architecture decisions.

## Architecture

### Three Abstraction Layers (ABCs)

- **`BrokerBase`** → `ZerodhaBroker` — execution only (free tier, no market data)
- **`LLMBase`** → `GeminiLLM` — 6 methods: `review_trade`, `analyze_sentiment`, `summarize_with_web_grounding`, `validate_watchlist`, `summarize_market_day`, `analyze_prediction_failures`
- **`MarketDataBase`** → `JugaadDataProvider` (primary) → `YFinanceProvider` (fallback) → `TVDatafeedProvider` (intraday)

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

All data exchange between skills uses typed Pydantic models defined in REQUIREMENTS.md Section 8 (to be implemented in `src/yolovest/models/schemas.py`): `Signal`, `Trade`, `Position`, `PortfolioState`, `TradeContext`, `TradeReview`, `SentimentResult`, `OHLCVBar`, `Prediction`.

## Key Files

- **`REQUIREMENTS.md`** — Complete specification (950+ lines). Sections 1-11 are functional requirements, Section 13 is the cross-functional review (CEO/PM/BA findings), Section 14 is the glossary. Always consult this before making architectural decisions.
- **`src/yolovest/skills/base.py`** — `SkillBase` ABC, `SkillResult`, `SkillTrigger` enum
- **`src/yolovest/skills/__init__.py`** — `SKILL_REGISTRY` mapping skill names to classes

## Implementation Phases

Phase 0 (current target): Skill infrastructure — context object, schemas, orchestrator, event bus, heartbeat loop, config validation, LLM abstraction, Telegram integration.

Phases 1-5 follow sequentially (data pipeline → intelligence → risk/execution → self-learning → dashboard). See REQUIREMENTS.md Section 6.

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
- SQLite with WAL mode. Schema versioned via migration scripts.
- Paper trading mode by default — live trading requires explicit `mode: live` in config.
