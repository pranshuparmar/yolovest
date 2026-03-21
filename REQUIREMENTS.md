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
| FR-1.3 | Implement 15 OpenClaw Skills (see Skill Manifest below). Each skill is a discrete, independently invocable capability: | P0 |

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

**Heartbeat error propagation policy:**

| If this skill fails... | Then... | Rationale |
|------------------------|---------|-----------|
| `health-check` | **ABORT entire heartbeat.** Alert via Telegram. | System is unhealthy — no point running anything else. |
| `ingest-data` | **SKIP `market-scan` and `generate-signals`.** Run `position-monitor` only. | Cannot scan/signal on stale data (FR-10.4). But must still monitor open positions. |
| `market-scan` | **SKIP `generate-signals`.** Run `position-monitor` only. | No watchlist = no signals. Open positions still need monitoring. |
| `generate-signals` | **SKIP signal chain.** Run `position-monitor` only. | No signals to process. |
| `risk-check` (per signal) | **SKIP this signal only.** Continue to next signal. Log rejection. | One bad signal should not block others. |
| `llm-review` (per signal) | **Auto-approve if `risk.llm_fallback_to_rules` is true.** Otherwise skip signal. | FR-5.12: never block trading on LLM availability. |
| `trade-execute` (per signal) | **Log error, alert.** Continue to next signal. Retry on next heartbeat if order is retryable. | FR-6.6 handles retries internally. |
| `position-monitor` | **Alert via Telegram as CRITICAL.** Open positions are unmonitored. | Most dangerous failure — trailing SLs not updating. |

**Heartbeat mutex:** If a heartbeat is still running when the next interval fires, **skip the new heartbeat** and log a warning. If 3 consecutive heartbeats are skipped due to overrun, alert via Telegram as CRITICAL. Configurable via `heartbeat.max_consecutive_skips` (default: `3`).
| FR-1.4 | Use OpenClaw's messaging integration for Telegram — trade alerts, daily summaries, error notifications | P0 |
| FR-1.5 | OpenClaw Memory — persist agent state, trading context, conversation history, and reasoning across restarts using OpenClaw's Markdown-based memory system | P0 |
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
| FR-2.3 | Scrape/fetch news from MoneyControl, Economic Times Markets, LiveMint — headlines, analyst ratings, target prices | P0 |
| FR-2.4 | Fetch fundamental data from Screener.in — PE, PB, debt ratios, quarterly results, promoter holdings | P1 |
| FR-2.5 | Fetch technical screener data from Trendlyne — momentum scores, volume breakouts, technical signals | P1 |
| FR-2.6 | Ingest economic calendar events (RBI policy, US Fed, GDP data, earnings dates) | P1 |
| FR-2.7 | Use Gemini API to summarize and extract actionable sentiment from raw news data — classify as bullish/bearish/neutral per symbol | P0 |
| FR-2.8 | Store all ingested data in SQLite with timestamps for historical reference | P0 |
| FR-2.9 | Rate-limit all data fetching to respect source APIs and avoid IP bans | P0 |
| FR-2.10 | Fetch pre-market data (GIFT Nifty, global indices, overnight US market moves) before market open | P1 |
| FR-2.11 | Use Gemini with web grounding to search and summarize real-time market news beyond what RSS/APIs provide | P0 |
| FR-2.12 | Fetch Google Finance data for broader market sentiment and global cues | P1 |
| FR-2.13 | Aggregate all news sources with deduplication — same news from multiple sources should be merged, not double-counted in sentiment | P0 |

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
| FR-4.2 | XGBoost/LightGBM model trained on historical features to generate buy/sell/hold signals with confidence scores. Confidence score = calibrated probability [0.0, 1.0], validated during each model retraining cycle. | P0 |
| FR-4.3 | Separate models for intraday (short features, 1-5min candles) and swing (daily features, multi-day patterns). Decision logic: use intraday model during market hours for MIS trades, swing model for CNC/overnight trades. Default trade type configurable via `strategy.default_trade_type` (default: `"intraday"`). | P0 |
| FR-4.4 | Gemini-powered trade review: before every trade, send full context (signal, indicators, news sentiment, portfolio state, market conditions) to Gemini for approval/rejection/resize recommendation | P0 |
| FR-4.5 | Signal must include: entry price, target price, stop-loss price, position size, expected holding period, confidence score | P0 |
| FR-4.6 | Backtesting engine: simulate strategies on historical data, compute Sharpe ratio, max drawdown, win rate, profit factor. Minimum deployment thresholds: Sharpe > `strategy.backtest_min_sharpe` (default: `1.0`), max drawdown < `strategy.backtest_max_drawdown_pct` (default: `0.20` i.e. 20%). | P0 |
| FR-4.7 | Walk-forward validation: backtest must use rolling train/test windows (no lookahead bias). Backtest without walk-forward is invalid. | P0 |
| FR-4.8 | Minimum confidence score from ML model required to generate a tradeable signal. Signals below threshold are discarded before risk-check. Configurable via `risk.min_confidence_score` (default: `0.65`). | P0 |

### FR-5: Risk Management

**All risk parameters are configurable via `risk` section in config.yaml. Defaults shown below are conservative starting values.**

| ID | Requirement | Config Key | Default | Priority |
|----|------------|-----------|---------|----------|
| FR-5.1 | Max risk per trade as % of total capital. "Total capital" = current portfolio value (cash + unrealized positions value). | `risk.max_risk_per_trade_pct` | `0.02` (2%) | P0 |
| FR-5.2 | Max total portfolio exposure as % of capital (rest stays in cash) | `risk.max_portfolio_exposure_pct` | `0.60` (60%) | P0 |
| FR-5.3 | Max simultaneous open positions | `risk.max_open_positions` | `3` | P0 |
| FR-5.4 | Max single stock exposure as % of portfolio | `risk.max_single_stock_pct` | `0.25` (25%) | P0 |
| FR-5.5 | Daily loss circuit breaker — if daily realized loss exceeds this % of capital, stop NEW signal generation and discard pending pipeline orders. Exits, square-offs, and SL orders are still allowed. Resets automatically at next market open (9:15 AM). **AC:** On trigger: block new signals, allow exits/SLs, send Telegram alert within 10s. | `risk.daily_loss_limit_pct` | `0.03` (3%) | P0 |
| FR-5.6 | Weekly loss circuit breaker — reduce position sizing by `weekly_loss_sizing_reduction` if weekly loss exceeds this %. Weekly period: Monday 9:15 AM → Friday 15:30 PM. Resets Monday 9:15 AM. Reset day configurable via `risk.weekly_reset_day`. **AC:** Weekly period status included in daily report. | `risk.weekly_loss_limit_pct` | `0.05` (5%) | P0 |
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
| FR-5.13 | Correlation check — max positions allowed in same sector simultaneously | `risk.max_same_sector_positions` | `1` | P0 |
| FR-5.14 | **Emergency kill switch**: Telegram commands (`/stop` to pause, `/kill` to square off everything) + dashboard button | `risk.kill_switch_enabled` | `true` | P0 |
| FR-5.15 | Kill switch state persists across restarts — stays paused until `/resume` | `risk.kill_switch_persistent` | `true` | P0 |
| ~~FR-5.16~~ | *Moved to FR-4.8 (signal generation filter)* | — | — | — |
| FR-5.17 | Max number of trades per day (to avoid overtrading) | `risk.max_trades_per_day` | `10` | P1 |
| FR-5.18 | Cooldown period (minutes) after a losing trade before next entry | `risk.loss_cooldown_minutes` | `15` | P1 |

### FR-6: Order Execution

| ID | Requirement | Priority |
|----|------------|----------|
| FR-6.1 | Execute via Zerodha Kite Connect API: support Market, Limit, SL, and SL-M order types | P0 |
| FR-6.2 | Paper trading mode by default — live trading requires explicit config flag. Paper trading fill model: fill at LTP with configurable simulated slippage via `execution.paper_slippage_pct` (default: `0.001` i.e. 0.1%). | P0 |
| FR-6.3 | Handle Kite daily re-authentication: semi-automated — bot sends Telegram reminder at 9 AM, user pastes `request_token`, bot exchanges for `access_token`. ~30 seconds daily. | P0 |
| FR-6.4 | Order lifecycle tracking: placed → open → partially_filled → filled/rejected/cancelled — with timestamps. Partial fill handling: if <100% filled within `execution.order_timeout_sec` (default: `30`), cancel remainder. SL set for filled quantity only. | P0 |
| FR-6.5 | Reconcile local position state with broker state periodically (every heartbeat) | P0 |
| FR-6.6 | Retry failed orders with exponential backoff. Max retries configurable via `execution.max_order_retries` (default: `3`), base delay via `execution.retry_base_delay_sec` (default: `2`) | P0 |
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
| FR-7.8 | Track Gemini's own trade review accuracy — did its approvals/rejections lead to better outcomes? | P1 |

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
| FR-8.9 | Dashboard auth: basic password protection (single user, personal use) | P0 |

### FR-9: Capital & Portfolio Management

| ID | Requirement | Priority |
|----|------------|----------|
| FR-9.1 | Track current portfolio value (cash + unrealized positions) as "total capital". Initial amount configurable via `capital.initial_amount` (default: `100000`). | P0 |
| FR-9.2 | Transaction cost modeling — include brokerage (Zerodha: ₹20 or 0.03%), STT, stamp duty, GST, exchange fees in all PnL calculations and backtesting. | P0 |
| FR-9.3 | Capital exhaustion detection — when remaining cash < minimum viable trade size, pause new signals and alert via Telegram. | P0 |
| FR-9.4 | Margin usage toggle — `risk.margin_usage_enabled` (default: `false`). MIS gets leverage but we don't use it unless explicitly enabled. | P0 |

### FR-10: Database & Data Management

| ID | Requirement | Priority |
|----|------------|----------|
| FR-10.1 | SQLite schema versioned via migration scripts. | P0 |
| FR-10.2 | Automated daily DB backup. Configurable via `database.backup_enabled` (default: `true`), `database.backup_cron` (default: `"0 18 * * *"`), `database.backup_dir` (default: `./backups`). | P0 |
| FR-10.3 | Data retention policy — OHLCV: 2 years, audit logs: 1 financial year (Apr-Mar), predictions: 1 year. Configurable via `database.retention.ohlcv_days`, `database.retention.audit_log_days`, `database.retention.predictions_days`. | P1 |
| FR-10.4 | Data staleness validation — reject data older than `market_data.stale_threshold_minutes` (default: `30`). If all providers return stale data, skip signal generation. | P0 |

### FR-11: Market Calendar & Holiday Handling

| ID | Requirement | Priority |
|----|------------|----------|
| FR-11.1 | NSE holiday calendar — maintain holiday list (fetch from NSE or manual config via `market_hours.holidays`). No trading activity on holidays. | P0 |
| FR-11.2 | Early close handling — configurable via `market_hours.early_close_days`. Adjust square-off time accordingly. | P1 |
| FR-11.3 | Market status check in all `should_run()` — skills must verify market is actually open, not just check weekday+time. | P0 |

---

## 2. Non-Functional Requirements

| ID | Requirement |
|----|------------|
| NFR-1 | **Reliability**: Bot must recover from crashes without manual intervention. OpenClaw heartbeat detects failures and restarts. |
| NFR-2 | **Latency**: Trade execution pipeline (signal → risk check → order placement) must complete within configurable threshold `execution.max_pipeline_latency_sec` (default: `2`). LLM review is asynchronous and excluded from this latency requirement. |
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
| **Zerodha Auth** | Semi-automated — bot sends Telegram reminder at 9 AM, user pastes `request_token`, bot exchanges for `access_token`. ~30 seconds daily. No browser automation. |
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
| Tax implications | Tax rates per prevailing law. System tracks trades for tax export; actual computation is out of scope. Intraday profits taxed as speculative business income. |
| Audit requirement | If turnover exceeds ₹10 crore (speculative) or ₹10 crore (non-speculative), tax audit is mandatory. Unlikely at ₹1L capital. |
| Zerodha API ToS | API usage for trading is explicitly allowed. Semi-automated auth via user-pasted request_token. |
| Tax data export | System must export trade data in CA-friendly format for ITR filing. |
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

### Phase 0: Skill Infrastructure & Orchestration
- Typed Pydantic data schemas (`models/schemas.py`): Signal, Trade, Position, PortfolioState, TradeContext, TradeReview, SentimentResult, OHLCVBar, Prediction — all inter-skill contracts
- Set up OpenClaw as the orchestration layer: context object (`self.ctx`), skill registry, orchestrator, event bus
- Heartbeat loop with error propagation policy and mutex (skip-on-overrun)
- LLM abstraction (`LLMBase` ABC) with all 6 methods
- Telegram messaging integration
- `kill-switch`, `health-check`, `auth-broker` skill stubs
- Config validation (Pydantic validators for all config sections)

### Phase 1: Foundation & Data Pipeline
- Project scaffold, config system
- Database schema design and migration setup (FR-10.1)
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
- `kill-switch` full implementation, `health-check` full implementation

### Phase 4: Self-Learning Loop
- Prediction tracking system
- Weekly model retraining pipeline
- Model versioning & A/B testing
- Prediction accuracy dashboard

### Phase 5: Dashboard & Reporting
- FastAPI dashboard with live updates
- Daily/weekly auto-generated reports
- Telegram summaries
- Full audit trail viewer

---

## 7. Verification Plan

1. **Unit tests** for every module (features, risk rules, signal generation)
2. **Skill isolation tests**: unit test for each skill with mocked context
3. **Risk-check test**: verify all FR-5 rules with edge cases (boundary values, combinations)
4. **Square-off test**: verify retry/escalation on failure (including extension window expiry)
5. **Kill switch test**: verify `/stop`, `/kill`, `/resume` end-to-end (including persistence across restarts)
6. **Market holiday test**: verify no trading activity on holidays
7. **Capital exhaustion test**: verify pause when cash runs out or falls below minimum trade size
8. **Partial fill test**: verify correct behavior on partial fills (SL for filled qty, remainder cancelled)
9. **Paper trading for 2+ weeks** before any live trading
10. **Backtest validation**: run strategies on 6 months of historical data, verify Sharpe > 1.0
11. **Prediction tracking validation**: verify prediction vs actual logging is accurate
12. **Gemini integration test**: verify trade review gate works, verify fallback when API is down
13. **OpenClaw health test**: kill the agent, verify heartbeat detects and restarts
14. **Telegram test**: verify all alert types (trade, daily summary, error) are delivered
15. **End-to-end paper run**: full autonomous day of paper trading with all systems active

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
    # Core trade pipeline
    @abstractmethod
    async def review_trade(self, context: TradeContext) -> TradeReview: ...

    @abstractmethod
    async def analyze_sentiment(self, symbol: str, headlines: list[str]) -> SentimentResult: ...

    # Market intelligence
    @abstractmethod
    async def summarize_with_web_grounding(self, prompt: str) -> WebGroundingResult: ...

    @abstractmethod
    async def validate_watchlist(self, shortlist: list[dict], sector_analysis: dict,
                                  premarket_context: dict) -> WatchlistValidation: ...

    # Reporting & analysis
    @abstractmethod
    async def summarize_market_day(self) -> MarketDaySummary: ...

    @abstractmethod
    async def analyze_prediction_failures(self, failures: list[dict]) -> FailureAnalysis: ...
```

### Data Schema Contracts (Pydantic models in `models/schemas.py`)

All inter-skill data exchange uses typed Pydantic models. No raw dicts between skills.

```python
# --- Core Trading ---
class Signal(BaseModel):
    symbol: str
    signal_type: Literal["BUY", "SELL", "HOLD"]
    entry_price: float
    target_price: float
    stop_loss_price: float
    position_size: int
    expected_holding_period: str          # e.g., "intraday", "3d", "1w"
    confidence_score: float               # calibrated probability [0.0, 1.0]
    model_version: str
    features_snapshot: dict               # indicator values at signal time

class Trade(BaseModel):
    trade_id: str
    symbol: str
    signal_type: Literal["BUY", "SELL"]
    entry_price: float
    fill_price: float
    quantity: int
    stop_loss_price: float
    target_price: float
    order_id: str | None                  # None for paper trades
    sl_order_id: str | None
    product: Literal["MIS", "CNC"]
    mode: Literal["paper", "live"]
    status: Literal["placed", "open", "partially_filled", "filled", "rejected", "cancelled"]
    slippage: float
    pnl: float | None                    # None while open
    exit_price: float | None
    created_at: datetime
    closed_at: datetime | None

class Position(BaseModel):
    position_id: str
    trade: Trade
    current_price: float
    unrealized_pnl: float
    trailing_sl_active: bool

class PortfolioState(BaseModel):
    total_capital: float                  # cash + unrealized (FR-5.1 definition)
    available_cash: float
    exposure_pct: float
    open_positions: int
    stock_exposures: dict[str, float]     # symbol → exposure %
    sector_counts: dict[str, int]         # sector → position count
    daily_pnl_pct: float
    weekly_pnl_pct: float
    trades_today: int
    minutes_since_last_loss: float

# --- LLM I/O ---
class TradeContext(BaseModel):
    signal: Signal
    portfolio: PortfolioState
    sentiment: SentimentResult | None
    premarket: PremarketContext | None
    sector_rotation: dict | None
    todays_trades: list[Trade]

class TradeReview(BaseModel):
    decision: Literal["APPROVE", "REJECT", "RESIZE"]
    reasoning: str
    adjusted_size: int | None             # only if RESIZE

class SentimentResult(BaseModel):
    symbol: str
    sentiment: Literal["bullish", "bearish", "neutral"]
    confidence: float                     # [0.0, 1.0]
    key_drivers: list[str]

# --- Market Data ---
class OHLCVBar(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

class PremarketContext(BaseModel):
    gift_nifty_change_pct: float | None
    us_sp500_change_pct: float | None
    market_bias: Literal["bullish", "bearish", "neutral"] | None
    llm_summary: str | None

# --- Prediction Tracking ---
class Prediction(BaseModel):
    prediction_id: str
    signal: Signal
    trade_id: str | None
    prediction_end_time: datetime          # computed: created_at + holding_period
    actual_price: float | None
    direction_correct: bool | None
    target_hit: bool | None
    actual_pnl_pct: float | None
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

capital:
  initial_amount: 100000              # starting capital in INR

broker:
  name: zerodha
  api_key: ${KITE_API_KEY}        # from environment
  api_secret: ${KITE_API_SECRET}

llm:
  provider: gemini
  model: gemini-2.5-pro           # or gemini-2.5-flash for lower latency
  api_key: ${GEMINI_API_KEY}

market_data:
  daily_provider: jugaad          # jugaad | yfinance
  daily_fallback: yfinance        # fallback if primary fails
  intraday_provider: tvdatafeed   # tvdatafeed | kite (paid)
  bhavcopy_dir: ./data/bhavcopy   # path to downloaded NSE Bhavcopy CSVs
  cache_ttl_minutes: 15           # cache fetched data to reduce API calls
  stale_threshold_minutes: 30     # reject data older than this many minutes

heartbeat:
  market_hours_interval_min: 15   # heartbeat frequency during market hours
  off_hours_interval_min: 60      # heartbeat frequency outside market hours
  max_consecutive_skips: 3        # alert if N heartbeats skipped due to overrun

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
  default_trade_type: "intraday"    # intraday | swing
  backtest_min_sharpe: 1.0          # minimum Sharpe ratio for model deployment
  backtest_max_drawdown_pct: 0.20   # maximum drawdown % for model deployment

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
  margin_usage_enabled: false        # MIS leverage disabled unless explicitly enabled
  weekly_reset_day: "monday"         # day when weekly circuit breaker resets

market_hours:
  open: "09:15"
  close: "15:30"
  order_start: "09:15"           # earliest time for new orders
  order_end: "15:15"             # latest time for new orders
  square_off: "15:15"            # auto square-off intraday positions
  square_off_extension: "00:05"  # extra window for square-off orders after order_end
  timezone: "Asia/Kolkata"
  holidays: []                   # list of NSE holiday dates (YYYY-MM-DD)
  early_close_days: {}           # map of date → early close time (e.g., "2026-03-30": "13:00")

execution:
  max_order_retries: 3            # retry failed orders this many times
  retry_base_delay_sec: 2         # exponential backoff base (2s, 4s, 8s)
  max_pipeline_latency_sec: 2     # signal→risk→order must complete within this (LLM excluded)
  paper_slippage_pct: 0.001      # simulated slippage for paper trading (0.1%)
  order_timeout_sec: 30          # cancel unfilled remainder after this many seconds

database:
  backup_enabled: true             # enable daily DB backups
  backup_cron: "0 18 * * *"       # backup schedule (daily 6 PM)
  backup_dir: ./backups            # backup directory
  retention:
    ohlcv_days: 730                # OHLCV data retention (2 years)
    audit_log_days: 365            # audit log retention (1 financial year)
    predictions_days: 365          # prediction data retention (1 year)

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
- **Authentication**: OAuth-style login flow that generates a `request_token` daily. Semi-automated — bot sends Telegram reminder, user pastes `request_token`.
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

---

## 13. Cross-Functional Review (CEO + PM + BA)

**Review date:** 2026-03-21
**Reviewers:** CEO (strategic), PM (execution), BA (quality)
**Status:** Findings logged. To be addressed before Phase 1 implementation begins.

### 13.1 CRITICAL Findings (Must Fix Before Build)

| # | Finding | Source | Affected FRs |
|---|---------|--------|-------------|
| C1 | **No disaster recovery or backup plan.** SQLite corruption or VPS disk failure loses all trade history, models, predictions. No backup/restore procedure. | CEO, BA | NFR (new) |
| C2 | **No broker API outage handling for open positions.** If Kite API goes down while positions are open and trailing SLs need updating, stale SLs on broker side could cause catastrophic loss. No fallback (e.g., bracket orders). | CEO | FR-5.8, FR-6 |
| C3 | **No gap risk management.** Swing (CNC) positions can gap 2-5% at open, blowing through a 2% SL. No pre-market SL adjustment or overnight gap risk sizing. | CEO | FR-5.1, FR-5.7 |
| C4 | **Daily circuit breaker (FR-5.5) may trap losing positions.** "Stop ALL trading" — does this prevent exits too? If yes, you're locked into losing positions. Must explicitly allow exits when breaker is active. | CEO | FR-5.5 |
| C5 | **`ingest-data` is a mega-skill.** Covers 12 sub-requirements (OHLCV, news, fundamentals, sentiment, dedup). Too complex to test, debug, maintain. Should decompose into 4-5 smaller skills. | PM | FR-2.1-2.13 |
| C6 | **No error propagation policy for heartbeat chain.** If `ingest-data` fails, should `market-scan` run on stale data or skip? No failure cascade rules defined. | PM, CEO | FR-1.3 |
| C7 | **`kill-switch` has no implementation phase.** P0 requirement (FR-5.14-5.15) not covered in any of the 6 phases. | PM | FR-5.14 |
| C8 | **11 of 14 skills have no verification items.** Only health-check, llm-review, and Telegram are covered. Risk-check, trade-execute, square-off, etc. have zero named tests. | PM | Section 7 |
| C9 | **Auth flow is contradictory across 3 locations.** FR-6.3 says "Selenium fallback", Resolved Decisions says "user pastes token", Legal says Selenium "may violate ToS". | BA | FR-6.3 |
| C10 | **Config conflict: `llm.review_every_trade` vs `risk.llm_review_enabled`.** Two different config paths claim to control the same LLM review behavior. | BA, PM | FR-5.11 |

### 13.2 CRITICAL Findings from PM Deep Dive (Architecture Blockers)

| # | Finding | Affected FRs |
|---|---------|-------------|
| C11 | **No shared context object (`self.ctx`) design.** Every skill depends on `self.ctx.broker`, `self.ctx.db`, `self.ctx.llm`, `self.ctx.market_data`, etc. — but the context, orchestrator, and event bus don't exist yet and aren't in any phase. **Must be Phase 0.** | FR-1.1, FR-1.3 |
| C12 | **No data schemas (`schemas.py`).** All inter-skill data contracts are implicit dict-key assumptions. `Signal`, `Trade`, `Position`, `PortfolioState`, `Prediction`, `Sentiment`, `TradeContext`, `TradeReview` — none are typed. Prerequisite for any skill implementation. | FR-4.5, FR-6.4, FR-7.1 |
| C13 | **No SQLite schema design.** 12+ tables implied across skills (`ohlcv`, `signals`, `trades`, `positions`, `predictions`, `sentiment`, `watchlist`, `reports`, `llm_reviews`, `system_state`, etc.) — none defined. No indexes, no migration strategy. | FR-2.8, NFR-3 |
| C14 | **Phase 5 (OpenClaw) must move to Phase 1.** Skills reference `self.ctx.*` everywhere. Without the orchestrator skeleton, Phases 1-4 can't be integration-tested. Phase 5 says "implement Skills" but all skill files already exist. | Section 6 |
| C15 | **`square-off` has no retry on failure.** If square-off fails and 5-min extension expires, MIS positions are left open for Zerodha to auto-square at penalties. No escalation path. | FR-5.10, FR-6.6 |
| C16 | **NFR-2 (2-sec latency) is infeasible.** LLM review alone takes 3-5 seconds per Gemini call. The signal→risk→order pipeline cannot meet 2 seconds with LLM in the path. Must redefine or make LLM async. | NFR-2, FR-4.4 |
| C17 | **`LLMBase` ABC only defines 2 methods but skills call 6.** `summarize_with_web_grounding()`, `validate_watchlist()`, `summarize_market_day()`, `analyze_prediction_failures()` are all missing from the interface. | Section 8 |

### 13.3 HIGH Findings (Fix During Phase 1)

| # | Finding | Source | Affected FRs |
|---|---------|--------|-------------|
| H1 | **No transaction cost modeling.** Brokerage, STT, GST, stamp duty not in position sizing, backtesting, or P&L. At 10 trades/day, costs are material. | CEO | FR-4.6, FR-5.1 |
| H2 | **Sector correlation (FR-5.13) is P2 but should be P0.** With 3 max positions and ₹1L capital, 2/3 in one sector during a sector crash is realistic. | CEO | FR-5.13 |
| H3 | **No staleness check for market data.** If jugaad-data returns cached yesterday's data or yfinance has 15min delay, ML models trade on stale prices. No freshness validation. | CEO | FR-2.1, FR-2.9 |
| H4 | **No partial fill handling.** Limit orders can partially fill. SL quantity, position tracking, and exit logic are undefined for partial fills. | CEO | FR-6.4 |
| H5 | **FR-5.1 "total capital" is undefined.** 2% of what — current capital? initial capital? minus unrealized losses? | CEO | FR-5.1 |
| H6 | **Confidence score undefined.** Is it model probability? Calibrated? Arbitrary 0-1? Calibration matters for FR-5.16 threshold. | CEO | FR-4.2, FR-5.16 |
| H7 | **No intraday-vs-swing decision logic.** FR-4.3 says "separate models" but nothing specifies how/when to use which model. | CEO | FR-4.3 |
| H8 | **FR-4.7 (walk-forward) is P1 but FR-4.6 (backtest) is P0.** Backtest without walk-forward has guaranteed lookahead bias. | CEO | FR-4.6, FR-4.7 |
| H9 | **Heartbeat can exceed 15min.** Full chain with Gemini calls for 25 stocks + per-signal reviews can overrun. No overlap/skip policy. | PM | FR-1.2 |
| H10 | **FR-3.1 scans "NSE universe" (~2000 stocks).** Unrealistic on 15min heartbeat with rate-limited free data sources. Needs pre-filter stage. | CEO | FR-3.1 |
| H11 | **10 missing test files in `tests/`.** Only 4 listed vs NFR-8 requiring "every component testable in isolation." | PM | NFR-8 |
| H12 | **No market holiday / early close handling.** ~15 holidays/year. No holiday calendar, no early-close square-off adjustment. | BA | FR-5.9-5.10 |
| H13 | **Paper trading realism undefined.** Does paper mode simulate fills at LTP? Simulate slippage? 100% fill assumption? | CEO | FR-6.2 |
| H14 | **FR-2.8 "store all data" — no schema.** Does "all" mean raw HTML, parsed text, or just scores? Storage varies wildly. | CEO | FR-2.8 |
| H15 | **Minimum training data size undefined.** Model retrain on 20 data points produces garbage. No minimum threshold. | CEO | FR-7.4 |
| H16 | **`position-monitor` vs `square-off` race condition at 15:15.** Both can try to close the same position simultaneously. No mutex or exclusion window. | PM | FR-5.10, FR-6.5 |
| H17 | **`auth-broker` "waiting" state has no timeout.** Skill returns `success=False` while awaiting user token. Appears as failed in every health check. No pending/callback mechanism in `SkillResult`. | PM | FR-6.3 |
| H18 | **`predict-track` never sets `prediction_end_time`.** Logging stores `expected_holding_period` but scoring reads `prediction_end_time` which is never computed. | PM | FR-7.1, FR-7.2 |
| H19 | **News scraping targets (MoneyControl, ET, LiveMint) actively block scraping.** No anti-scraping mitigation, no proxy strategy, no API alternatives. FR-2.3 will fail in production. | CEO | FR-2.3 |
| H20 | **FR-2.7 (P0) depends on FR-2.3 (P1).** Gemini sentiment analysis needs news as input, but news fetching is lower priority. Priority mismatch. | CEO | FR-2.3, FR-2.7 |

### 13.3 MEDIUM Findings (Address in Relevant Phase)

| # | Finding | Source |
|---|---------|--------|
| M1 | No log rotation / data retention policy. SQLite grows unbounded on 2GB VPS. | CEO, BA |
| M2 | No observability stack (structured logging, metrics, monitoring beyond Telegram). | CEO, BA |
| M3 | Loss cooldown (FR-5.18) is P2, should be at least P1 — prevents revenge trading. | CEO |
| M4 | Slippage tracking (FR-6.7) is P1 but data isn't fed back into anything. | CEO |
| M5 | No partial exit / scale-out capability. Binary full-entry / full-exit only. | CEO |
| M6 | No market regime detection (trending vs range-bound vs crash). Same params used always. | CEO |
| M7 | FR-3.2 weights must sum to 1.0 but no validation rule specified. | CEO, BA |
| M8 | F&O ban list data source not specified in FR-2, but FR-3.3 requires it. Implicit dependency. | CEO |
| M9 | `predict-track` dual trigger (EVENT + HEARTBEAT) could cause duplicate logging. | PM |
| M10 | Saturday CRON conflict: `model-retrain` (6AM) + `report-generate` (10AM) on 2GB VPS. | PM |
| M11 | Gemini API quota exhaustion mid-day not handled (distinct from "API down"). | CEO |
| M12 | `risk-check` vs `llm-review` boundary blurry — both can veto, both evaluate same context. | PM |
| M13 | No external health-check endpoint. If Telegram is down, no alert pathway. | BA |
| M14 | No glossary — terms like MIS, CNC, SL-M, WAL, GIFT Nifty, F&O ban, Bhavcopy, TOTP undefined. | BA |
| M15 | First-time setup / onboarding flow not documented. Section 12 lists commands but not prereqs. | BA |

### 13.5 BA: Requirement Quality Summary (53 Defects, 21 Improvements, 8 Observations)

**New Critical Defects from BA (not already captured above):**

| # | Finding | Affected FRs |
|---|---------|-------------|
| B1 | **FR-1.3 says "14 skills" but manifest table and implementation have 15.** Count discrepancy. | FR-1.3 |
| B2 | **FR-5.5 circuit breaker: no reset condition.** "Stop all trading" — when does it reset? Next market open? Manual resume? | FR-5.5 |
| B3 | **FR-5.6 weekly breaker: no reset definition.** Rolling 7 days? Calendar week? Monday open? No `weekly_reset` config. | FR-5.6 |
| B4 | **No capital exhaustion handling.** When cash hits zero or below min trade size, behavior is undefined. | FR-5.1, FR-5.2 |
| B5 | **No config hot-reload mechanism.** Changing parameters mid-day requires restart. No FR addresses live config updates. | All config-dependent FRs |
| B6 | **FR-5.16 is misplaced.** Listed under Risk (FR-5) but enforced in `generate-signals` (FR-4 domain). `risk-check` never validates confidence. | FR-5.16 |
| B7 | **FR-1.6 implementation doesn't match requirement.** Requirement says "positions are protected." Implementation only sends alert — no protective square-off or order cancellation. | FR-1.6 |
| B8 | **FR-4.6/4.7 (backtesting) have no skill.** Listed in `scripts/` only. Should clarify: skill or standalone script? | FR-4.6, FR-4.7 |
| B9 | **Tax/audit trail incomplete.** No turnover calculation, no ITR-3/ITR-4 export fields, no tax-filing-friendly data format. STCG rate stated as 20% (correct post-2024 budget) but should not be hardcoded. | Section 4 |
| B10 | **FR-6.3 Selenium option must be removed.** Three conflicting statements about auth. Selenium flagged as ToS-violating by our own Legal section — should not be in an FR. | FR-6.3 |

**BA: Proposed Acceptance Criteria for Top 10 FRs:**

| FR | Proposed AC |
|----|------------|
| FR-1.6 | On crash: cancel all pending orders within 30s of restart. Existing SL orders untouched on broker. Auto-restart within 60s. Alert if broker connectivity lost >5min with open positions. |
| FR-2.7 | Sentiment output: `{symbol, sentiment: bullish|bearish|neutral, confidence: 0.0-1.0, key_drivers: list[str]}`. Latency <5s per symbol batch. |
| FR-2.13 | Duplicates = same entity + same event within 4h window. Must reduce article count by >=30% when multiple sources active. |
| FR-4.2 | Confidence scores are calibrated probabilities [0.0, 1.0]. Weekly calibration check during model retrain. |
| FR-4.6 | Backtest on 6 months must complete in <10min. Must output: Sharpe, max drawdown, return, win rate, profit factor. Min Sharpe 1.0 for deployment. |
| FR-5.5 | On trigger: stop new signals, discard pipeline orders, keep existing positions+SLs. Alert within 10s. Reset at next market open (9:15 AM). |
| FR-5.6 | Weekly period: Monday 9:15 AM → Friday 15:30 PM. Resets Monday 9:15 AM. Status in daily report. |
| FR-7.4 | Min data: 100 scored predictions + 50 completed trades before retrain proceeds. Skip with log if threshold not met. |
| FR-8.8 | Retain logs for 1 financial year (Apr-Mar). Append-only. Fields: timestamp_ist, action_type, skill_name, input_summary, output_summary, duration_ms. |
| FR-5.14 | `/kill` must cancel all orders + square off all positions within 30s. Telegram confirmation sent back to user. |

### 13.6 PM: Missing Config Validations

| Validation | Documented? |
|------------|-------------|
| `scanning.weights` must sum to 1.0 | Yes (FR-3.2) |
| `risk.*_pct` values must be 0 < x < 1 | No |
| `market_hours.order_start` < `market_hours.order_end` | No |
| `market_hours.square_off` between `order_end` and `close` | No |
| `mode` must be "paper" or "live" | No |
| `execution.max_pipeline_latency_sec` referenced in NFR-2 but never enforced in code | No |

### 13.7 Summary Scorecard

| Area | Score | Notes |
|------|-------|-------|
| **Strategic completeness** | 7/10 | Strong core. Missing disaster recovery, gap risk, cost modeling. |
| **Implementation readiness** | 5/10 | No context object, no schemas, no DB design, no orchestrator. Phase 5 must become Phase 0. |
| **Requirement quality** | 5/10 | 53 BA defects. Many FRs lack AC. Auth contradictory. Circuit breaker resets undefined. Capital exhaustion unhandled. |
| **Test coverage** | 4/10 | Only 3 of 14 skills have verification items. 10 missing test files. No integration tests. |
| **Risk management** | 7/10 | FR-5 is comprehensive. But gap risk, breaker reset/exit logic, partial fills, capital exhaustion are blind spots. |
| **Architecture readiness** | 4/10 | LLMBase 2/6 methods. No typed schemas. No DB schema. Skill triggers need rework. |
| **Regulatory/Compliance** | 5/10 | SEBI status vague. Tax trail incomplete. Selenium violates own Legal section. No holiday calendar. |
| **Overall** | **5.3/10** | **Strong vision + skill architecture. 17 CRITICALs + 20 HIGHs + 10 BA defects before Phase 1. BA proposed ACs for top 10 FRs ready to incorporate.** |

### 13.8 Combined Finding Count

| Source | Critical | High | Medium | BA Defects | BA Improvements |
|--------|----------|------|--------|------------|-----------------|
| CEO | 5 | 8 | 6 | — | — |
| PM | 7 | 13 | 8 | — | — |
| BA | — | — | — | 53 | 21 |
| **Unique actionable** | **17** | **20** | **15** | **10 new** | **top 10 ACs proposed** |

---

## 14. Glossary

| Term | Definition |
|------|-----------|
| MIS | Margin Intraday Square-off — Zerodha order product type for intraday trades. Positions auto-squared by broker at end of day. |
| CNC | Cash and Carry — Zerodha order product type for delivery (swing/positional) trades. Positions held overnight. |
| SL | Stop-Loss order — a limit order triggered when price reaches a specified level, used to cap losses. |
| SL-M | Stop-Loss Market order — like SL but executes as a market order once triggered. Guarantees fill, not price. |
| TOTP | Time-based One-Time Password — used for Zerodha two-factor authentication during login. |
| OHLCV | Open, High, Low, Close, Volume — standard candlestick data format for price bars. |
| Bhavcopy | NSE's end-of-day equity data file containing closing prices, volumes, and trade statistics for all listed securities. |
| GIFT Nifty | GIFT Nifty (formerly SGX Nifty) — Nifty 50 futures traded on Gujarat International Finance Tec-City (GIFT City) exchange. Used as a pre-market indicator for Indian markets. |
| F&O Ban | SEBI-imposed ban on fresh F&O positions when open interest exceeds 95% of market-wide position limit (MWPL). Stocks in ban can only reduce existing positions. |
| FII/DII | Foreign Institutional Investors / Domestic Institutional Investors — their daily buy/sell data is a key market sentiment indicator. |
| WAL mode | Write-Ahead Logging — SQLite journaling mode that enables concurrent reads during writes, improving performance. |
| ATR | Average True Range — volatility indicator measuring the average range of price movement over N periods. Used for stop-loss sizing. |
| OBV | On-Balance Volume — cumulative volume indicator that relates volume to price change. Used to confirm trends. |
| VWAP | Volume-Weighted Average Price — average price weighted by volume, commonly used as an intraday benchmark. |
| MWPL | Market-Wide Position Limit — SEBI-defined maximum open interest allowed for a stock's F&O contracts. |
| STT | Securities Transaction Tax — tax levied on every stock exchange transaction in India. |
| Sharpe ratio | Risk-adjusted return metric: (portfolio return − risk-free rate) / standard deviation of returns. Higher is better; >1.0 is acceptable, >2.0 is good. |
