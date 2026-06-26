# Contributing to YoloVest

Thanks for your interest in improving YoloVest. This is a personal, best-effort
open-source project, so please read this guide before opening a pull request.

## Ground Rules

- **Be honest about risk.** This software places real trades in live mode.
  Changes that touch order execution, risk gates, position sizing, or the
  paper/live boundary get extra scrutiny. Never weaken a safety control without
  a clear rationale.
- **Paper and live never mix.** All trade/position/prediction code paths filter
  by `ctx.config.mode`. Preserve that separation.
- **Open an issue first for anything substantial.** For bug fixes and small
  improvements, a PR is fine. For new features or architectural changes, please
  open an issue to discuss before investing time.

## Development Setup

YoloVest has a Python backend (`backend/`) and a React/TypeScript frontend
(`frontend/`).

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.dev.lock.txt
pip install -e .
```

### Frontend

```bash
cd frontend
npm ci
```

## Checks That Must Pass

CI runs the same gates on every pull request. Run them locally before pushing.

### Backend

```bash
cd backend

# Lint
ruff check

# Type-check (strict)
mypy src/

# Tests (CI enforces a coverage floor of 55%)
PYTHONPATH=src python -m pytest tests/ -v
```

### Frontend

```bash
cd frontend

# Type-check + production build
npm run build

# Tests
npm run test
```

## Conventions

- **Python 3.11+**, async throughout. `mypy` (strict) and `ruff` are enforced.
- **Tests mirror `src/` structure** under `backend/tests/`. Add or update tests
  with your change.
- **Database changes are schema-only migrations.** Add a new numbered SQL file
  in `backend/migrations/` (applied in lexical order at startup). Never put data
  cleanup in a migration, and never edit an already-shipped migration.
- **Timestamps:** IST for market logic, UTC for database storage.
- See [`docs/conventions.md`](docs/conventions.md) for the full list, and
  [`CLAUDE.md`](CLAUDE.md) plus the [`docs/`](docs/) folder for architecture.

## Pull Requests

- Keep PRs focused; one logical change per PR.
- Write a clear description of *what* changed and *why*.
- Make sure all checks above pass locally.
- Do not commit secrets, `.env` files, databases, models, or build artifacts —
  these are already covered by `.gitignore`.

## Reporting Security Issues

Do **not** open a public issue for security vulnerabilities. See
[`SECURITY.md`](SECURITY.md) for private disclosure.
