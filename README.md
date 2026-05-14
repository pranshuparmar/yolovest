# YoloVest

YoloVest is a fully autonomous AI-driven stock trading platform for the Indian market. It runs a heartbeat pipeline every 15 minutes during market hours — scanning stocks, generating ML signals, validating them through risk checks and optional LLM review, and executing trades via Zerodha Kite Connect. It learns from its own predictions, retrains models weekly, and sends you Telegram alerts so you can monitor everything from your phone.

**Paper trading by default** — live trading requires explicit opt-in after you're confident in the system's performance.

> **Disclaimer:** This application was vibe-coded for personal use. I do not take any responsibility for financial losses, bugs, or unexpected behavior. If you choose to run this in live mode with real money, you do so entirely at your own risk. Markets are unpredictable, software has bugs, and past performance (including backtests) does not guarantee future results. Always start with paper mode and small capital.

---

## What It Does

- **Autonomous trading pipeline** — Every 15 minutes during market hours: fetches data, scans the market, generates ML signals, runs risk checks, optionally gets LLM approval, and executes trades
- **ML signal generation** — XGBoost models with probability calibration, **real-PnL walk-forward backtesting** (simulates each fold-test prediction through actual transaction costs, sizing, and slippage — not a synthetic payoff), and automatic weekly retraining
- **Shadow A/B testing** — New models run in shadow mode alongside production for 7 days before promotion, based on real market performance
- **LLM trade review** — Optional Google Gemini gate that approves, rejects, or resizes trades with full reasoning
- **Multi-source market data** — Kite paid plan (when enabled, primary for both daily and intraday) with free-provider fallback chain (jugaad-data, yfinance, tvDatafeed). Optional **KiteTicker WebSocket** gives sub-second LTP for instant target/SL detection on the heartbeat cycle.
- **News sentiment** — Aggregates from MoneyControl, ET Markets, LiveMint, NSE, and Google Finance with Gemini-powered sentiment scoring
- **Full risk management** — Position sizing (via Kite's pre-trade `order_margins` when leverage is on, falling back to notional), exposure caps, daily/weekly circuit breakers, trailing stop-loss, kill switch, symbol cooldowns, sector caps, correlation limits
- **Broker-side exit enforcement** — Two-leg OCO GTT for CNC trades (target + SL placed at the broker after entry fills; ghost-recovery closes the local row when one fires); resting target LIMIT + SL for MIS (Zerodha doesn't allow GTT on intraday — this is the OCO equivalent). Position-monitor reconciles GTT state every heartbeat so user-cancelled / rejected / expired GTTs fall back to client-side detection automatically.
- **Real transaction costs** — When the broker is authenticated, realized PnL on close uses Kite's actual contract-note charges (`POST /charges/orders`). Falls back to a config-based estimate on paper / offline. Same breakdown is stored on the trade row so the detail view shows actuals rather than recomputing on every page load.
- **Signed postbacks with business logic** — `/api/auth/zerodha/postback` verifies `SHA-256(order_id + order_timestamp + api_secret)` and drives real trade-row updates (entry COMPLETE backfills fill_price; SL/target COMPLETE cancels the other leg; entry/SL REJECTED alerts you immediately) so trade state stays current between heartbeats.
- **Self-learning loop** — Tracks predictions, scores them against actual outcomes, feeds failures back into retraining
- **Web dashboard** — 20+ pages covering portfolio, trades (with broker order history + GTT lifecycle audit), positions, ML models, dry-run previews, risk simulation, analytics, reports, settings
- **Telegram bot** — Real-time alerts, portfolio status, kill switch control, trade approval/rejection, daily Kite re-authentication
- **Dry-run previews** — Generate signals without executing them to see what the system would do today

---

## Paper Mode vs Live Mode

YoloVest has two operating modes, controlled by the `mode` setting (changeable via the Settings page):

### Paper Mode (default)

- All orders are **simulated locally** — no real money is involved
- Simulated slippage is applied (configurable, default 0.1%) to keep results realistic
- Transaction costs (brokerage, STT, GST) are still deducted from PnL
- Does **not** require a Zerodha account — the system runs with a simulated broker if Kite credentials aren't set
- If Kite credentials are provided, it will still fetch real market data and holdings but won't place real orders

Use paper mode to evaluate the system's performance before committing real capital.

### Live Mode

- Orders are placed directly via Zerodha Kite Connect API
- Real positions, margins, and order lifecycle are tracked
- Requires valid Zerodha API credentials and **daily re-authentication** (Zerodha expires tokens at 6 AM IST)
- Price drift rejection prevents stale signals from executing at unexpected prices
- All safety mechanisms (kill switch, circuit breakers, exposure caps) are active

Switch to live mode only after you've run paper mode long enough to trust the system's signals and risk management.

---

## Setup

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Zerodha Kite Connect** | Required for live trading. Paper mode works without it. Get API keys at [kite.trade](https://kite.trade/) |
| **Google Gemini API** | Optional. Enables sentiment analysis, LLM trade review, and failure analysis. Get a key at [ai.google.dev](https://ai.google.dev/) |
| **Telegram Bot** | Optional but recommended. Create one via [@BotFather](https://t.me/botfather) for alerts and remote control |

### Option 1: Docker (recommended)

```bash
git clone https://github.com/pranshuparmar/yolovest.git
cd yolovest

# Create config
cp backend/config.example.yaml backend/config.yaml

# Create .env with your secrets
cp .env.example .env
# Edit .env and fill in your API keys
```

Edit your `.env` file:

```env
DOMAIN=yolovest.example.com        # Your domain (for SSL)
LETSENCRYPT_EMAIL=you@example.com   # For Let's Encrypt certificate
KITE_API_KEY=your_key               # Zerodha (leave blank for paper-only)
KITE_API_SECRET=your_secret
GEMINI_API_KEY=your_key             # Google Gemini (optional)
TELEGRAM_BOT_TOKEN=your_token       # Telegram (optional)
TELEGRAM_CHAT_ID=your_chat_id
```

Start everything:

```bash
docker compose up -d

# Check logs
docker compose logs -f backend
```

The dashboard will be available at `https://your-domain`. Default password: `yolovest`.

Docker automatically manages SSL certificates, nginx reverse proxy, database backups, and log rotation. Data is persisted across restarts via Docker volumes (database, models, backups, logs).

### Option 2: Run Locally (without Docker)

**Backend** (Python 3.11+):

```bash
cd backend
pip install -e ".[dev]"
cp config.example.yaml config.yaml
# Edit config.yaml — set your API keys or export them as environment variables
PYTHONPATH=src python -m yolovest.main --config config.yaml
```

**Frontend** (Node.js 18+, separate terminal):

```bash
cd frontend
npm install
npm run dev
```

The backend runs on `http://localhost:8080` and the frontend dev server on `http://localhost:5173` (proxies API calls to the backend).

For production frontend builds:

```bash
cd frontend
npm run build   # Outputs to dist/
```

---

## Daily Kite Re-Authentication

Zerodha expires API tokens daily at 6:00 AM IST. If you're using Kite Connect (paper with real data, or live mode), you need to re-authenticate each trading day:

1. The system sends you a Telegram message at **8:30 AM IST** (configurable) with a Kite login link
2. Click the link and log in to Zerodha — the app receives the token automatically via OAuth redirect
3. As a fallback (e.g., if the redirect doesn't work), you can also send the token manually via `/auth <request_token>` on Telegram or paste it on the **Integrations** page

The token is cached in the database and restored automatically if you restart the app during the day.

---

## Configuration

Configuration is split into two layers:

### 1. Config File (`config.yaml`)

Contains secrets and infrastructure settings that can't be changed at runtime:

- API keys and secrets (Zerodha, Gemini, Telegram)
- Database and backup paths
- Server host/port
- Log file paths and rotation settings

These use `${VAR_NAME}` syntax to pull values from environment variables. See `config.example.yaml` for all options with inline documentation.

### 2. Settings Page (DB config, ~90 keys)

Everything else is editable live from the **Settings** page in the dashboard — no restart needed. Changes take effect immediately. Settings are grouped into sections:

| Section | What You Can Tune |
|---------|------------------|
| **Capital** | Starting capital amount |
| **Risk** | Max risk per trade, exposure caps, position limits, daily/weekly loss limits, confidence thresholds, symbol cooldowns, kill switch |
| **Scanning** | Stock universe, shortlist size, minimum volume, sector weights |
| **Strategy** | Indicator toggles (RSI, MACD, Bollinger, etc.), EMA periods, volatility filters, market regime detection |
| **Execution** | Paper slippage %, max order retries, price drift tolerance, scaled entry |
| **Transaction Costs** | Brokerage, STT, stamp duty rates (pre-filled with current NSE/Zerodha rates) |
| **Heartbeat** | Pipeline interval during/outside market hours |
| **Market Hours** | Open/close times, square-off time, timezone |
| **Market Data** | Data providers, news/scraper toggles, staleness thresholds |
| **LLM** | Enable/disable Gemini, model selection |
| **Reports** | Daily report time, weekly report schedule |
| **Retraining** | Retrain schedule, shadow mode duration, minimum samples |
| **Notifications** | Telegram alert toggles (trade entry, exit, daily summary, errors, kill switch) |

### Service Toggles

External services can be independently enabled or disabled:

| Service | Config Key | Default | What It Controls |
|---------|-----------|---------|-----------------|
| Gemini LLM | `llm.enabled` | `false` | Sentiment analysis, trade review, failure analysis |
| News sources | `market_data.news_enabled` | `true` | MoneyControl, ET Markets, LiveMint RSS feeds |
| Scrapers | `market_data.scrapers_enabled` | `true` | Screener.in, Trendlyne, Google Finance, NSE filings |
| Kite data plan | `market_data.kite_data_enabled` | `false` | Paid Kite Connect historical data API (preferred over free providers when on) |
| KiteTicker WebSocket | `market_data.kite_websocket_enabled` | `false` | Sub-second LTP cache; position-monitor reads from it before falling back to REST |
| Telegram | `notifications.telegram.enabled` | `false` | Telegram bot and notifications |
| LLM review gate | `risk.llm_review_enabled` | `true` | Gemini trade approval (falls back to rules-only if LLM disabled) |
| Trailing SL | `risk.trailing_sl_enabled` | `true` | Raises SL as profit accrues — works for both client-side SL orders and broker-side GTT (via `kite.modify_gtt`) |
| Margin / leverage | `risk.margin_usage_enabled` | `false` | When on, risk-check sizes positions against Kite's `order_margins` instead of full notional |

### Broker Integration (Zerodha Kite)

The Kite integration goes well beyond `place_order` — every part of the
order lifecycle is wired so trade state stays consistent between the
broker and the local database.

| Surface | What's wired |
|---|---|
| **Order placement** | MARKET → LIMIT auto-conversion at LTP ± buffer (Kite rejects raw MARKET via API now). SL-M → SL with a 0.5% past-trigger limit. Universal tick rounding (NSE 0.05). Every order carries a `tag` (`yv-entry`, `yv-sl`, `yv-tgt`, `yv-sqoff`, `yv-close`, `yv-partial`, etc.) so the placer is visible in `kite.orders()`. Residual MARKET/SL-M orders carry `market_protection=-1` to satisfy Kite's new requirement. |
| **CNC OCO GTT** | Two-leg target + SL GTT placed at the broker on entry fill. Pre-flight validates SL/target sides; slot-cap (≥45 active GTTs out of 50) skips placement and falls back to client-side. Reconciler each heartbeat detects user-cancelled / rejected / expired / vanished GTTs and clears `gtt_id` locally so client-side detection resumes. Status badge on trade detail. |
| **MIS OCO** | Kite doesn't allow GTT on MIS, so trade-execute places a resting LIMIT at target alongside the SL. Position-monitor enforces OCO: when one fills, the other is cancelled. Same ghost-recovery path closes the local row. |
| **Trailing SL** | When profit ≥ `trailing_sl_trigger_multiple × risk`, the SL leg is raised in place — `kite.modify_gtt` for CNC, `kite.modify_order` for plain SL orders. |
| **Partial profit booking** | Books a fraction of the position at an intermediate target. GTT is resized to the remaining quantity so subsequent fires aren't rejected for over-qty. |
| **Square-off / kill switch** | Cancels SL + target orders and deletes attached GTT before the market exit. Kill switch additionally sweeps orphan GTTs (live at broker but with no matching open local trade). |
| **`convert_position`** | `POST /api/positions/{trade_id}/convert` promotes winning MIS to CNC before square-off; cancels stale MIS-side OCO orders since they're product-specific. |
| **Postback handler** | Verifies `SHA-256(order_id + order_timestamp + api_secret)` against the payload's `checksum` — rejects 401 on mismatch. Drives real trade-row updates: entry REJECTED → mark failed + alert; entry COMPLETE → backfill fill_price / slippage; SL or target COMPLETE → cancel the other leg; SL REJECTED → loud alert (position unprotected). |
| **Real transaction costs** | On close, `compute_charges` calls `kite.get_virtual_contract_note` for actual brokerage / STT / GST / exchange / SEBI / stamp. Stored on `trades.realized_costs_json`; trade detail page shows the breakdown with a source badge (`Contract note` / `Broker actuals` / `Estimate`). |
| **Pre-trade margin** | `risk.margin_usage_enabled=true` switches sizing from `entry × qty ≤ available_cash` to Kite's `order_margins` total — gives the real MIS leverage and includes broker-side charges. |
| **WebSocket** | Optional `KiteTicker` runs in threaded mode with auto-reconnect. Position-monitor subscribes to open-position symbols and reads the cached LTP (max 5s freshness) before falling back to REST. Order-update text frames are *not* bridged — HTTP postbacks with signature verification already cover that. |
| **Audit trail** | Every GTT lifecycle event (placed / modified / deleted / status_change / rejected_placement) is logged to `gtt_events` with a `details_json` payload; trade detail page renders the per-trade history. Same goes for broker order history + per-fill records via `kite.order_history` / `kite.order_trades`, surfaced behind a "Fetch from broker" toggle on the trade detail page. |

---

### How Config Impacts Trades

The settings you change directly affect trading behavior:

- **`risk.max_open_positions`** — Limits how many stocks you hold simultaneously. Lower = more conservative.
- **`risk.max_portfolio_exposure_pct`** — Caps total capital deployed. At 0.60, the system never invests more than 60% of capital.
- **`risk.daily_loss_limit_pct`** — Circuit breaker. At 0.03, if you lose 3% of capital in a day, all new trades stop.
- **`risk.min_confidence_score`** — ML confidence threshold. At 0.65, only signals with 65%+ model confidence are considered. Higher = fewer but more selective trades.
- **`scanning.shortlist_size`** — How many stocks are evaluated each cycle. Larger shortlists find more opportunities but take longer.
- **`strategy.mode`** — Controls holding period preference: `intraday` (MIS, auto-squared at EOD), `short_term`, `balanced`, or `long_term` (CNC, held overnight).
- **`execution.paper_slippage_pct`** — Simulated slippage in paper mode. Set higher (e.g., 0.3%) for more conservative backtests.
- **`risk.target_early_exit_pct`** — How close LTP must get to target before client-side detection exits (default 0.15%). Heartbeats run every 15 min; a price that gets within a paisa of target but never quite prints there would otherwise wait a full cycle. Set to 0 for exact-target behaviour.
- **`risk.llm_review_enabled`** — When on, every signal goes through Gemini for a second opinion. Gemini can APPROVE, REJECT, or RESIZE the trade.

---

## Safety Mechanisms

### Kill Switch

Available via Telegram (`/stop`, `/kill`, `/resume`) and the dashboard:

| Command | Effect |
|---------|--------|
| `/stop` | Pause all trading. Existing positions are kept, no new trades. |
| `/kill` | Square off ALL open positions at market price and pause trading. |
| `/resume` | Resume trading after a stop/kill. |

Kill switch state persists across restarts.

### Circuit Breakers

- **Daily loss limit** (default 3%): If cumulative daily losses exceed this, all new trades are blocked until next market day.
- **Weekly loss limit** (default 5%): If weekly losses exceed this, position sizing is automatically reduced by 50%.

### Other Protections

- **Mandatory stop-loss** on every trade (cannot be disabled)
- **Symbol cooldown** — Hard block on re-trading a symbol for N days after last trade
- **Symbol quarantine** — Auto-blocks symbols after 3 consecutive data fetch failures
- **Price drift rejection** — In live mode, rejects orders if the market price has drifted too far from the signal price
- **Broker circuit breaker** — Trips after 5 consecutive API failures, 30-second cooldown
- **Heartbeat overrun protection** — If a pipeline cycle takes too long, the next one is skipped (with alerts after 3 consecutive skips)

---

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Quick status summary (mode, positions, PnL, pending trades) |
| `/help` | Full command reference |
| `/status` | System health — DB, broker, LLM, Telegram connectivity |
| `/pnl` | Today's PnL summary |
| `/positions` | Open positions with entry prices, targets, stop-losses |
| `/dashboard` | Link to the web dashboard |
| `/pending` | Show trades awaiting manual approval |
| `/approve SYMBOL` | Approve a pending trade (supports overrides — see `/help`) |
| `/reject SYMBOL` | Reject a pending trade |
| `/trade BUY SYMBOL entry target sl` | Place a manual trade |
| `/stop` | Activate kill switch |
| `/kill` | Square off all positions + activate kill switch |
| `/resume` | Deactivate kill switch |
| `/auth TOKEN` | Daily Kite Connect re-authentication |
| `/holiday` | List market holidays |
| `/holiday add YYYY-MM-DD` | Add a holiday (also accepts `today`/`tomorrow`) |
| `/holiday rm YYYY-MM-DD` | Remove a holiday |

---

## Dashboard Pages

| Page | What It Shows |
|------|--------------|
| **Dashboard** | Portfolio overview, equity curve, today's PnL, quick stats |
| **Positions** | Open positions with real-time unrealized P&L |
| **Trades** | Trade history with full execution details |
| **Trade Detail** | Complete reasoning chain for a single trade (signal → risk → LLM → execution → outcome), broker order IDs, GTT lifecycle audit, broker order history on demand, transaction-cost breakdown with source badge, gross/net/% PnL |
| **Watchlist** | Sector rotation view, shortlisted symbols |
| **Holdings** | Brokerage account holdings (from Zerodha) |
| **News** | Sentiment-scored articles from all sources |
| **Calendar** | Market holidays, early close days, economic events |
| **Predictions** | Model predictions with scoring status |
| **ML Models** | Model management — versions, shadow testing, promotion, performance comparison |
| **Symbol Deep-Dive** | OHLCV chart, trades, and predictions for a specific stock |
| **Strategy** | Win rate, average gains, Sharpe ratio, performance by strategy |
| **Execution Quality** | Slippage analysis, fill quality metrics |
| **Correlations** | Inter-symbol correlation heatmap |
| **Risk Simulator** | Replay historical signals under different risk parameters |
| **Dry-Run** | Preview what signals would be generated today without executing |
| **Reports** | Daily and weekly generated reports |
| **Audit Log** | Complete system action history |
| **Data Management** | Database size, row counts, backups, cleanup |
| **Skills** | Manually trigger pipeline stages (ingest data, retrain model, etc.) |
| **Integrations** | Zerodha, Gemini, Telegram connection status and auth flow |
| **Settings** | All editable configuration parameters |

---

## Recommended Approach

If you're setting this up for the first time, here's a suggested timeline:

### Week 1: Setup and Observation

1. Deploy with Docker or run locally. Don't set Kite credentials yet — the system works fully in paper mode with free market data.
2. Set `GEMINI_API_KEY` if you want LLM features (sentiment, trade review). This is optional but improves signal quality.
3. Set up the Telegram bot — it's the easiest way to monitor the system without opening the dashboard.
4. Watch the dashboard daily. Use the **Dry-Run** page to see what signals the system generates. Don't change any risk parameters yet.

### Week 2: Paper Trading

1. Let the system run through several market days. It generates signals and executes paper trades automatically.
2. Check the **Predictions** page to see how signal accuracy tracks over time.
3. Review the **Trades** page to understand the system's behavior — entry/exit logic, position sizing, stop-loss placement.
4. Use the **Risk Simulator** to replay historical signals under different risk parameters and see how outcomes change.

### Week 3: Model Retraining

1. After enough predictions accumulate, the weekly retraining kicks in automatically (Saturday 6 AM by default). You can also trigger it manually from the **Skills** page.
2. New models enter **shadow mode** for 7 days — running predictions alongside production without affecting trades.
3. If the shadow model outperforms, it's automatically promoted. Watch the **ML Models** page for promotion events.
4. Tune risk parameters based on what you observe. Start conservative (lower exposure, fewer positions) and loosen gradually.

### Week 4: Evaluate and Decide

1. Use the **Strategy** page to review win rate, Sharpe ratio, and drawdowns.
2. Check that the model has been retrained at least once with real prediction feedback.
3. Verify that paper PnL shows positive returns after transaction costs across different market conditions.
4. Set up the **kill switch** via Telegram and test that `/stop` and `/resume` work.
5. Make sure you're comfortable with the daily Kite re-authentication routine.

If you're confident in the system, switch to live mode from **Settings**. Use the **Sync with Zerodha** button on the Integrations page to pull your actual account capital. Start with a small amount and a conservative daily loss limit you can genuinely afford to lose.

---

## Market Context

- **MIS** (Margin Intraday Square-off) — Positions are auto-squared by the broker at end of day. The system squares off at 3:15 PM IST to avoid broker's forced square-off.
- **CNC** (Cash and Carry) — Delivery trades held overnight or longer. Used for short-term and long-term strategy modes.
- **Market hours** — 9:15 AM to 3:30 PM IST, Monday through Friday. ~15 NSE holidays per year (managed via the Calendar page or Telegram).
- **Kite API rate limit** — 10 requests/second aggregate. The system respects this with built-in rate limiting.

---

## Running Tests

```bash
cd backend
PYTHONPATH=src python -m pytest tests/ -v
```
