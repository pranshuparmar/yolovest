# YoloVest — AI Trading App Plan

## Overview

A **Python-based autonomous Indian stock trading bot** that uses ML models for signal generation and rule-based risk management. Trades via **Zerodha Kite Connect API**, with a generic broker abstraction so it can be extended to other brokers/markets. Claude (via Claude Max) is used as the development partner to build, iterate, and improve the system — not called at runtime.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     YoloVest Core                        │
│                                                          │
│  ┌──────────┐  ┌───────────┐  ┌───────────────────────┐ │
│  │  Market   │  │ Strategy  │  │  Risk Manager         │ │
│  │  Data     │→ │  Engine   │→ │  (rule-based limits,  │ │
│  │  Ingester │  │  (ML)     │  │   circuit breakers)   │ │
│  └──────────┘  └───────────┘  └───────────────────────┘ │
│       │              │               │                   │
│       ▼              ▼               ▼                   │
│  ┌──────────┐  ┌───────────┐  ┌───────────────────────┐ │
│  │  DB      │  │ Backtester│  │  Order Executor       │ │
│  │ (SQLite) │  │           │  │  (Broker Interface)   │ │
│  └──────────┘  └───────────┘  └───────────────────────┘ │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │  FastAPI Dashboard + WebSocket live updates      │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘

Broker Abstraction Layer:
┌──────────────────┐
│  BrokerBase      │  ← Abstract interface
├──────────────────┤
│  ZerodhaBroker   │  ← Kite Connect implementation
│  (future) IBBrkr │  ← Interactive Brokers, etc.
└──────────────────┘
```

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.12+ | Best trading/ML ecosystem |
| Broker | Zerodha (Kite Connect API) | Popular Indian broker, good API |
| Broker abstraction | Custom ABC | Generic interface to swap brokers |
| ML | scikit-learn + XGBoost | Fast iteration, good for tabular signal data |
| Data | SQLite + pandas | Simple, no infra needed, fast for time-series |
| API/Dashboard | FastAPI + Jinja2 + HTMX | Lightweight dashboard, WebSocket for live data |
| Task scheduling | APScheduler | In-process cron-like scheduling |
| Config | YAML + pydantic | Type-safe config with validation |
| Deployment | Docker Compose | Reproducible, easy to deploy anywhere |

## Role of Claude

Since you have **Claude Max** (not API access), Claude's role is:
- **Development partner** — building and iterating on the codebase (what we're doing now)
- **Strategy advisor** — analyzing backtest results, suggesting improvements
- **Debugging** — diagnosing issues with live trading, data quality, etc.
- **NOT called at runtime** — the bot is fully self-contained once deployed

## Project Structure

```
yolovest/
├── pyproject.toml              # Project config, dependencies
├── Dockerfile
├── docker-compose.yml
├── config.yaml                 # Trading config (symbols, limits, intervals)
├── src/
│   └── yolovest/
│       ├── __init__.py
│       ├── main.py             # Entry point, scheduler setup
│       ├── config.py           # Pydantic settings/config models
│       ├── models/
│       │   ├── __init__.py
│       │   └── schemas.py      # Data models (Trade, Signal, Position, etc.)
│       ├── broker/
│       │   ├── __init__.py
│       │   ├── base.py         # Abstract broker interface (ABC)
│       │   └── zerodha.py      # Zerodha Kite Connect implementation
│       ├── data/
│       │   ├── __init__.py
│       │   ├── ingester.py     # Fetch OHLCV candles via broker API
│       │   ├── features.py     # Feature engineering (indicators, derived)
│       │   └── db.py           # SQLite read/write, migrations
│       ├── strategy/
│       │   ├── __init__.py
│       │   ├── ml_signal.py    # XGBoost/sklearn signal model
│       │   └── backtest.py     # Backtesting engine
│       ├── risk/
│       │   ├── __init__.py
│       │   └── manager.py      # Position sizing, exposure limits, circuit breakers
│       ├── execution/
│       │   ├── __init__.py
│       │   └── executor.py     # Order placement, fills, retries via broker
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
2. Broker abstraction: `BrokerBase` ABC + `ZerodhaBroker` implementation
3. Database layer: SQLite schema for OHLCV, trades, positions, signals
4. Market data ingester: fetch OHLCV candles via broker, store in DB
5. Feature engineering: RSI, MACD, Bollinger Bands, volume profile, VWAP
6. Basic CLI to run ingestion and inspect data

### Phase 2: Strategy Engine
7. ML signal model: train XGBoost on historical features → buy/sell/hold signal
8. Backtesting engine: simulate trades on historical data, compute PnL/Sharpe/drawdown
9. Training script: CLI to train model, save artifacts, log metrics

### Phase 3: Risk Management
10. Risk manager: max position size, max drawdown circuit breaker, exposure limits
11. Per-symbol and portfolio-level position limits
12. Market-hours enforcement (NSE/BSE trading hours only)

### Phase 4: Execution
13. Order executor: market/limit orders via broker interface, handle fills and errors
14. Position tracker: reconcile broker state with local DB
15. Paper trading mode: simulated execution for testing without real money

### Phase 5: Dashboard + Deployment
16. FastAPI dashboard: portfolio overview, open positions, trade history, PnL chart
17. WebSocket live updates: push new trades/signals to the UI in real time
18. Docker Compose setup
19. Alerting: Telegram notifications on trades, errors, circuit breakers

## Safety Rails (Critical)

- **Paper trading by default** — live trading requires explicit config flag
- **Max position size** — configurable per-symbol and total portfolio limits
- **Daily loss limit** — circuit breaker halts trading if daily loss exceeds threshold
- **Market hours only** — no orders outside NSE/BSE trading hours (9:15 AM - 3:30 PM IST)
- **Rate limiting** — respect Kite Connect API limits, back off on errors
- **Audit log** — every decision (signal, risk check, order) is logged with full context

## Config Example (config.yaml)

```yaml
mode: paper  # paper | live

broker:
  name: zerodha
  api_key: ${KITE_API_KEY}        # from environment
  api_secret: ${KITE_API_SECRET}

trading:
  symbols: ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
  exchange: NSE
  interval: "5minute"
  max_position_pct: 0.05       # max 5% of portfolio per trade
  max_daily_loss_pct: 0.03     # stop trading if down 3% in a day
  max_open_positions: 3

market_hours:
  open: "09:15"
  close: "15:30"
  timezone: "Asia/Kolkata"

dashboard:
  host: "0.0.0.0"
  port: 8080

notifications:
  telegram:
    enabled: false
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
```

## Zerodha Kite Connect Notes

- **Authentication**: Kite uses a login flow that generates a `request_token` daily. The bot needs to handle daily re-authentication (manual or automated).
- **Historical data**: Available via `kite.historical_data()` — OHLCV candles at various intervals.
- **Order types**: Market, Limit, SL, SL-M supported.
- **WebSocket**: Kite Ticker for real-time price streaming.
- **Rate limits**: 3 requests/second for most endpoints, 1 request/second for historical data.

## Getting Started (after implementation)

```bash
# Install
pip install -e ".[dev]"

# Configure
cp config.example.yaml config.yaml
# Set env vars: KITE_API_KEY, KITE_API_SECRET

# Ingest historical data
python -m yolovest.main ingest --symbol RELIANCE --days 90

# Train model
python scripts/train_model.py

# Run backtest
python scripts/backtest_runner.py

# Start bot (paper mode)
python -m yolovest.main run

# Start dashboard
python -m yolovest.dashboard.app
```
