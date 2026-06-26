# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

YoloVest is an AI-driven Indian stock trading platform. It uses Google Gemini for LLM reasoning, XGBoost for ML signals, and Zerodha Kite Connect for execution. Market data comes from a paid Kite Connect data plan as primary (when enabled), with free providers (jugaad-data, yfinance, tvDatafeed) as fallback. Designed to be self-hosted; the dashboard runs behind nginx-proxy + acme-companion for HTTPS.

## Project Structure

```
/
├── backend/                — Python backend (FastAPI + trading engine)
│   ├── src/yolovest/       — Main package
│   │   ├── broker/         — Broker integration (base.py, zerodha.py,
│   │   │                     kite_rate_limiter.py, circuit_breaker.py)
│   │   ├── dashboard/      — FastAPI REST API + WebSocket (app.py wires
│   │   │                     middleware/auth; routes/*.py hold the 140
│   │   │                     endpoints by domain; security/helpers/postback/ws)
│   │   ├── data/           — Market data providers (Kite, jugaad, yfinance,
│   │   │                     tvfeed), DB layer, ingester chain, features,
│   │   │                     NSE constituents, scrapers
│   │   ├── llm/            — LLM integration (base.py, gemini.py)
│   │   ├── models/         — Pydantic data contracts (schemas.py)
│   │   ├── news/           — News scrapers + aggregator
│   │   ├── skills/         — Skills extending SkillBase
│   │   ├── strategy/       — ML signal generation, holding period logic,
│   │   │                     session caps, backtesting
│   │   ├── main.py         — Entry point, context builder
│   │   ├── orchestrator.py — Heartbeat pipeline coordinator
│   │   ├── config.py       — Nested Pydantic config classes
│   │   ├── context.py      — AppContext + Protocol types
│   │   ├── telegram_bot.py — Telegram command handlers
│   │   ├── cron_scheduler.py — CRON skill scheduler
│   │   └── ...             — timezone, memory, costs, events, notify, watchdog
│   ├── tests/              — Tests (mirrors src/ structure)
│   ├── migrations/         — Numbered SQL migration files (.sql)
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/               — React SPA (Vite + TypeScript + Tailwind)
│   ├── src/
│   │   ├── pages/          — Page components
│   │   ├── components/     — Reusable UI components
│   │   ├── api/            — API client + endpoint definitions
│   │   ├── hooks/          — React Query hooks, WebSocket, auth
│   │   ├── types/          — TypeScript type definitions
│   │   └── utils/          — datetime, csvExport, priceMove helpers
│   ├── nginx.conf          — Reverse proxy for /api → backend (uses Docker DNS resolver)
│   ├── Dockerfile
│   └── package.json
├── nginx/                  — Shared nginx-proxy assets
│   ├── custom.conf         — Global HTTP-level overrides
│   ├── heal-cert-symlinks.sh   — Restore <domain>.crt / <domain>.key on each boot
│   └── tls-healthcheck.sh  — Detect ssl_reject_handshake / missing symlinks
├── docs/                   — Detail split out of CLAUDE.md + operational docs
│   ├── architecture.md     — subsystems, heartbeat, risk gates, exit paths
│   ├── database.md         — key tables, quarantine, universe resolution
│   ├── configuration.md    — file-only keys, config sections, toggles
│   ├── key-files.md        — file-by-file backend/frontend/infra map
│   ├── domain-context.md   — Indian-market trading specifics
│   ├── conventions.md      — coding conventions
│   ├── telegram-commands.md
│   ├── tls.md              — TLS reliability overview
│   └── tls-recovery.md
├── backups/                — Volume snapshots (.gitignored)
├── docker-compose.yml
└── CLAUDE.md
```

## Architecture

YoloVest layers cleanly: `orchestrator.py` drives a heartbeat pipeline of
`SkillBase` units (`skills/`) that reach shared subsystems — `broker/`,
`strategy/`, `data/`, `llm/`, `news/` — through Protocol-typed `AppContext`
(`context.py`). Abstraction seams: `BrokerBase`→`ZerodhaBroker`,
`LLMBase`→`GeminiLLM`, `MarketDataBase`→`MarketDataIngester` (provider
fallback chain), `MLBase`→`XGBoostSignalModel`.

**Heartbeat pipeline** (market hours, every 15 min — configurable):
`expire-pending → health-check → ingest-data → depth-snapshot → market-scan
→ generate-signals → [per signal: risk-check → llm-review → trade-execute →
predict-track] → position-monitor`.

Full detail (KiteTicker WebSocket, rate limiter, skill system + schedules,
error propagation, strategy modes, intraday circuit caps, paper/live
filtering, manual-approval flow, signal-disposition retry caps, position
adoption & exit paths, optional risk gates, exit tweaks, margin enforcement,
AppContext, inter-skill data contracts): **[docs/architecture.md](docs/architecture.md)**.

## Database

SQLite (WAL, `synchronous=FULL` — never lose the last write, `foreign_keys=ON`)
versioned by numbered SQL migrations in `backend/migrations/` (lexical order at
startup; **schema only** — never data cleanup). The `Database` class is composed
from per-domain mixins under `data/db/`.

Key tables, the quarantine/replacement resolver, and universe resolution:
**[docs/database.md](docs/database.md)**.

## Telegram Commands

Full command reference (`/status`, `/pnl`, `/approve`, `/trade`, `/kill`,
`/auth`, `/symbol`, …): **[docs/telegram-commands.md](docs/telegram-commands.md)**.

## Configuration

Config is split between a YAML file (file-only keys: secrets, filesystem paths,
server binding) and a SQLite `config` table (everything else — editable via the
Settings UI and hot-applied). Code defaults seed the table on first start.

File-only key list, key config sections (mode / strategy / risk / retraining /
market_hours / execution / scanning / market_data), and service toggles:
**[docs/configuration.md](docs/configuration.md)**.

## Domain Context

Indian-market trading specifics — MIS vs CNC, SL / SL-M conversion,
MARKET→LIMIT conversion, tick rounding, market hours, GTT (CNC-only), daily
Kite re-auth, no overnight retail shorting:
**[docs/domain-context.md](docs/domain-context.md)**.

## TLS / nginx-proxy Reliability

The dashboard runs behind pinned `nginx-proxy` + `acme-companion` with three
defensive layers against the acme cert-symlink renewal failure mode. Overview:
**[docs/tls.md](docs/tls.md)**; manual recovery runbook:
**[docs/tls-recovery.md](docs/tls-recovery.md)**.

## Conventions

Essentials (full list in **[docs/conventions.md](docs/conventions.md)**):

- Python 3.11+, async throughout; mypy (strict) + ruff enforced in CI.
- Tests: `cd backend && PYTHONPATH=src python -m pytest tests/ -v`; frontend: `cd frontend && npm run test`.
- Paper mode by default; all trade/position/prediction queries filter by `ctx.config.mode` — paper and live never mix.
- SQLite WAL; **no explicit `BEGIN`** in write methods (deferred isolation auto-begins). Migrations are schema-only.
- All timestamps: IST for market logic, UTC for DB storage.

## Key Files

A file-by-file map of the backend, frontend, and infrastructure:
**[docs/key-files.md](docs/key-files.md)**.

## Detailed Documentation

This file is the lean overview; deep detail lives in `docs/` and is pulled in
only when a task needs it:

- **[docs/architecture.md](docs/architecture.md)** — subsystems, heartbeat, skills, risk gates, exit paths, inter-skill contracts
- **[docs/database.md](docs/database.md)** — key tables, quarantine, universe resolution
- **[docs/configuration.md](docs/configuration.md)** — file-only keys, config sections, service toggles
- **[docs/key-files.md](docs/key-files.md)** — file-by-file backend / frontend / infra map
- **[docs/domain-context.md](docs/domain-context.md)** — Indian-market trading specifics
- **[docs/conventions.md](docs/conventions.md)** — full coding conventions
- **[docs/telegram-commands.md](docs/telegram-commands.md)** — bot command reference
- **[docs/tls.md](docs/tls.md)** — TLS / nginx-proxy reliability overview
- **[docs/tls-recovery.md](docs/tls-recovery.md)** — TLS / nginx-proxy recovery runbook
