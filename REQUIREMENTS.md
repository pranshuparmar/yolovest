# YoloVest — Requirements Document (v1.0)

## Context

Building a **fully autonomous AI-driven Indian stock trading platform** for personal use. The platform will operate on a "YOLO" basis — zero human intervention for trade decisions — while maintaining full transparency through reports, dashboards, and Telegram alerts. It uses **OpenClaw** as the autonomous agent orchestration layer, **Gemini API** (Google AI Pro) for LLM reasoning, ML models for signal generation, and **Zerodha Kite Connect** for execution.

**Starting capital:** Under ₹1L, conservative risk appetite (max 2% per trade).
**Trading style:** Primarily intraday and swing (2-10 days), with flexibility for positional.
**Stock universe:** Dynamic — bot scans and picks stocks daily.

---

## 1. Functional Requirements

### FR-1: OpenClaw Agent Orchestration

| ID | Requirement | Priority |
|----|------------|----------|
| FR-1.1 | Deploy OpenClaw as the autonomous orchestration layer — the bot runs 24/7 as an always-on agent | P0 |
| FR-1.2 | Configure OpenClaw heartbeat (every 15 min during market hours, every 60 min outside) to monitor system health, check positions, and trigger scheduled tasks | P0 |
| FR-1.3 | Implement OpenClaw Skills for each capability: `market-scan`, `news-fetch`, `trade-execute`, `report-generate`, `model-retrain`, `risk-check` | P0 |
| FR-1.4 | Use OpenClaw's messaging integration for Telegram — trade alerts, daily summaries, error notifications | P0 |
| FR-1.5 | OpenClaw Memory — persist agent state, trading context, conversation history, and reasoning across restarts using OpenClaw's Markdown-based memory system | P1 |
| FR-1.6 | Implement graceful degradation — if OpenClaw agent crashes, positions are protected (no open orders left hanging), and agent auto-restarts | P0 |

### FR-2: Market Data Ingestion & News Intelligence

| ID | Requirement | Priority |
|----|------------|----------|
| FR-2.1 | Fetch OHLCV candle data from Zerodha Kite Connect (1min, 5min, 15min, daily intervals) | P0 |
| FR-2.2 | Ingest NSE/BSE official data: corporate announcements, bulk/block deals, FII/DII activity, delivery data | P0 |
| FR-2.3 | Scrape/fetch news from MoneyControl, Economic Times Markets, LiveMint — headlines, analyst ratings, target prices | P1 |
| FR-2.4 | Fetch fundamental data from Screener.in — PE, PB, debt ratios, quarterly results, promoter holdings | P1 |
| FR-2.5 | Fetch technical screener data from Trendlyne — momentum scores, volume breakouts, technical signals | P1 |
| FR-2.6 | Ingest economic calendar events (RBI policy, US Fed, GDP data, earnings dates) | P1 |
| FR-2.7 | Use Gemini API to summarize and extract actionable sentiment from raw news data — classify as bullish/bearish/neutral per symbol | P0 |
| FR-2.8 | Store all ingested data in SQLite with timestamps for historical reference | P0 |
| FR-2.9 | Rate-limit all data fetching to respect source APIs and avoid IP bans | P0 |
| FR-2.10 | Fetch pre-market data (SGX Nifty, global indices, overnight US market moves) before market open | P2 |

### FR-3: Dynamic Stock Scanning & Ranking

| ID | Requirement | Priority |
|----|------------|----------|
| FR-3.1 | Daily pre-market scan: scan entire NSE universe and shortlist 20-30 candidate stocks based on momentum, volume, and news | P0 |
| FR-3.2 | Stock ranking algorithm combining: technical score (40%), volume/momentum (25%), news sentiment (20%), fundamental quality (15%) | P0 |
| FR-3.3 | Filter out illiquid stocks (min avg daily volume threshold), stocks in ban period (F&O ban list), and stocks with pending corporate actions | P0 |
| FR-3.4 | Maintain a dynamic watchlist that updates throughout the day as new data comes in | P1 |
| FR-3.5 | Track sector rotation — identify which sectors are showing strength/weakness | P2 |
| FR-3.6 | Use Gemini to cross-validate top-ranked stocks against current market narrative | P1 |

### FR-4: Strategy Engine (ML + LLM)

| ID | Requirement | Priority |
|----|------------|----------|
| FR-4.1 | Feature engineering: RSI, MACD, Bollinger Bands, VWAP, ATR, volume profile, OBV, SuperTrend, moving averages (9/21/50/200 EMA) | P0 |
| FR-4.2 | XGBoost/LightGBM model trained on historical features to generate buy/sell/hold signals with confidence scores | P0 |
| FR-4.3 | Separate models for intraday (short features, 1-5min candles) and swing (daily features, multi-day patterns) | P0 |
| FR-4.4 | Gemini-powered trade review: before every trade, send full context (signal, indicators, news sentiment, portfolio state, market conditions) to Gemini for approval/rejection/resize recommendation | P0 |
| FR-4.5 | Signal must include: entry price, target price, stop-loss price, position size, expected holding period, confidence score | P0 |
| FR-4.6 | Backtesting engine: simulate strategies on historical data, compute Sharpe ratio, max drawdown, win rate, profit factor | P0 |
| FR-4.7 | Walk-forward validation: backtest must use rolling train/test windows (no lookahead bias) | P1 |

### FR-5: Risk Management

| ID | Requirement | Priority |
|----|------------|----------|
| FR-5.1 | Max risk per trade: 2% of total capital | P0 |
| FR-5.2 | Max total portfolio exposure: 60% of capital (40% always in cash) | P0 |
| FR-5.3 | Max open positions: 3 simultaneous | P0 |
| FR-5.4 | Max single stock exposure: 25% of portfolio | P0 |
| FR-5.5 | Daily loss circuit breaker: stop all trading if daily loss exceeds 3% of capital | P0 |
| FR-5.6 | Weekly loss circuit breaker: reduce position sizing by 50% if weekly loss exceeds 5% | P0 |
| FR-5.7 | Mandatory stop-loss on every trade (no exceptions). SL must be set at order time | P0 |
| FR-5.8 | Trailing stop-loss: once a trade is in profit by 1.5x the risk, trail the SL to breakeven; continue trailing as price moves favorably | P1 |
| FR-5.9 | Market hours enforcement: no orders outside 9:15 AM - 3:20 PM IST (3:20 not 3:30 to avoid closing auction volatility) | P0 |
| FR-5.10 | Auto square-off: close all intraday positions by 3:15 PM IST | P0 |
| FR-5.11 | Gemini risk review: LLM acts as a second opinion on risk before execution — can veto trades that look dangerous | P0 |
| FR-5.12 | If Gemini API is down, fall back to rules-only risk management (never block trading on LLM availability) | P0 |
| FR-5.13 | Correlation check: avoid opening multiple positions in the same sector/theme simultaneously | P2 |

### FR-6: Order Execution

| ID | Requirement | Priority |
|----|------------|----------|
| FR-6.1 | Execute via Zerodha Kite Connect API: support Market, Limit, SL, and SL-M order types | P0 |
| FR-6.2 | Paper trading mode by default — live trading requires explicit config flag | P0 |
| FR-6.3 | Handle Kite daily re-authentication: generate `request_token` → `access_token` flow each morning (manual TOTP or automated via Selenium as fallback) | P0 |
| FR-6.4 | Order lifecycle tracking: placed → open → filled/rejected/cancelled — with timestamps | P0 |
| FR-6.5 | Reconcile local position state with broker state periodically (every heartbeat) | P0 |
| FR-6.6 | Retry failed orders with exponential backoff (max 3 retries) | P1 |
| FR-6.7 | Slippage tracking: record expected vs actual fill price for every trade | P1 |
| FR-6.8 | Respect Kite API rate limits: 3 req/s for orders, 1 req/s for historical data | P0 |

### FR-7: Prediction Tracking & Self-Learning

| ID | Requirement | Priority |
|----|------------|----------|
| FR-7.1 | Log every prediction: symbol, predicted direction, confidence, predicted target, predicted timeframe | P0 |
| FR-7.2 | After the predicted timeframe elapses, record actual outcome: actual price movement, actual PnL | P0 |
| FR-7.3 | Maintain a prediction scoreboard: accuracy by symbol, by strategy, by market condition, by timeframe | P0 |
| FR-7.4 | Weekly model retraining: every Saturday, retrain ML models using accumulated prediction vs actual data | P0 |
| FR-7.5 | A/B testing: when a new model is trained, run it in shadow mode alongside the current model for 1 week before promoting | P1 |
| FR-7.6 | Use Gemini to analyze prediction failures — identify patterns in what the model gets wrong | P1 |
| FR-7.7 | Version all model artifacts with metrics (accuracy, Sharpe, drawdown) so we can rollback if a new model underperforms | P0 |
| FR-7.8 | Track Gemini's own trade review accuracy — did its approvals/rejections lead to better outcomes? | P2 |

### FR-8: Reporting & Dashboard

| ID | Requirement | Priority |
|----|------------|----------|
| FR-8.1 | FastAPI web dashboard: portfolio overview, open positions, today's trades, PnL chart, equity curve | P0 |
| FR-8.2 | WebSocket live updates: push trade executions and position changes in real time | P1 |
| FR-8.3 | Trade detail view: for any trade, show the full reasoning chain (ML signal → Gemini review → risk check → execution → outcome) | P0 |
| FR-8.4 | Daily report: auto-generated at market close — today's trades, PnL, prediction accuracy, top signals, market summary | P0 |
| FR-8.5 | Weekly report: cumulative PnL, model performance trends, prediction accuracy trends, Gemini review analysis | P0 |
| FR-8.6 | Telegram integration: real-time trade alerts (entry/exit), daily summary at 4 PM IST, weekly summary on Saturday | P0 |
| FR-8.7 | Historical report access: query any date range for full trade history and performance metrics | P1 |
| FR-8.8 | Audit log: every action (data fetch, signal, risk check, LLM call, order, fill) logged with timestamp and full context | P0 |
| FR-8.9 | Dashboard auth: basic password protection (single user, personal use) | P1 |

---

## 2. Non-Functional Requirements

| ID | Requirement |
|----|------------|
| NFR-1 | **Reliability**: Bot must recover from crashes without manual intervention. OpenClaw heartbeat detects failures and restarts. |
| NFR-2 | **Latency**: Trade execution pipeline (signal → risk check → order) must complete within 2 seconds for intraday trades. |
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

## 3.1 Remaining Open Questions

### Q1: Intraday Auto Square-off Timing
Zerodha auto-squares-off at 3:20 PM with penalties. Should our bot:
- Square off at 3:15 PM (safe margin)?
- Hold swing positions overnight (requires CNC order type)?
- Convert winning intraday positions to swing if conditions met?

### Q2: News Source Approach
Web scraping MoneyControl/ET Markets may violate their ToS. Options:
- Use their RSS feeds (publicly available)
- Use Google News API for financial news
- Use NSE official APIs (most reliable, no legal risk)
- Use Gemini with web grounding to search/summarize market news
- **Recommendation**: Use RSS feeds + NSE official APIs + Gemini web grounding. Avoid direct scraping.

### Q3: Emergency Kill Switch
Should there be a way to immediately halt all trading via:
- Telegram command (`/stop` or `/kill`)
- Dashboard button
- Both?
- **Recommendation**: Both. This is critical safety infrastructure.

---

## 4. Legal & Regulatory Notes (India)

| Area | Status |
|------|--------|
| Algo trading for retail | **Legal** — SEBI allows retail algo trading via broker APIs. No registration needed for personal use. |
| Tax implications | Short-term capital gains (STCG) at 20% for equity held < 1 year. Intraday profits taxed as speculative business income. |
| Audit requirement | If turnover exceeds ₹10 crore (speculative) or ₹10 crore (non-speculative), tax audit is mandatory. Unlikely at ₹1L capital. |
| Zerodha API ToS | Automated login (Selenium) may violate ToS. API usage for trading is explicitly allowed. |
| Data scraping | Scraping NSE/BSE data may have restrictions. Use official APIs where available. |

---

## 5. Tech Stack (Final)

| Component | Choice |
|-----------|--------|
| Agent Orchestration | OpenClaw (model-agnostic, heartbeat, skills, Telegram integration) |
| Language | Python 3.12+ |
| Broker | Zerodha Kite Connect API |
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
- Market data ingestion (OHLCV from Kite)
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
