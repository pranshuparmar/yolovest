# YoloVest

Fully autonomous AI-driven Indian stock trading platform. Uses Google Gemini for LLM reasoning, XGBoost/LightGBM for ML signals, and Zerodha Kite Connect for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed).

**Paper trading by default** — live trading requires explicit opt-in.

## Features

- **15-skill heartbeat pipeline** — health check, data ingestion, market scanning, signal generation, risk checks, LLM trade review, execution, position monitoring, square-off, prediction tracking, model retraining, and reporting
- **ML signal models** — XGBoost with Platt scaling, walk-forward backtesting, shadow mode promotion
- **Gemini LLM integration** — sentiment analysis, trade review gate, watchlist validation, market summaries
- **Full risk management** — position sizing, exposure caps, daily/weekly circuit breakers, trailing stop-loss, kill switch
- **Multi-source market data** — jugaad-data (primary) → yfinance (fallback) → tvDatafeed (intraday), with automatic failover
- **Zerodha Kite Connect** — paper mode with simulated slippage, live mode with SL orders and retry logic
- **Web dashboard** — FastAPI REST API (12 endpoints) + WebSocket live updates, trade reasoning chain, equity curve
- **Telegram bot** — real-time alerts, portfolio status, kill switch control
- **Self-learning** — prediction tracking/scoring, automated model retraining with A/B shadow testing

## Requirements

- Python 3.11+
- SQLite (bundled with Python)
- API keys (optional, falls back to stubs if not set):
  - [Zerodha Kite Connect](https://kite.trade/) — for live/paper brokerage
  - [Google Gemini](https://ai.google.dev/) — for LLM features
  - [Telegram Bot](https://core.telegram.org/bots) — for notifications

## Installation

```bash
# Clone the repository
git clone https://github.com/pranshuparmar/yolovest.git
cd yolovest

# Install with pip
pip install -e .

# Install dev dependencies (for tests/linting)
pip install -e ".[dev]"
```

## Configuration

### 1. Create config file

```bash
cp config.example.yaml config.yaml
```

### 2. Set environment variables for secrets

```bash
export KITE_API_KEY="your_zerodha_api_key"
export KITE_API_SECRET="your_zerodha_api_secret"
export GEMINI_API_KEY="your_gemini_api_key"
export TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
export TELEGRAM_CHAT_ID="your_telegram_chat_id"
```

All secrets use `${VAR_NAME}` syntax in `config.yaml` and are expanded from environment variables at startup. Never put secrets directly in config files.

### 3. Key configuration sections

| Section | What it controls |
|---------|-----------------|
| `mode` | `paper` (default) or `live` |
| `capital.initial_amount` | Starting capital in INR (default: 100,000) |
| `broker` | Zerodha Kite API credentials |
| `llm` | Gemini model and API key |
| `market_data` | Data providers and staleness thresholds |
| `heartbeat` | Pipeline interval (15min market hours, 60min off hours) |
| `scanning` | Stock universe, shortlist size, scoring weights |
| `strategy` | Indicators, EMA periods, trade type, backtest thresholds |
| `risk` | Position limits, exposure caps, circuit breakers, trailing SL |
| `market_hours` | NSE trading hours, holidays, square-off time |
| `execution` | Order retries, slippage, timeouts |
| `dashboard` | Host, port, password |
| `notifications.telegram` | Bot token, chat ID, per-alert-type toggles |

See `config.example.yaml` for all options with inline documentation.

## Usage

### Run locally

```bash
# Paper trading (default)
python -m yolovest.main --config config.yaml

# Live trading
python -m yolovest.main --config config.yaml --mode live

# Without web dashboard
python -m yolovest.main --config config.yaml --no-dashboard
```

### Run with Docker

```bash
# Build and start
docker compose up -d

# View logs
docker compose logs -f yolovest

# Stop
docker compose down
```

Set environment variables in a `.env` file or pass them directly:

```bash
KITE_API_KEY=xxx GEMINI_API_KEY=xxx docker compose up -d
```

The dashboard is exposed on port 8080 (configurable via `DASHBOARD_PORT`).

### Dashboard

Once running, access the web dashboard at `http://localhost:8080`. Default password: `yolovest` (change via `dashboard.password` in config).

**API endpoints:**

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/portfolio` | Portfolio overview |
| `GET /api/positions` | Open positions |
| `GET /api/trades/today` | Today's trades |
| `GET /api/trades/history` | Trade history with filters |
| `GET /api/trades/{id}` | Trade detail with full reasoning chain |
| `GET /api/equity-curve` | Daily cumulative PnL |
| `GET /api/predictions` | Prediction scoreboard |
| `GET /api/reports` | Historical reports |
| `GET /api/watchlist` | Current watchlist |
| `GET /api/sectors` | Sector rotation data |
| `GET /api/audit` | Audit log |
| `WS /ws` | Real-time event stream |

### Telegram Bot

Enable in config:

```yaml
notifications:
  telegram:
    enabled: true
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
```

**Commands:**

| Command | Description |
|---------|-------------|
| `/start` | Initialize bot |
| `/status` | System status |
| `/pnl` | Today's PnL |
| `/positions` | Open positions |
| `/stop` | Activate kill switch (stop new trades) |
| `/kill` | Force close all positions |
| `/resume` | Deactivate kill switch |
| `/auth <token>` | Authenticate Zerodha session |

### Running Tests

```bash
python -m pytest tests/ -v
```

## Architecture

```
heartbeat pipeline (every 15min during market hours):

health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → trade-execute → predict-track
  → position-monitor
```

### Abstraction layers

- **BrokerBase** → `ZerodhaBroker` — order execution (paper + live)
- **LLMBase** → `GeminiLLM` — 7 methods (sentiment, trade review, summaries, etc.)
- **MarketDataBase** → `JugaadDataProvider` → `YFinanceProvider` → `TVDatafeedProvider` (fallback chain)

### Key directories

```
src/yolovest/
├── broker/          # Zerodha Kite Connect integration
├── dashboard/       # FastAPI web dashboard
├── data/            # Database, market data providers, feature engineering
├── llm/             # Gemini LLM integration
├── models/          # Pydantic schemas for inter-skill data contracts
├── news/            # RSS scrapers (MoneyControl, ET Markets, LiveMint)
├── skills/          # 15 skills (heartbeat pipeline stages)
├── strategy/        # ML models (XGBoost), backtesting engine
├── config.py        # Pydantic config with env var expansion
├── context.py       # AppContext, Protocol types, MarketHoursChecker
├── events.py        # Async pub/sub event bus
├── notify.py        # Console + Telegram notifier
├── orchestrator.py  # Heartbeat pipeline with error propagation
├── telegram_bot.py  # Telegram bot commands
└── main.py          # Entry point
migrations/          # Numbered SQL migration scripts
tests/               # pytest-asyncio test suite (472 tests)
```

## License

MIT
