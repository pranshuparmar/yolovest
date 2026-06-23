> Detail extracted from the project overview (`CLAUDE.md`), which links here. Refer to it when you need this depth.

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

