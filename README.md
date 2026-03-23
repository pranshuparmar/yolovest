# YoloVest

Fully autonomous AI-driven Indian stock trading platform. Uses Google Gemini for LLM reasoning, XGBoost/LightGBM for ML signals, and Zerodha Kite Connect for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed).

**Paper trading by default** — live trading requires explicit opt-in.

## Features

- **16-skill heartbeat pipeline** — health check, data ingestion, market scanning, signal generation, risk checks, LLM trade review, execution, position monitoring, square-off, prediction tracking, model retraining, reporting, and database maintenance
- **ML signal models** — XGBoost with Platt scaling, walk-forward backtesting, shadow mode promotion
- **Gemini LLM integration** — sentiment analysis, trade review gate, watchlist validation, market summaries
- **Full risk management** — position sizing, exposure caps, daily/weekly circuit breakers, trailing stop-loss, kill switch
- **Multi-source market data** — jugaad-data (primary) → yfinance (fallback) → tvDatafeed (intraday), with automatic failover
- **Zerodha Kite Connect** — paper mode with simulated slippage, live mode with SL orders and retry logic
- **Web dashboard** — FastAPI REST API (14 endpoints) + WebSocket live updates, trade reasoning chain, equity curve
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
tests/               # pytest-asyncio test suite (671+ tests)
```

## Sample Deployment Plan (₹25k, 1 Month)

Below is a sample plan for going from zero to live trading with ₹25,000 starting capital. Paper trading for the first 2 days, then live. Model retrains automatically every Saturday.

### Pre-Launch Setup (Day 0)

**1. Get API Keys**

| Service | What You Need | How |
|---------|--------------|-----|
| Zerodha Kite Connect | `KITE_API_KEY`, `KITE_API_SECRET` | Create app at kite.trade/developer (₹2,000 one-time) |
| Google Gemini | `GEMINI_API_KEY` | Get from aistudio.google.com (free tier works) |
| Telegram Bot | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Create bot via @BotFather, get chat_id via @userinfobot |

**2. Set environment variables** (see [Configuration](#configuration) above).

**3. Create `config.yaml`** — copy from `config.example.yaml` with these overrides for ₹25k:

```yaml
mode: paper                          # start paper, switch to live on Day 3

capital:
  initial_amount: 25000

risk:
  max_risk_per_trade_pct: 0.02       # ₹500 max risk per trade
  max_portfolio_exposure_pct: 0.60   # ₹15,000 max deployed
  max_open_positions: 2              # reduced from 3 — ₹25k is tight for 3
  max_single_stock_pct: 0.30         # ₹7,500 max per stock
  daily_loss_limit_pct: 0.03         # ₹750 daily stop
  weekly_loss_limit_pct: 0.05        # ₹1,250 weekly stop
  max_trades_per_day: 5              # conservative for small capital
  min_confidence_score: 0.70         # raised from 0.65 — be picky with ₹25k

scanning:
  seed_symbols: ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
  shortlist_size: 15                 # smaller universe = less noise

notifications:
  telegram:
    enabled: true
```

**4. Initialize database**

```bash
pip install -e .
PYTHONPATH=src python -c "
import asyncio
from yolovest.data.db import Database
async def init():
    db = Database('data/yolovest.db')
    await db.initialize()
    await db.close()
asyncio.run(init())
"
```

**5. Run tests** to verify setup: `PYTHONPATH=src python -m pytest tests/ -v`

---

### Days 1–2: Paper Trading

**Goal:** Validate end-to-end without real money.

| Time (IST) | What | How |
|------------|------|-----|
| **8:45 AM** | Start YoloVest | `PYTHONPATH=src python -m yolovest.main --config config.yaml --mode paper` |
| **9:00 AM** | Authenticate Kite | Bot sends Telegram reminder. Go to Kite login URL, copy `request_token`, send `/auth <token>` to Telegram bot |
| **9:15 AM** | Market opens | System auto-runs heartbeat pipeline. Watch Telegram for alerts |
| **During market** | Monitor | Check dashboard at `http://localhost:8080` |
| **3:15 PM** | Auto square-off | MIS positions closed automatically |
| **4:00 PM** | Daily report | Arrives on Telegram — check trades, PnL, slippage |
| **Evening** | Dashboard review | Check `/api/trades`, `/api/predictions`, `/api/watchlist` |

**What to watch for:**
- Zero signals? Lower `min_confidence_score` to 0.65
- LLM reviews working? Check `/api/llm-accuracy`
- Any errors in console output?

**End of Day 2 checklist:**
- [ ] Heartbeat cycles ran without crashes
- [ ] Paper trades executed
- [ ] Telegram alerts arriving
- [ ] Dashboard shows data
- [ ] Kill switch works (`/stop` → `/resume`)

---

### Days 3–7: Go Live (Week 1)

Change `mode: live` in config.yaml and restart.

```bash
PYTHONPATH=src python -m yolovest.main --config config.yaml
# Or: docker compose up -d
```

**Daily routine (30 seconds + reading the report):**

| Time | Action |
|------|--------|
| **9:00 AM** | `/auth <token>` via Telegram (**critical — no auth = no trades**) |
| **9:15–3:30** | Let it run, monitor Telegram |
| **4:00 PM** | Read daily report |
| **Evening** | 5-min dashboard review |

**Risk guardrails protecting your ₹25k:**

| Guardrail | Trigger | Effect |
|-----------|---------|--------|
| Per-trade risk | ₹500 (2%) | Max loss per trade |
| Daily circuit breaker | ₹750 loss (3%) | Stops new trades for the day |
| Weekly circuit breaker | ₹1,250 loss (5%) | Halves position sizes rest of week |
| Kill switch | `/kill` command | Squares off everything immediately |
| Max exposure | ₹15,000 (60%) | Always keeps ₹10,000 in cash |
| Mandatory SL | Every trade | No trade without a stop-loss |

---

### Days 8–14: First Retrain + Tuning (Week 2)

**Saturday (Day 8) — automatic events:**
1. XGBoost retrains on accumulated data (6 AM, per `retraining.schedule_cron`)
2. New model enters **shadow mode** for 7 days (runs alongside current model, doesn't trade)
3. Gemini analyzes prediction failures (if 5+ failures exist)
4. Weekly report generated (10 AM Saturday)

**What you do:**
1. Read the weekly report (Telegram, Saturday 10 AM)
2. Check prediction scoreboard: `/api/predictions`
3. Review LLM accuracy: `/api/llm-accuracy`

**Mid-week tuning (if needed):**

If win rate < 40% or frequent daily losses — tighten:
```yaml
risk:
  min_confidence_score: 0.75
  max_trades_per_day: 3
  loss_cooldown_minutes: 30
```

If too conservative (zero trades most days) — loosen:
```yaml
risk:
  min_confidence_score: 0.60
  max_trades_per_day: 8
scanning:
  shortlist_size: 25
```

---

### Days 15–21: Shadow Model Promotion (Week 3)

**Saturday (Day 15) — automatic events:**
1. Week 1's shadow model completes its 7-day trial
2. Compared against active model on Sharpe ratio
3. **Auto-promoted** if better, **auto-retired** if worse
4. New retrain produces another shadow model

**What you do:**
1. Read weekly report — focus on cumulative PnL trend, best/worst trades, LLM accuracy
2. If cumulative loss > ₹2,500 (10%): pause live (`/stop`), go back to paper, tighten params
3. Consider tuning scan weights based on what's working:

```yaml
scanning:
  weights:
    technical: 0.45        # increase if technical signals are winning
    volume_momentum: 0.25
    news_sentiment: 0.15   # decrease if sentiment trades underperform
    fundamental: 0.15
```

---

### Days 22–30: Steady State (Week 4)

**Saturday (Day 22):** Third retrain. Model now has 3+ weeks of live data — retrains are meaningful.

**End-of-month review:**

| Metric | Where | Good | Concerning |
|--------|-------|------|------------|
| Cumulative PnL | Equity curve | > ₹0 | < -₹2,500 |
| Win rate | Daily/weekly reports | > 45% | < 35% |
| Prediction accuracy | `/api/predictions` | > 55% | < 45% |
| Max drawdown | Equity curve | < 10% | > 15% |
| LLM review value | `/api/llm-accuracy` | Approvals profitable | Random |
| Slippage | `/api/slippage` | < 0.2% avg | > 0.5% |

**Month-end decisions:**

| Outcome | Action |
|---------|--------|
| Profitable, consistent | Increase capital to ₹50k, keep same risk % |
| Profitable but volatile | Keep ₹25k, tighten `daily_loss_limit_pct` to 0.02 |
| Breakeven | Keep running — model needs more data |
| Losing < 10% | Tighten confidence to 0.75, reduce to 1 max position |
| Losing > 10% | Back to paper mode, retrain, review strategy |

---

### Key Dates Summary

| Day | Event |
|-----|-------|
| 0 | Setup: API keys, config, DB init, test run |
| 1–2 | Paper trading — validate everything works |
| 3 | **Go live** — switch `mode: live`, restart |
| 8 (Sat) | First auto-retrain + first weekly report |
| 15 (Sat) | Shadow model promotion + second retrain |
| 22 (Sat) | Third retrain — model has meaningful data |
| 30 | Full month review — decide on capital increase |

### Time Commitment

- **Daily:** ~30 seconds (Kite auth) + reading the daily report
- **Weekly:** ~10 minutes (review weekly report + dashboard)
- **Everything else is fully autonomous**

### Emergency Quick Reference

```
/stop    →  Pause new trades (positions stay open)
/kill    →  Close everything NOW
/resume  →  Resume after stop/kill
```

## License

MIT
