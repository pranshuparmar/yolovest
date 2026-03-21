# YoloVest — AI Trading App Plan

## Overview

A **Python-based autonomous Indian stock trading bot** that uses ML models for signal generation, **Gemini API** (via Google AI Pro) for LLM-powered trade reasoning/risk review, and rule-based safety rails. Trades via **Zerodha Kite Connect API**, with a generic broker abstraction so it can be extended to other brokers/markets. Claude (via Claude Max) is used as the development partner to build, iterate, and improve the system.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       YoloVest Core                          │
│                                                              │
│  ┌──────────┐  ┌───────────┐  ┌────────────────────────┐    │
│  │  Market   │  │ Strategy  │  │  Risk Manager          │    │
│  │  Data     │→ │  Engine   │→ │  (rules + LLM review)  │    │
│  │  Ingester │  │  (ML)     │  │                        │    │
│  └──────────┘  └───────────┘  └────────────────────────┘    │
│       │              │               │                       │
│       ▼              ▼               ▼                       │
│  ┌──────────┐  ┌───────────┐  ┌────────────────────────┐    │
│  │  DB      │  │ Backtester│  │  Order Executor        │    │
│  │ (SQLite) │  │           │  │  (Broker Interface)    │    │
│  └──────────┘  └───────────┘  └────────────────────────┘    │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  FastAPI Dashboard + WebSocket live updates          │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘

Broker Abstraction Layer:         LLM Abstraction Layer:
┌──────────────────┐              ┌──────────────────┐
│  BrokerBase      │  ← ABC      │  LLMBase         │  ← ABC
├──────────────────┤              ├──────────────────┤
│  ZerodhaBroker   │              │  GeminiLLM       │  ← Google AI Pro
│  (future) IBBrkr │              │  (future) others │
└──────────────────┘              └──────────────────┘
```

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.12+ | Best trading/ML ecosystem |
| Broker | Zerodha (Kite Connect API) | Popular Indian broker, good API |
| Broker abstraction | Custom ABC | Generic interface to swap brokers |
| ML | scikit-learn + XGBoost | Fast iteration, good for tabular signal data |
| LLM (runtime) | Gemini API (Google AI Pro) | Included with your subscription, API access via AI Studio |
| LLM (dev) | Claude Max | Development partner — building, iterating, debugging |
| Data | SQLite + pandas | Simple, no infra needed, fast for time-series |
| API/Dashboard | FastAPI + Jinja2 + HTMX | Lightweight dashboard, WebSocket for live data |
| Task scheduling | APScheduler | In-process cron-like scheduling |
| Config | YAML + pydantic | Type-safe config with validation |
| Deployment | Docker Compose | Reproducible, easy to deploy anywhere |

## LLM Integration (Gemini API via Google AI Pro)

Your **Google AI Pro** subscription includes Gemini API access with baseline quotas. This enables runtime LLM capabilities:

- **Trade review gate** — before executing any trade, send the signal context (symbol, direction, indicators, portfolio state) to Gemini for a sanity check. It can approve, reject, or suggest resizing.
- **Market sentiment analysis** — summarize recent news/events for traded symbols and assess sentiment.
- **Trade reasoning log** — Gemini explains *why* a trade makes sense (or doesn't), stored in the audit log.
- **Graceful degradation** — if Gemini API is unavailable or quota exhausted, fall back to rule-based risk checks only (never block trading on LLM availability).

**Note:** ChatGPT Go does **not** include API access (app-only), so it can't be used programmatically.

### LLM Abstraction

Generic `LLMBase` ABC so the provider can be swapped:

```python
class LLMBase(ABC):
    @abstractmethod
    async def review_trade(self, context: TradeContext) -> TradeReview: ...

    @abstractmethod
    async def analyze_sentiment(self, symbol: str, headlines: list[str]) -> SentimentResult: ...
```

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
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── base.py         # Abstract LLM interface (ABC)
│       │   └── gemini.py       # Google Gemini implementation
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
│       │   └── manager.py      # Position sizing, limits, circuit breakers, LLM review
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
3. LLM abstraction: `LLMBase` ABC + `GeminiLLM` implementation
4. Database layer: SQLite schema for OHLCV, trades, positions, signals
5. Market data ingester: fetch OHLCV candles via broker, store in DB
6. Feature engineering: RSI, MACD, Bollinger Bands, volume profile, VWAP
7. Basic CLI to run ingestion and inspect data

### Phase 2: Strategy Engine
8. ML signal model: train XGBoost on historical features → buy/sell/hold signal
9. Backtesting engine: simulate trades on historical data, compute PnL/Sharpe/drawdown
10. Training script: CLI to train model, save artifacts, log metrics

### Phase 3: Risk Management + LLM Trade Review
11. Risk manager: max position size, max drawdown circuit breaker, exposure limits
12. LLM trade review gate: send trade context to Gemini → approve/reject/resize
13. Market-hours enforcement (NSE/BSE trading hours only)
14. Graceful LLM fallback: if Gemini unavailable, proceed with rules-only

### Phase 4: Execution
15. Order executor: market/limit orders via broker interface, handle fills and errors
16. Position tracker: reconcile broker state with local DB
17. Paper trading mode: simulated execution for testing without real money

### Phase 5: Dashboard + Deployment
18. FastAPI dashboard: portfolio overview, open positions, trade history, PnL chart
19. WebSocket live updates: push new trades/signals to the UI in real time
20. Docker Compose setup
21. Alerting: Telegram notifications on trades, errors, circuit breakers

## Safety Rails (Critical)

- **Paper trading by default** — live trading requires explicit config flag
- **Max position size** — configurable per-symbol and total portfolio limits
- **Daily loss limit** — circuit breaker halts trading if daily loss exceeds threshold
- **LLM gate** — every trade reviewed by Gemini before execution (with graceful fallback)
- **Market hours only** — no orders outside NSE/BSE trading hours (9:15 AM - 3:30 PM IST)
- **Rate limiting** — respect Kite Connect and Gemini API limits, back off on errors
- **Audit log** — every decision (signal, risk check, LLM reasoning, order) logged with full context

## Config Example (config.yaml)

```yaml
mode: paper  # paper | live

broker:
  name: zerodha
  api_key: ${KITE_API_KEY}        # from environment
  api_secret: ${KITE_API_SECRET}

llm:
  provider: gemini
  model: gemini-2.5-pro           # or gemini-2.5-flash for lower latency
  api_key: ${GEMINI_API_KEY}
  review_every_trade: true
  fallback_to_rules: true         # if LLM unavailable, use rules-only

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

## Gemini API Notes (Google AI Pro)

- **Quota**: Baseline quota included with AI Pro subscription; AI credits consumed after baseline is exhausted.
- **Models**: Gemini 2.5 Pro (best reasoning) or Gemini 2.5 Flash (faster, cheaper quota usage).
- **Rate limits**: Varies by model and tier; implement exponential backoff.
- **API Key**: Generate from Google AI Studio (aistudio.google.com).

## Getting Started (after implementation)

```bash
# Install
pip install -e ".[dev]"

# Configure
cp config.example.yaml config.yaml
# Set env vars: KITE_API_KEY, KITE_API_SECRET, GEMINI_API_KEY

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
