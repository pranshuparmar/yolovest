# YoloVest — AI Trading App Plan

## Overview

A **Python-based autonomous crypto trading bot** that uses ML models for signal generation and an LLM (Claude) for reasoning/risk management. Trades on centralized exchanges (starting with Binance) via the `ccxt` library. Deployed via Docker with a web dashboard for monitoring.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   YoloVest Core                     │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  Market   │  │ Strategy │  │  Risk Manager    │  │
│  │  Data     │→ │  Engine  │→ │  (LLM-powered)   │  │
│  │  Ingester │  │  (ML)    │  │                  │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│       │              │               │              │
│       ▼              ▼               ▼              │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  DB      │  │ Backtester│  │  Order Executor  │  │
│  │ (SQLite) │  │          │  │  (ccxt)          │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │  FastAPI Dashboard + WebSocket live updates  │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.12+ | Best trading/ML ecosystem |
| Exchange | Binance (via ccxt) | Most liquid, good API, ccxt abstracts others |
| ML | scikit-learn + XGBoost | Fast iteration, good for tabular signal data |
| LLM | Claude API (Anthropic SDK) | Risk assessment, trade reasoning, news analysis |
| Data | SQLite + pandas | Simple, no infra needed, fast for time-series |
| API/Dashboard | FastAPI + Jinja2 + HTMX | Lightweight dashboard, WebSocket for live data |
| Task scheduling | APScheduler | In-process cron-like scheduling |
| Config | YAML + pydantic | Type-safe config with validation |
| Deployment | Docker Compose | Reproducible, easy to deploy anywhere |

## Project Structure

```
yolovest/
├── pyproject.toml              # Project config, dependencies
├── Dockerfile
├── docker-compose.yml
├── config.yaml                 # Trading config (pairs, limits, intervals)
├── src/
│   └── yolovest/
│       ├── __init__.py
│       ├── main.py             # Entry point, scheduler setup
│       ├── config.py           # Pydantic settings/config models
│       ├── models/
│       │   ├── __init__.py
│       │   └── schemas.py      # Data models (Trade, Signal, Position, etc.)
│       ├── data/
│       │   ├── __init__.py
│       │   ├── ingester.py     # Fetch OHLCV, orderbook, funding rates
│       │   ├── features.py     # Feature engineering (indicators, derived)
│       │   └── db.py           # SQLite read/write, migrations
│       ├── strategy/
│       │   ├── __init__.py
│       │   ├── ml_signal.py    # XGBoost/sklearn signal model
│       │   └── backtest.py     # Backtesting engine
│       ├── risk/
│       │   ├── __init__.py
│       │   ├── manager.py      # Position sizing, exposure limits
│       │   └── llm_review.py   # Claude-based trade review & reasoning
│       ├── execution/
│       │   ├── __init__.py
│       │   └── executor.py     # Order placement, fills, retries
│       └── dashboard/
│           ├── __init__.py
│           ├── app.py          # FastAPI app
│           ├── routes.py       # API routes + WebSocket
│           └── templates/      # Jinja2 HTML templates
│               └── index.html
├── tests/
│   ├── test_features.py
│   ├── test_strategy.py
│   ├── test_risk.py
│   └── test_executor.py
└── scripts/
    ├── train_model.py          # Train/retrain ML model
    └── backtest_runner.py      # Run backtests from CLI
```

## Implementation Phases

### Phase 1: Foundation (scaffold + data pipeline)
1. Project setup: `pyproject.toml`, package structure, config system
2. Database layer: SQLite schema for OHLCV, trades, positions, signals
3. Market data ingester: fetch OHLCV candles via ccxt, store in DB
4. Feature engineering: RSI, MACD, Bollinger Bands, volume profile, funding rate
5. Basic CLI to run ingestion and inspect data

### Phase 2: Strategy Engine
6. ML signal model: train XGBoost on historical features → buy/sell/hold signal
7. Backtesting engine: simulate trades on historical data, compute PnL/Sharpe/drawdown
8. Training script: CLI to train model, save artifacts, log metrics

### Phase 3: Risk Management + LLM Integration
9. Risk manager: max position size, max drawdown circuit breaker, exposure limits
10. LLM trade review: send trade context to Claude API → approve/reject/resize
11. News/sentiment: optional — fetch headlines, have Claude assess market sentiment

### Phase 4: Execution
12. Order executor: market/limit orders via ccxt, handle fills and errors
13. Position tracker: reconcile exchange state with local DB
14. Paper trading mode: simulated execution for testing without real money

### Phase 5: Dashboard + Deployment
15. FastAPI dashboard: portfolio overview, open positions, trade history, PnL chart
16. WebSocket live updates: push new trades/signals to the UI in real time
17. Docker Compose setup: app + optional Grafana for metrics
18. Alerting: Telegram/Discord notifications on trades, errors, circuit breakers

## Safety Rails (Critical)

- **Paper trading by default** — live trading requires explicit config flag
- **Max position size** — configurable per-pair and total portfolio limits
- **Daily loss limit** — circuit breaker halts trading if daily loss exceeds threshold
- **LLM gate** — every trade goes through Claude for a sanity check before execution
- **Rate limiting** — respect exchange API limits, back off on errors
- **Audit log** — every decision (signal, risk check, order) is logged with reasoning

## Config Example (config.yaml)

```yaml
mode: paper  # paper | live
exchange:
  name: binance
  sandbox: true
trading:
  pairs: ["BTC/USDT", "ETH/USDT"]
  interval: "5m"
  max_position_pct: 0.05      # max 5% of portfolio per trade
  max_daily_loss_pct: 0.03    # stop trading if down 3% in a day
  max_open_positions: 3
llm:
  provider: anthropic
  model: claude-sonnet-4-6
  review_every_trade: true
dashboard:
  host: "0.0.0.0"
  port: 8080
```

## Getting Started (after implementation)

```bash
# Install
pip install -e ".[dev]"

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your API keys

# Ingest historical data
python -m yolovest.main ingest --pair BTC/USDT --days 90

# Train model
python scripts/train_model.py

# Run backtest
python scripts/backtest_runner.py

# Start bot (paper mode)
python -m yolovest.main run

# Start dashboard
python -m yolovest.dashboard.app
```
