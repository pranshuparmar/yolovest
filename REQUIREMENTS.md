# YoloVest — Requirements Document (v1.0)

## Context

Building a **fully autonomous AI-driven Indian stock trading platform** for personal use. The platform will operate on a "YOLO" basis — zero human intervention for trade decisions — while maintaining full transparency through reports, dashboards, and Telegram alerts. It uses **OpenClaw** as the autonomous agent orchestration layer, **Gemini API** (Google AI Pro) for LLM reasoning, ML models for signal generation, and **Zerodha Kite Connect (free personal tier)** for execution. Market data is sourced from **free external providers** (jugaad-data, yfinance, tvDatafeed) to keep the platform self-sufficient with zero recurring costs.

**Starting capital:** Under ₹1L, conservative risk appetite (max 2% per trade).
**Trading style:** Primarily intraday and swing (2-10 days), with flexibility for positional.
**Stock universe:** Dynamic — bot scans and picks stocks daily.

---

## 1. Functional Requirements

### FR-1: OpenClaw Agent Orchestration

| ID | Requirement | Priority |
|----|------------|----------|
| FR-1.1 | Deploy OpenClaw as the autonomous orchestration layer — the bot runs 24/7 as an always-on agent | P0 |
| FR-1.2 | Configure OpenClaw heartbeat to monitor system health, check positions, and trigger scheduled tasks. Intervals configurable via `heartbeat.market_hours_interval_min` (default: `15`) and `heartbeat.off_hours_interval_min` (default: `60`) | P0 |
| FR-1.3 | Implement 14 OpenClaw Skills (see `src/yolovest/skills/`). Each skill is a discrete, independently invocable capability: | P0 |

**Skill Manifest:**

| Skill | Trigger | Schedule | Covers FRs | Description |
|-------|---------|----------|-----------|-------------|
| `auth-broker` | CRON | `0 9 * * 1-5` (9 AM weekdays) | FR-6.3 | Daily Kite re-authentication |
| `ingest-data` | HEARTBEAT | — | FR-2.1-2.9, 2.12-2.13 | OHLCV + news + fundamentals + sentiment |
| `ingest-premarket` | CRON | `30 8 * * 1-5` (8:30 AM) | FR-2.10-2.11 | GIFT Nifty, global cues, Gemini web summary |
| `market-scan` | HEARTBEAT | — | FR-3.1-3.6 | Scan NSE, rank stocks, produce watchlist |
| `generate-signals` | HEARTBEAT | — | FR-4.1-4.5 | ML feature eng → buy/sell/hold signals |
| `risk-check` | EVENT | per signal | FR-5.1-5.18 | Validate signal against all risk rules |
| `llm-review` | EVENT | per signal | FR-4.4, FR-5.11-5.12 | Gemini trade approval gate |
| `trade-execute` | EVENT | per approved signal | FR-6.1-6.2, 6.4-6.9 | Place orders (paper or live) |
| `position-monitor` | HEARTBEAT | — | FR-6.5, FR-5.8 | Reconcile, trail SLs, track PnL |
| `square-off` | CRON | `market_hours.square_off` | FR-5.9-5.10 | Auto close intraday positions |
| `predict-track` | EVENT + HEARTBEAT | — | FR-7.1-7.3 | Log predictions, score outcomes |
| `model-retrain` | CRON | `retraining.schedule_cron` | FR-7.4-7.7 | Retrain ML, version, A/B test |
| `report-generate` | CRON | daily + weekly crons | FR-8.4-8.6 | Daily/weekly reports + Telegram |
| `health-check` | HEARTBEAT | — | FR-1.2, FR-1.6 | System health, crash recovery |
| `kill-switch` | MANUAL | Telegram `/stop` `/kill` `/resume` | FR-5.14-5.15 | Emergency stop/kill/resume |

**Skill execution order per heartbeat (market hours):**
```
health-check → ingest-data → market-scan → generate-signals
  → [per signal]: risk-check → llm-review → trade-execute → predict-track
  → position-monitor
```
| FR-1.4 | Use OpenClaw's messaging integration for Telegram — trade alerts, daily summaries, error notifications | P0 |
| FR-1.5 | OpenClaw Memory — persist agent state, trading context, conversation history, and reasoning across restarts using OpenClaw's Markdown-based memory system | P1 |
| FR-1.6 | Implement graceful degradation — if OpenClaw agent crashes, positions are protected (no open orders left hanging), and agent auto-restarts | P0 |

### FR-2: Market Data Ingestion & News Intelligence

**Data Source Strategy:** Use free external providers for all market data. Zerodha Kite Connect free tier is used for execution only (no market data). The ₹500/month Kite data plan is an optional upgrade for real-time streaming, not a dependency.

| ID | Requirement | Priority |
|----|------------|----------|
| FR-2.1 | **Data source abstraction layer** (`MarketDataBase` ABC) — pluggable providers behind a unified interface, with automatic fallback chain | P0 |
| FR-2.1a | **Primary (Daily/EOD):** `jugaad-data` — scrapes NSE directly, actively maintained, built-in caching. History from 2013+. | P0 |
| FR-2.1b | **Fallback (Daily/EOD):** `yfinance` — Yahoo Finance via `.NS` suffix. 20 years history. Fragile rate limits, use as backup only. | P0 |
| FR-2.1c | **Intraday (5min/15min):** `tvDatafeed` — TradingView unofficial API. Free tier: 5min bars, last 15 days. Sufficient for POC intraday signals. | P1 |
| FR-2.1d | **Seed data:** One-time download of NSE Bhavcopy CSVs for deep historical backtesting (2013+). | P1 |
| FR-2.1e | **Optional upgrade:** Kite Connect data plan (₹500/month) for real-time streaming + full historical via `kite.historical_data()`. Architecture must support this as a drop-in provider. | P2 |
| FR-2.2 | Ingest NSE/BSE official data: corporate announcements, bulk/block deals, FII/DII activity, delivery data | P0 |
| FR-2.3 | Scrape/fetch news from MoneyControl, Economic Times Markets, LiveMint — headlines, analyst ratings, target prices | P1 |
| FR-2.4 | Fetch fundamental data from Screener.in — PE, PB, debt ratios, quarterly results, promoter holdings | P1 |
| FR-2.5 | Fetch technical screener data from Trendlyne — momentum scores, volume breakouts, technical signals | P1 |
| FR-2.6 | Ingest economic calendar events (RBI policy, US Fed, GDP data, earnings dates) | P1 |
| FR-2.7 | Use Gemini API to summarize and extract actionable sentiment from raw news data — classify as bullish/bearish/neutral per symbol | P0 |
| FR-2.8 | Store all ingested data in SQLite with timestamps for historical reference | P0 |
| FR-2.9 | Rate-limit all data fetching to respect source APIs and avoid IP bans | P0 |
| FR-2.10 | Fetch pre-market data (SGX Nifty/GIFT Nifty, global indices, overnight US market moves) before market open | P1 |
| FR-2.11 | Use Gemini with web grounding to search and summarize real-time market news beyond what RSS/APIs provide | P0 |
| FR-2.12 | Fetch Google Finance data for broader market sentiment and global cues | P1 |
| FR-2.13 | Aggregate all news sources with deduplication — same news from multiple sources should be merged, not double-counted in sentiment | P1 |

### FR-3: Dynamic Stock Scanning & Ranking

| ID | Requirement | Priority |
|----|------------|----------|
| FR-3.1 | Daily pre-market scan: scan NSE universe and shortlist candidates based on momentum, volume, and news. Shortlist size configurable via `scanning.shortlist_size` (default: `25`) | P0 |
| FR-3.2 | Stock ranking algorithm with configurable weights via `scanning.weights`: `technical` (default: `0.40`), `volume_momentum` (default: `0.25`), `news_sentiment` (default: `0.20`), `fundamental` (default: `0.15`). Must sum to 1.0. | P0 |
| FR-3.3 | Filter out illiquid stocks below `scanning.min_avg_daily_volume` (default: `500000`), stocks in F&O ban period, and stocks with pending corporate actions | P0 |
| FR-3.4 | Maintain a dynamic watchlist that updates throughout the day as new data comes in | P1 |
| FR-3.5 | Track sector rotation — identify which sectors are showing strength/weakness | P2 |
| FR-3.6 | Use Gemini to cross-validate top-ranked stocks against current market narrative | P1 |

### FR-4: Strategy Engine (ML + LLM)

| ID | Requirement | Priority |
|----|------------|----------|
| FR-4.1 | Feature engineering: RSI, MACD, Bollinger Bands, VWAP, ATR, volume profile, OBV, SuperTrend, moving averages. EMA periods configurable via `strategy.ema_periods` (default: `[9, 21, 50, 200]`). Individual indicators toggleable via `strategy.indicators` map. | P0 |
| FR-4.2 | XGBoost/LightGBM model trained on historical features to generate buy/sell/hold signals with confidence scores | P0 |
| FR-4.3 | Separate models for intraday (short features, 1-5min candles) and swing (daily features, multi-day patterns) | P0 |
| FR-4.4 | Gemini-powered trade review: before every trade, send full context (signal, indicators, news sentiment, portfolio state, market conditions) to Gemini for approval/rejection/resize recommendation | P0 |
| FR-4.5 | Signal must include: entry price, target price, stop-loss price, position size, expected holding period, confidence score | P0 |
| FR-4.6 | Backtesting engine: simulate strategies on historical data, compute Sharpe ratio, max drawdown, win rate, profit factor | P0 |
| FR-4.7 | Walk-forward validation: backtest must use rolling train/test windows (no lookahead bias) | P1 |

### FR-5: Risk Management

**All risk parameters are configurable via `risk` section in config.yaml. Defaults shown below are conservative starting values.**

| ID | Requirement | Config Key | Default | Priority |
|----|------------|-----------|---------|----------|
| FR-5.1 | Max risk per trade as % of total capital | `risk.max_risk_per_trade_pct` | `0.02` (2%) | P0 |
| FR-5.2 | Max total portfolio exposure as % of capital (rest stays in cash) | `risk.max_portfolio_exposure_pct` | `0.60` (60%) | P0 |
| FR-5.3 | Max simultaneous open positions | `risk.max_open_positions` | `3` | P0 |
| FR-5.4 | Max single stock exposure as % of portfolio | `risk.max_single_stock_pct` | `0.25` (25%) | P0 |
| FR-5.5 | Daily loss circuit breaker — stop all trading if daily loss exceeds this % of capital | `risk.daily_loss_limit_pct` | `0.03` (3%) | P0 |
| FR-5.6 | Weekly loss circuit breaker — reduce position sizing by `weekly_loss_sizing_reduction` if weekly loss exceeds this % | `risk.weekly_loss_limit_pct` | `0.05` (5%) | P0 |
| FR-5.6a | Position sizing reduction factor when weekly circuit breaker triggers | `risk.weekly_loss_sizing_reduction` | `0.50` (50%) | P0 |
| FR-5.7 | Mandatory stop-loss on every trade (no exceptions). SL must be set at order time. Enable/disable enforcement. | `risk.mandatory_stop_loss` | `true` | P0 |
| FR-5.8 | Trailing stop-loss: once trade profit exceeds `trailing_sl_trigger_multiple` × risk, trail SL to breakeven and continue trailing | `risk.trailing_sl_enabled` | `true` | P1 |
| FR-5.8a | Profit multiple of risk at which trailing SL activates | `risk.trailing_sl_trigger_multiple` | `1.5` | P1 |
| FR-5.8b | Trailing SL step size as % of price (how tightly it trails) | `risk.trailing_sl_step_pct` | `0.005` (0.5%) | P1 |
| FR-5.9 | Market hours enforcement — no new orders outside configured window | `market_hours.order_start` / `market_hours.order_end` | `09:15` / `15:15` | P0 |
| FR-5.9a | Extended window for square-off orders after `order_end` | `market_hours.square_off_extension` | `"00:05"` (5 min) | P0 |
| FR-5.10 | Auto square-off all intraday (MIS) positions at configured time. Swing (CNC) held overnight. | `market_hours.square_off` | `15:15` | P0 |
| FR-5.11 | Gemini risk review — LLM second opinion before execution, can veto trades | `risk.llm_review_enabled` | `true` | P0 |
| FR-5.12 | If Gemini API is down, fall back to rules-only risk management (never block trading on LLM availability) | `risk.llm_fallback_to_rules` | `true` | P0 |
| FR-5.13 | Correlation check — max positions allowed in same sector simultaneously | `risk.max_same_sector_positions` | `1` | P2 |
| FR-5.14 | **Emergency kill switch**: Telegram commands (`/stop` to pause, `/kill` to square off everything) + dashboard button | `risk.kill_switch_enabled` | `true` | P0 |
| FR-5.15 | Kill switch state persists across restarts — stays paused until `/resume` | `risk.kill_switch_persistent` | `true` | P0 |
| FR-5.16 | Minimum confidence score from ML model required to take a trade | `risk.min_confidence_score` | `0.65` | P0 |
| FR-5.17 | Max number of trades per day (to avoid overtrading) | `risk.max_trades_per_day` | `10` | P1 |
| FR-5.18 | Cooldown period (minutes) after a losing trade before next entry | `risk.loss_cooldown_minutes` | `15` | P2 |

### FR-6: Order Execution

| ID | Requirement | Priority |
|----|------------|----------|
| FR-6.1 | Execute via Zerodha Kite Connect API: support Market, Limit, SL, and SL-M order types | P0 |
| FR-6.2 | Paper trading mode by default — live trading requires explicit config flag | P0 |
| FR-6.3 | Handle Kite daily re-authentication: generate `request_token` → `access_token` flow each morning (manual TOTP or automated via Selenium as fallback) | P0 |
| FR-6.4 | Order lifecycle tracking: placed → open → filled/rejected/cancelled — with timestamps | P0 |
| FR-6.5 | Reconcile local position state with broker state periodically (every heartbeat) | P0 |
| FR-6.6 | Retry failed orders with exponential backoff. Max retries configurable via `execution.max_order_retries` (default: `3`), base delay via `execution.retry_base_delay_sec` (default: `2`) | P1 |
| FR-6.7 | Slippage tracking: record expected vs actual fill price for every trade | P1 |
| FR-6.8 | Respect Kite API rate limits: 10 req/s aggregate across all endpoints per API key | P0 |
| FR-6.9 | Respect external data source rate limits: jugaad-data (use built-in caching), yfinance (add delays between requests), tvDatafeed (respect TradingView limits) | P0 |

### FR-7: Prediction Tracking & Self-Learning

| ID | Requirement | Priority |
|----|------------|----------|
| FR-7.1 | Log every prediction: symbol, predicted direction, confidence, predicted target, predicted timeframe | P0 |
| FR-7.2 | After the predicted timeframe elapses, record actual outcome: actual price movement, actual PnL | P0 |
| FR-7.3 | Maintain a prediction scoreboard: accuracy by symbol, by strategy, by market condition, by timeframe | P0 |
| FR-7.4 | Scheduled model retraining using accumulated prediction vs actual data. Schedule configurable via `retraining.schedule_cron` (default: `"0 6 * * 6"` — Saturday 6 AM) | P0 |
| FR-7.5 | A/B testing: new model runs in shadow mode alongside current model before promoting. Shadow duration configurable via `retraining.shadow_mode_days` (default: `7`) | P1 |
| FR-7.6 | Use Gemini to analyze prediction failures — identify patterns in what the model gets wrong | P1 |
| FR-7.7 | Version all model artifacts with metrics (accuracy, Sharpe, drawdown) so we can rollback if a new model underperforms | P0 |
| FR-7.8 | Track Gemini's own trade review accuracy — did its approvals/rejections lead to better outcomes? | P2 |

### FR-8: Reporting & Dashboard

| ID | Requirement | Priority |
|----|------------|----------|
| FR-8.1 | FastAPI web dashboard: portfolio overview, open positions, today's trades, PnL chart, equity curve | P0 |
| FR-8.2 | WebSocket live updates: push trade executions and position changes in real time | P1 |
| FR-8.3 | Trade detail view: for any trade, show the full reasoning chain (ML signal → Gemini review → risk check → execution → outcome) | P0 |
| FR-8.4 | Daily report: auto-generated at market close — today's trades, PnL, prediction accuracy, top signals, market summary. Time configurable via `reports.daily_report_time` (default: `"16:00"`) | P0 |
| FR-8.5 | Weekly report: cumulative PnL, model performance trends, prediction accuracy trends, Gemini review analysis. Day/time configurable via `reports.weekly_report_cron` (default: `"0 10 * * 6"` — Saturday 10 AM) | P0 |
| FR-8.6 | Telegram integration: real-time trade alerts (entry/exit), daily summary, weekly summary. Alert types individually toggleable via `notifications.telegram.alerts` map (`trade_entry`, `trade_exit`, `daily_summary`, `weekly_summary`, `errors`) | P0 |
| FR-8.7 | Historical report access: query any date range for full trade history and performance metrics | P1 |
| FR-8.8 | Audit log: every action (data fetch, signal, risk check, LLM call, order, fill) logged with timestamp and full context | P0 |
| FR-8.9 | Dashboard auth: basic password protection (single user, personal use) | P1 |

---

## 2. Non-Functional Requirements

| ID | Requirement |
|----|------------|
| NFR-1 | **Reliability**: Bot must recover from crashes without manual intervention. OpenClaw heartbeat detects failures and restarts. |
| NFR-2 | **Latency**: Trade execution pipeline (signal → risk check → order) must complete within configurable threshold `execution.max_pipeline_latency_sec` (default: `2`). |
| NFR-3 | **Data integrity**: All market data, predictions, and trade records must be persisted to SQLite with WAL mode for concurrent access. |
| NFR-4 | **Security**: API keys stored as environment variables, never in code or config files. Docker deployment with restricted network access. |
| NFR-5 | **Auditability**: Every decision point must be logged. Full reconstruction of any trade's reasoning chain must be possible. |
| NFR-6 | **Cost efficiency**: Optimize Gemini API calls — batch where possible, use Flash model for routine checks, Pro model only for complex analysis. |
| NFR-7 | **Deployment**: Docker Compose for single-command deployment. Must run on a small VPS (2GB RAM, 2 vCPU). |
| NFR-8 | **Testability**: Every component must be testable in isolation. Paper trading must be indistinguishable from live trading except for actual order execution. |

---

## 3. Resolved Decisions

| Question | Decision |
|----------|----------|
| **Zerodha Auth** | Semi-automated — bot sends Telegram reminder at 9 AM, user pastes request_token. ~30 seconds daily. |
| **F&O Trading** | Equity (cash segment) only for now. Architecture designed to support F&O in future. |
| **Starting Mode** | Paper trading for most strategies + tiny live trades (₹5-10K) to test real execution pipeline. |
| **Hosting** | Decide later — Docker Compose makes it portable. Build first, deploy when ready. |
| **Intraday Square-off** | Auto square-off at 3:15 PM IST (configurable via config.yaml). Swing positions use CNC order type and are held overnight. |
| **News Sources** | Use ALL available sources — RSS feeds, NSE/BSE official APIs, Google News API, Gemini web grounding, Screener.in, Trendlyne. Maximize coverage. |
| **Kill Switch** | Both Telegram (`/stop`, `/kill`) and dashboard button. Immediately cancels all open orders and optionally squares off all positions. |
| **Market Data** | Free external sources only (jugaad-data, yfinance, tvDatafeed). Zero recurring cost. Kite ₹500/month data plan is optional upgrade, not a dependency. |

All open questions have been resolved. No remaining blockers for implementation.

---

## 4. Legal & Regulatory Notes (India)

| Area | Status |
|------|--------|
| Algo trading for retail | **Legal** — SEBI allows retail algo trading via broker APIs. No registration needed for personal use. |
| Tax implications | Short-term capital gains (STCG) at 20% for equity held < 1 year. Intraday profits taxed as speculative business income. |
| Audit requirement | If turnover exceeds ₹10 crore (speculative) or ₹10 crore (non-speculative), tax audit is mandatory. Unlikely at ₹1L capital. |
| Zerodha API ToS | Automated login (Selenium) may violate ToS. API usage for trading is explicitly allowed. |
| Data scraping | Scraping NSE/BSE data may have restrictions. Use official APIs where available. jugaad-data uses built-in caching to minimize scraping impact. |
| Static IP requirement | SEBI retail algo trading regulations may require a static IP for API-based order placement. Monitor compliance requirements. |

---

## 5. Tech Stack (Final)

| Component | Choice |
|-----------|--------|
| Agent Orchestration | OpenClaw (model-agnostic, heartbeat, skills, Telegram integration) |
| Language | Python 3.12+ |
| Broker (execution) | Zerodha Kite Connect API (free personal tier — execution only) |
| Market Data | jugaad-data (primary) + yfinance (fallback) + tvDatafeed (intraday) |
| LLM (runtime) | Gemini API via Google AI Pro (trade review, sentiment, analysis) |
| LLM (development) | Claude Max (building, debugging, iterating) |
| ML | XGBoost + LightGBM + scikit-learn |
| Database | SQLite (WAL mode) + pandas |
| Dashboard | FastAPI + Jinja2 + HTMX |
| Messaging | Telegram (via OpenClaw) |
| Scheduling | OpenClaw heartbeat + APScheduler for fine-grained tasks |
| Config | YAML + pydantic |
| Deployment | Docker Compose |

---

## 6. Implementation Phases (High-Level)

### Phase 1: Foundation & Data Pipeline
- Project scaffold, config system, database schema
- Broker abstraction + Zerodha implementation
- LLM abstraction + Gemini implementation
- Market data abstraction (`MarketDataBase` ABC) + free providers (jugaad-data, yfinance, tvDatafeed)
- Feature engineering (technical indicators)

### Phase 2: Intelligence Layer
- News & data aggregation from multiple sources
- Gemini-powered sentiment analysis
- Dynamic stock scanner & ranking algorithm
- ML signal models (intraday + swing)
- Backtesting engine with walk-forward validation

### Phase 3: Risk & Execution
- Risk manager (all rules from FR-5)
- LLM trade review gate
- Order executor with paper trading mode
- Position tracking & reconciliation

### Phase 4: Self-Learning Loop
- Prediction tracking system
- Weekly model retraining pipeline
- Model versioning & A/B testing
- Prediction accuracy dashboard

### Phase 5: OpenClaw Integration
- Set up OpenClaw as the orchestration layer
- Implement Skills for each capability
- Heartbeat configuration
- Telegram messaging integration
- 24/7 autonomous operation

### Phase 6: Dashboard & Reporting
- FastAPI dashboard with live updates
- Daily/weekly auto-generated reports
- Telegram summaries
- Full audit trail viewer

---

## 7. Verification Plan

1. **Unit tests** for every module (features, risk rules, signal generation)
2. **Paper trading for 2+ weeks** before any live trading
3. **Backtest validation**: run strategies on 6 months of historical data, verify Sharpe > 1.0
4. **Prediction tracking validation**: verify prediction vs actual logging is accurate
5. **Gemini integration test**: verify trade review gate works, verify fallback when API is down
6. **OpenClaw health test**: kill the agent, verify heartbeat detects and restarts
7. **Telegram test**: verify all alert types (trade, daily summary, error) are delivered
8. **End-to-end paper run**: full autonomous day of paper trading with all systems active

---

## 8. Architecture

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

Broker Abstraction:      LLM Abstraction:         Market Data Abstraction:
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────────┐
│  BrokerBase      │←ABC │  LLMBase         │←ABC │  MarketDataBase      │←ABC
├──────────────────┤     ├──────────────────┤     ├──────────────────────┤
│  ZerodhaBroker   │     │  GeminiLLM       │     │  JugaadDataProvider  │ ← primary
│  (future) IBBrkr │     │  (future) others │     │  YFinanceProvider    │ ← fallback
└──────────────────┘     └──────────────────┘     │  TVDatafeedProvider  │ ← intraday
                                                   │  (opt) KiteProvider  │ ← paid upgrade
                                                   └──────────────────────┘
```

### LLM Abstraction Interface

```python
class LLMBase(ABC):
    @abstractmethod
    async def review_trade(self, context: TradeContext) -> TradeReview: ...

    @abstractmethod
    async def analyze_sentiment(self, symbol: str, headlines: list[str]) -> SentimentResult: ...
```

---

## 9. Project Structure

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
│       │   ├── base.py         # MarketDataBase ABC (data source abstraction)
│       │   ├── jugaad.py       # jugaad-data provider (primary, daily/EOD)
│       │   ├── yfinance.py     # yfinance provider (fallback, daily)
│       │   ├── tvfeed.py       # tvDatafeed provider (intraday 5m/15m)
│       │   ├── bhavcopy.py     # NSE Bhavcopy CSV importer (seed data)
│       │   ├── ingester.py     # Orchestrates data fetching with fallback chain
│       │   ├── features.py     # Feature engineering (indicators, derived)
│       │   └── db.py           # SQLite read/write, migrations
│       ├── strategy/
│       │   ├── __init__.py
│       │   ├── ml_signal.py    # XGBoost/sklearn signal model
│       │   └── backtest.py     # Backtesting engine
│       ├── skills/
│       │   ├── __init__.py         # Skill registry (SKILL_REGISTRY dict)
│       │   ├── base.py             # SkillBase ABC, SkillResult, SkillTrigger
│       │   ├── auth_broker.py      # Daily Kite re-authentication
│       │   ├── ingest_data.py      # OHLCV + news + fundamentals + sentiment
│       │   ├── ingest_premarket.py  # Pre-market global cues
│       │   ├── market_scan.py      # NSE scan, rank, filter → watchlist
│       │   ├── generate_signals.py  # ML feature eng → trade signals
│       │   ├── risk_check.py       # Validate against all risk rules
│       │   ├── llm_review.py       # Gemini trade approval gate
│       │   ├── trade_execute.py    # Place orders (paper/live)
│       │   ├── position_monitor.py  # Reconcile, trail SLs, track PnL
│       │   ├── square_off.py       # Auto close intraday positions
│       │   ├── predict_track.py    # Log predictions, score outcomes
│       │   ├── model_retrain.py    # Retrain ML, version, A/B test
│       │   ├── report_generate.py  # Daily/weekly reports + Telegram
│       │   ├── health_check.py     # System health, crash recovery
│       │   └── kill_switch.py      # Emergency stop/kill/resume
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

---

## 10. Config Example (config.yaml)

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

market_data:
  daily_provider: jugaad          # jugaad | yfinance
  daily_fallback: yfinance        # fallback if primary fails
  intraday_provider: tvdatafeed   # tvdatafeed | kite (paid)
  bhavcopy_dir: ./data/bhavcopy   # path to downloaded NSE Bhavcopy CSVs
  cache_ttl_minutes: 15           # cache fetched data to reduce API calls

heartbeat:
  market_hours_interval_min: 15   # heartbeat frequency during market hours
  off_hours_interval_min: 60      # heartbeat frequency outside market hours

scanning:
  seed_symbols: ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]  # initial watchlist (scanner expands dynamically)
  shortlist_size: 25              # number of candidate stocks from daily scan
  min_avg_daily_volume: 500000    # filter out stocks below this avg daily volume
  weights:                        # ranking algorithm weights (must sum to 1.0)
    technical: 0.40
    volume_momentum: 0.25
    news_sentiment: 0.20
    fundamental: 0.15

strategy:
  exchange: NSE
  interval: "5minute"             # default candle interval
  ema_periods: [9, 21, 50, 200]   # moving average periods
  indicators:                     # toggle individual indicators
    rsi: true
    macd: true
    bollinger_bands: true
    vwap: true
    atr: true
    volume_profile: true
    obv: true
    supertrend: true

risk:
  max_risk_per_trade_pct: 0.02        # 2% of capital per trade
  max_portfolio_exposure_pct: 0.60    # 60% max exposure, 40% always cash
  max_open_positions: 3               # simultaneous open positions
  max_single_stock_pct: 0.25          # 25% max in one stock
  daily_loss_limit_pct: 0.03          # stop trading if down 3% today
  weekly_loss_limit_pct: 0.05         # weekly circuit breaker at 5%
  weekly_loss_sizing_reduction: 0.50  # reduce sizing by 50% when weekly breaker hits
  mandatory_stop_loss: true           # enforce SL on every trade
  trailing_sl_enabled: true           # enable trailing stop-loss
  trailing_sl_trigger_multiple: 1.5   # activate trailing SL at 1.5x risk in profit
  trailing_sl_step_pct: 0.005         # trail SL in 0.5% steps
  llm_review_enabled: true            # Gemini risk review before execution
  llm_fallback_to_rules: true         # rules-only if LLM unavailable
  max_same_sector_positions: 1        # correlation check — max positions per sector
  kill_switch_enabled: true           # enable /stop and /kill commands
  kill_switch_persistent: true        # kill switch survives restarts
  min_confidence_score: 0.65          # minimum ML confidence to take a trade
  max_trades_per_day: 10              # max trades per day (anti-overtrading)
  loss_cooldown_minutes: 15           # wait after a losing trade before next entry

market_hours:
  open: "09:15"
  close: "15:30"
  order_start: "09:15"           # earliest time for new orders
  order_end: "15:15"             # latest time for new orders
  square_off: "15:15"            # auto square-off intraday positions
  square_off_extension: "00:05"  # extra window for square-off orders after order_end
  timezone: "Asia/Kolkata"

execution:
  max_order_retries: 3            # retry failed orders this many times
  retry_base_delay_sec: 2         # exponential backoff base (2s, 4s, 8s)
  max_pipeline_latency_sec: 2     # signal→risk→order must complete within this

retraining:
  schedule_cron: "0 6 * * 6"     # retrain schedule (Saturday 6 AM)
  shadow_mode_days: 7             # run new model in shadow for N days before promoting

reports:
  daily_report_time: "16:00"      # daily report generation time (IST)
  weekly_report_cron: "0 10 * * 6"  # weekly report (Saturday 10 AM)

dashboard:
  host: "0.0.0.0"
  port: 8080

notifications:
  telegram:
    enabled: false
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
    alerts:                       # toggle individual alert types
      trade_entry: true
      trade_exit: true
      daily_summary: true
      weekly_summary: true
      errors: true
      kill_switch: true           # alert when kill switch is triggered
```

---

## 11. API & Broker Notes

### Zerodha Kite Connect

- **Tier**: Free personal API (Kite Connect Personal) — execution APIs only, no market data.
- **Authentication**: OAuth-style login flow that generates a `request_token` daily. Bot needs daily re-auth (manual TOTP or automated).
- **Order types**: Market, Limit, SL, SL-M supported.
- **Rate limits**: 10 requests/second aggregate across all endpoints per API key.
- **Paid upgrade (optional)**: ₹500/month adds real-time streaming (Kite Ticker WebSocket) + historical OHLCV via `kite.historical_data()`. Not needed for POC.
- **Static IP**: May be required per SEBI retail algo trading regulations (monitor).

### Free Market Data Sources

- **jugaad-data** (primary, daily/EOD): Scrapes NSE directly, built-in caching, actively maintained (last update March 2026). History from 2013+.
- **yfinance** (fallback, daily): Yahoo Finance via `.NS` suffix. 20 years history. Fragile — rate-limited unpredictably. Use as backup.
- **tvDatafeed** (intraday): Unofficial TradingView API. Free tier: 5min bars, last 15 days. Multiple timeframes (1m, 5m, 15m, daily).
- **NSE Bhavcopy CSVs** (seed data): Free download from NSE reports section. Daily EOD from 2013+. One-time bulk import for backtesting.

### Gemini API (Google AI Pro)

- **Quota**: Baseline quota included with AI Pro subscription; AI credits consumed after baseline is exhausted.
- **Models**: Gemini 2.5 Pro (best reasoning) or Gemini 2.5 Flash (faster, cheaper quota usage).
- **Rate limits**: Varies by model and tier; implement exponential backoff.
- **API Key**: Generate from Google AI Studio (aistudio.google.com).

---

## 12. Getting Started

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
