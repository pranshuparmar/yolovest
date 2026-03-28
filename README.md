# YoloVest

Fully autonomous AI-driven Indian stock trading platform. Uses Google Gemini for LLM reasoning, XGBoost for ML signals, and Zerodha Kite Connect for execution. Market data comes from free providers (jugaad-data, yfinance, tvDatafeed) with optional paid Kite data plan.

**Paper trading by default** — live trading requires explicit opt-in.

## Features

- **16-skill heartbeat pipeline** — health check, data ingestion, market scanning, signal generation, risk checks, LLM trade review, execution, position monitoring, square-off, prediction tracking, model retraining, reporting, and database maintenance
- **ML signal models** — XGBoost with Platt scaling, walk-forward backtesting, real shadow A/B testing with live prediction comparison
- **Gemini LLM integration** — sentiment analysis, trade review gate, watchlist validation, market summaries (toggleable via `llm.enabled`)
- **Full risk management** — position sizing, exposure caps, daily/weekly circuit breakers, trailing stop-loss, kill switch, margin enforcement, symbol cooldown
- **Transaction cost modeling** — brokerage, STT, stamp duty, GST deducted from all PnL calculations (paper and live)
- **Multi-source market data** — jugaad-data (primary) → yfinance (fallback) → tvDatafeed (intraday), with automatic failover and staleness detection
- **Symbol quarantine** — auto-blocks symbols after 3 consecutive fetch failures, excluded from all pipelines until manually unblocked
- **Zerodha Kite Connect** — paper mode with simulated slippage + costs, live mode with LIMIT→MARKET fallback, SL orders, and price drift rejection
- **Web dashboard** — React SPA with real-time WebSocket updates, dry-run signal preview, model management, quarantine management, full trade reasoning chain
- **Telegram bot** — real-time entry/exit alerts, portfolio status, kill switch control, per-alert-type toggles
- **Self-learning** — prediction tracking/scoring, automated model retraining with live shadow A/B testing, failure analysis

## Requirements

- Python 3.11+
- Node.js 18+ (for frontend build)
- SQLite (bundled with Python)
- API keys (optional, falls back to stubs if not set):
  - [Zerodha Kite Connect](https://kite.trade/) — for live/paper brokerage
  - [Google Gemini](https://ai.google.dev/) — for LLM features
  - [Telegram Bot](https://core.telegram.org/bots) — for notifications

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/pranshuparmar/yolovest.git
cd yolovest

# Create config
cp backend/config.example.yaml backend/config.yaml

# Create .env with secrets
cat > .env << 'EOF'
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
GEMINI_API_KEY=your_key
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
DOMAIN=yolovest.example.com
EOF

# Start
docker compose up -d

# View logs
docker compose logs -f backend
```

Dashboard at `https://your-domain` (or `http://localhost:8080` locally). Default password: `yolovest`.

### Local Development

```bash
# Backend
cd backend
pip install -e ".[dev]"
cp config.example.yaml config.yaml
PYTHONPATH=src python -m yolovest.main --config config.yaml

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

### Running Tests

```bash
cd backend
PYTHONPATH=src python -m pytest tests/ -v
```

## Configuration

Copy `config.example.yaml` to `config.yaml`. All secrets use `${VAR_NAME}` syntax — set them as environment variables or in `.env`.

### Service Toggles

External services can be independently enabled/disabled:

| Service | Config key | Default | What it controls |
|---------|-----------|---------|-----------------|
| Gemini LLM | `llm.enabled` | `false` | Sentiment analysis, trade review, failure analysis |
| News sources | `market_data.news_enabled` | `true` | MoneyControl, ET Markets, LiveMint RSS feeds |
| Scrapers | `market_data.scrapers_enabled` | `true` | Screener.in, Trendlyne, Google Finance, NSE, Economic Calendar |
| Kite data plan | `market_data.kite_data_enabled` | `false` | Paid Kite Connect historical data API |
| Telegram | `notifications.telegram.enabled` | `false` | Telegram bot and notifications |
| LLM review gate | `risk.llm_review_enabled` | `true` | Gemini trade approval (falls back to rules-only if LLM disabled) |

### Key Risk Parameters

```yaml
risk:
  max_risk_per_trade_pct: 0.02        # 2% of capital per trade
  max_portfolio_exposure_pct: 0.60    # 60% max exposure
  max_open_positions: 3               # simultaneous positions
  daily_loss_limit_pct: 0.03          # daily circuit breaker
  weekly_loss_limit_pct: 0.05         # weekly circuit breaker
  min_confidence_score: 0.65          # ML confidence threshold
  margin_usage_enabled: false         # false = no leverage (cash-only sizing)
  symbol_cooldown_days: 1             # hard block after last trade
  symbol_repeat_min_confidence: 0.80  # elevated threshold for recent symbols
```

See `config.example.yaml` for all options with inline documentation.

## Architecture

```
Heartbeat pipeline (every 15min during market hours):

health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → trade-execute → predict-track
  → position-monitor
```

### Abstraction Layers

- **BrokerBase** → `ZerodhaBroker` — order execution (paper + live)
- **LLMBase** → `GeminiLLM` — 7 methods (sentiment, trade review, summaries, etc.)
- **MarketDataBase** → `JugaadDataProvider` → `YFinanceProvider` → `TVDatafeedProvider` (fallback chain)
- **MLBase** → `XGBoostSignalModel` — with Platt scaling, shadow inference, model versioning

### ML Model Lifecycle

```
Saturday retrain → Shadow (7d live A/B testing) → Promote (if outperforms) → old model retires
                                                                              ↑
                                      Retired models can be re-shadowed manually ─┘
```

Shadow models run live predictions alongside production during every heartbeat. Promotion decisions are based on actual market performance (direction accuracy, target hit rate, avg PnL), not just training metrics.

### Project Structure

```
backend/
├── src/yolovest/
│   ├── broker/          # Zerodha Kite Connect integration
│   ├── dashboard/       # FastAPI REST API + WebSocket
│   ├── data/            # Database, market data providers, features
│   ├── llm/             # Gemini LLM integration
│   ├── models/          # Pydantic schemas
│   ├── news/            # RSS scrapers (MoneyControl, ET Markets, LiveMint)
│   ├── skills/          # 16 skills (pipeline stages)
│   ├── strategy/        # ML models (XGBoost), backtesting
│   ├── config.py        # Configuration with validation
│   ├── context.py       # AppContext + Protocol types
│   ├── costs.py         # Transaction cost computation (brokerage, STT, GST)
│   ├── orchestrator.py  # Heartbeat pipeline
│   └── main.py          # Entry point
├── migrations/          # SQL migration scripts (001-010)
├── tests/               # pytest-asyncio test suite
└── config.example.yaml  # Sample config with all keys documented

frontend/
├── src/
│   ├── pages/           # Dashboard, Trades, Positions, ML Models, Dry Run, etc.
│   ├── components/      # Reusable UI components
│   ├── hooks/           # React Query hooks, WebSocket, auth
│   └── api/             # API client with Basic Auth
├── Dockerfile
└── nginx.conf           # Reverse proxy to backend API
```

## Telegram Bot

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

## Domain Context

- **MIS** = Margin Intraday (auto-squared at EOD). **CNC** = Cash and Carry (delivery).
- Market hours: 9:15 AM - 3:30 PM IST. Square-off at 3:15 PM.
- Kite API requires daily re-authentication (user pastes request_token via Telegram or dashboard).
- All UI timestamps display in IST (`Asia/Kolkata`).

## License

MIT
