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
│   ├── telegram-commands.md
│   ├── tls-recovery.md
│   ├── kite-features-backlog.md
│   └── intraday-model-design.md   — Design notes for a future 5-min intraday model
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

**Heartbeat pipeline** (market hours, every 15 min):
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

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL** order = stop-loss limit (price + trigger_price). **SL-M** would be stop-loss market but Zerodha disabled it for retail API; `ZerodhaBroker` auto-converts incoming `SL-M` into `SL` with a 0.5% buffer past the trigger.
- **MARKET** orders auto-convert to **LIMIT** at LTP ± buffer (1% without paid data, 0.5% with paid data) — Zerodha API restriction.
- **Tick rounding** is applied automatically to every `price` and `trigger_price` in `_live_place_order` (default 0.05 tick).
- Market hours: 9:15 AM – 3:30 PM IST. Default `intraday_cutoff` = 14:30 IST (configurable; no MIS signals after).
- **GTT** (Good Till Triggered) is CNC-only at Zerodha. MIS positions get a broker-side resting LIMIT-target + SL pair (OCO enforced by position-monitor) instead.
- Kite Connect daily re-auth required (paste request_token via Telegram `/auth` or dashboard).
- SELL signals for stocks not in holdings are forced to MIS/intraday (Indian equity rules: no overnight short selling for retail).

## TLS / nginx-proxy Reliability

The stack hosts the dashboard behind `nginxproxy/nginx-proxy` + `nginxproxy/acme-companion` (pinned versions in `docker-compose.yml`). Three defensive layers protect against the well-known cert-symlink failure mode where acme-companion deletes top-level `<domain>.crt` / `<domain>.key` symlinks during a failed renewal attempt:

1. **Pinned image versions** prevent silent upstream behaviour drift.
2. **`nginx/heal-cert-symlinks.sh`** runs as the nginx-proxy entrypoint before nginx boots, recreating any missing symlinks.
3. **`nginx/tls-healthcheck.sh`** marks the container unhealthy when `ssl_reject_handshake on;` is present in generated config or when a per-domain dir exists without its top-level symlinks — Docker's `restart: always` then re-runs the entrypoint heal. Worst-case dashboard downtime after a botched mid-life renewal is ~3 min (healthcheck interval 60s × 3 retries) which is fine for a single-user app.

Details and manual recovery steps in `docs/tls-recovery.md`.

The frontend nginx config (`frontend/nginx.conf`) uses Docker's embedded DNS (`resolver 127.0.0.11`) and a `proxy_pass` variable so that backend container restarts (which assign a new IP) don't strand cached DNS in the frontend's nginx and cause 502s.

## Conventions

- Python 3.11+, async throughout.
- All skills follow the same pattern: extend `SkillBase`, implement `execute()` and `should_run()`.
- `safe_execute()` wraps all skill execution with `logger.exception()` on failure.
- Config via YAML (file-only keys) + DB `config` table (everything else) + Pydantic validation. Startup log prints effective values AFTER DB overrides are applied.
- SQLite with WAL mode. Schema versioned via numbered migration scripts. **No explicit `BEGIN` in write methods** — Python sqlite3's default deferred isolation auto-begins on the first DML, and explicit `BEGIN` conflicts when other writers are active on the same connection.
- Paper mode by default — live trading requires explicit `mode: live`.
- All trade/position queries filter by `ctx.config.mode` — paper and live data never mix.
- Trades track `origin` (`system` / `adopted`) and `mode` (`paper` / `live`). Signals, pending_trades, and predictions also carry `mode` so bulk-delete and analytics can scope cleanly.
- Pending trades use Python ISO timestamps (not SQLite `datetime('now')`) for correct expiry comparison.
- Broker `_retry_api_call` skips permanent errors (validation, margin, auth) — only retries transient errors.
- Before retrying order placement, trade-execute checks `kite.orders()` for recent matching orders. If found, the surviving order is **reconciled** as a successful trade (not retried; not marked failed).
- All Kite calls use the shared `KiteRateLimiter` (concurrency + time-based) plus, for `historical_data`, an additional tighter throttle inside `KiteDataProvider`.
- All timestamps use IST for market logic, UTC for DB storage.
- Frontend uses Vite + TypeScript + Tailwind. All UI timestamps localized to IST.
- Destructive actions require user confirmation in the UI.
- Tests: `cd backend && PYTHONPATH=src python -m pytest tests/ -v`.
- Frontend dev: `cd frontend && npm run dev`. Build: `npm run build`.

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
- **[docs/telegram-commands.md](docs/telegram-commands.md)** — bot command reference
- **[docs/intraday-model-design.md](docs/intraday-model-design.md)** — design notes for the future 5-min intraday model
- **[docs/kite-features-backlog.md](docs/kite-features-backlog.md)** — Kite integration backlog
- **[docs/tls-recovery.md](docs/tls-recovery.md)** — TLS / nginx-proxy recovery runbook
