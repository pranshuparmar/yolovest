<div align="center">

# YoloVest

### You Only Look Once + Invest

*Hands-off, ML-driven trading for Indian markets — autonomous, self-hosted, and yours.*

[![CI](https://img.shields.io/github/actions/workflow/status/pranshuparmar/yolovest/ci.yml?style=flat-square&label=CI)](https://github.com/pranshuparmar/yolovest/actions/workflows/ci.yml) [![CodeFactor](https://www.codefactor.io/repository/github/pranshuparmar/yolovest/badge/main)](https://www.codefactor.io/repository/github/pranshuparmar/yolovest/overview/main)

<img width="2736" height="1536" alt="YoloVest Dashboard" src="https://github.com/user-attachments/assets/ec821d46-9cef-41d2-a835-29ec4e540296" />

</div>

---

<div align="center">

[**Features**](#what-you-get) • [**How It Works**](#how-it-works-the-short-version) • [**Paper vs Live**](#paper-mode-vs-live-mode) • [**Setup**](#setup)
<br>
[**Daily Re-Auth**](#daily-kite-re-authentication) • [**Using It**](#using-yolovest) • [**First Month**](#recommended-first-month) • [**Market Context**](#indian-market-context-good-to-know) • [**Docs**](#documentation)

</div>

---

**YoloVest is a self-hosted, AI-driven trading assistant for the Indian stock market.** It watches the market for you during trading hours, finds opportunities using machine learning, double-checks each one against your risk rules (and, optionally, an AI second opinion), and can place and manage trades through your Zerodha account — all on autopilot, with you in control from a web dashboard or Telegram.

**It runs in paper (simulated) mode by default.** Live trading with real money is strictly opt-in, and only after you've watched it long enough to trust it.

> **Disclaimer:** This application was vibe-coded for personal use. I do not take any responsibility for financial losses, bugs, or unexpected behavior. If you choose to run this in live mode with real money, you do so entirely at your own risk. Markets are unpredictable, software has bugs, and past performance (including backtests) does not guarantee future results. Always start with paper mode and small capital.

---

## What You Get

- **Hands-off trading** — During market hours the system continuously scans stocks, generates trade ideas, vets them, and (in live mode) executes and manages them without you lifting a finger.
- **A second opinion on every trade** — An optional AI reviewer can approve, reject, or resize trades, with its reasoning recorded so you can see *why*.
- **Real risk controls** — Position sizing, exposure caps, daily and weekly loss limits, trailing stop-losses, a one-tap kill switch, and a mandatory stop-loss on every trade.
- **Your positions stay protected** — Stop-loss and target are placed *at the broker*, so an open position is guarded even if the app restarts or your server briefly goes down.
- **It learns from itself** — Every prediction is scored against what actually happened, and that feedback flows into a weekly model refresh.
- **Try before you trust** — Paper mode simulates everything (including costs and slippage) with no real money, and a one-click "dry run" shows you exactly what the system *would* do today.
- **Research on demand** — Run the ML model against any stocks or your own holdings to get a ranked buy / sell / hold call with reasoning, set your own price alerts, and trade or manage positions manually when you want to take the wheel.
- **Monitor from anywhere** — A full web dashboard plus a Telegram bot that sends alerts and takes commands from your phone.

---

## How It Works (the short version)

You don't need to understand the internals to use YoloVest, but here's the gist:

- **A trading cycle runs every 15 minutes (configurable)** while the market is open: pull fresh prices, scan the market, rank candidates with a machine-learning model, run each through risk checks and an optional AI review, then act.
- **The brain is a machine-learning model** (gradient-boosted trees) that outputs a calibrated confidence for each stock. It's validated with realistic backtests that account for actual brokerage, taxes, and slippage — not a rosy simulation — and is **retrained weekly**. New models run quietly in "shadow" mode alongside the live one for a week and are only promoted if they genuinely perform better.
- **Market data** comes from your paid Zerodha data plan when enabled, with free public sources as an automatic fallback so the system keeps working even if one source fails. An optional live price feed enables near-instant exits.
- **Market context beyond price** is folded into the signal: news sentiment from major Indian financial outlets, India VIX, F&O option-chain signals (put-call ratio, open-interest buildup), and institutional (FII/DII) flows.
- **Self-hosted and private** — everything runs on your own server in Docker, with automatic HTTPS, database backups, and log rotation. Your data and keys never leave your machine.

---

## Paper Mode vs Live Mode

YoloVest always starts in **paper mode**. You switch modes from the Settings page when you're ready.

| | Paper Mode (default) | Live Mode |
|---|---|---|
| **Money** | Simulated — none at risk | Real orders on your Zerodha account |
| **Zerodha account** | Not required | Required |
| **Slippage & costs** | Simulated realistically and deducted from results | Real |
| **Safety controls** | Active | Active |
| **Use it to** | Evaluate the system risk-free | Trade once you trust it |

In paper mode the system still uses **real market data** (and your real holdings, if you connect Zerodha) — it just doesn't place real orders. Switch to live mode only after paper trading has earned your confidence.

---

## Setup

The recommended way to run YoloVest is with Docker on a server you control (a small cloud VM or a home machine that stays on during market hours).

### What you'll need

| | Required? | Notes |
|---|---|---|
| **A server with Docker** | Yes | Any always-on machine that can run Docker — a small cloud VM or a home server — reachable during market hours |
| **A domain name** | Recommended | For automatic HTTPS on the dashboard |
| **Zerodha Kite Connect** | Optional, recommended | Paper mode runs on free public data without it. Needed for live order execution, and for the paid data plan (see below). Sign up at [kite.trade](https://kite.trade/) |
| **Google Gemini API key** | Optional | Unlocks AI trade review and news sentiment. Free key at [ai.google.dev](https://ai.google.dev/) |
| **Telegram bot** | Optional, recommended | The easiest way to monitor and control the system from your phone — create one via [@BotFather](https://t.me/botfather) |

> **Recommended: a paid Zerodha data plan.** YoloVest works out of the box on free public data sources, but a paid **Kite Connect data subscription** is strongly recommended — it gives noticeably more accurate, lower-latency market data, unlocks the live **WebSocket** tick feed (which powers near-instant exits), and enables features the free providers can't. The free sources stay as an automatic fallback. If you care about results — and especially before going live — turn it on.

**Before you start**, a couple of host basics the rest of this guide assumes:

- **DNS + ports for HTTPS.** If you're using a domain, point a DNS **A record** at your server's public IP and make sure inbound **ports 80 and 443** are open. The HTTPS certificate is then issued automatically on first boot (it can take a few minutes). For purely local testing you can skip the domain, but you won't get HTTPS.
- **Room to train.** A fresh install backfills a few years of price history and trains an ML model from it, so give the host at least **~2 GB RAM and ~10 GB free disk** (both the database and saved models grow over time).

### Get it running

```bash
git clone https://github.com/pranshuparmar/yolovest.git
cd yolovest

# Copy the example settings and secrets files
cp backend/config.example.yaml backend/config.yaml
cp .env.example .env
```

Open `.env` and fill in what applies to you (everything except the domain is optional for paper mode):

```env
DOMAIN=yolovest.example.com         # your domain, for HTTPS
LETSENCRYPT_EMAIL=you@example.com    # for the SSL certificate
KITE_API_KEY=                        # Zerodha — leave blank for paper-only
KITE_API_SECRET=
GEMINI_API_KEY=                      # Google Gemini — optional
TELEGRAM_BOT_TOKEN=                  # Telegram — optional
TELEGRAM_CHAT_ID=
```

> **Getting your `TELEGRAM_CHAT_ID`:** after creating the bot with [@BotFather](https://t.me/botfather), send the bot any message, then open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in a browser — your numeric ID is the `chat.id` field in the response. The `.env.example` also lists an optional `MODEL_SIGNING_KEY`, only needed if you upload or import model files through the dashboard (generate one with `python -c "import secrets; print(secrets.token_hex(32))"`).

Then start everything:

```bash
docker compose up -d --build
```

The dashboard comes up at `https://your-domain` — give it a few minutes on the very first boot, while it runs database migrations, warms up its data/broker/LLM connections, and provisions the HTTPS certificate. **Log in with the default password `yolovest` and change it immediately** from the Settings page — it's the only thing standing between the internet and your trading controls. Docker handles HTTPS certificates, the web server, database backups, and log rotation automatically, and your data persists across restarts.

### First run: seed data and train your first model

**A brand-new install has no price history and no trained model yet**, so it won't generate any trade ideas until you bootstrap it once. (It fails safe in the meantime — while no model exists, signal generation simply does nothing rather than erroring.) Two one-time steps get you there:

1. **Seed historical data.** Pull a few years of daily price history for the whole scanning universe, so the model has something to learn from. Run the **`ingest-universe`** task — from Telegram send `/run ingest-universe`, or trigger it on demand from the dashboard's **Admin** area. It fetches ~3 years (configurable) of daily bars across the Nifty 500; the first run takes a while because the free data providers are rate-limited.
2. **Train your first model.** Once the history is in, run **`model-retrain`** — `/run model-retrain` from Telegram, or from the dashboard's **Admin** area. It needs at least ~200 daily bars to train (step 1 provides far more); below that it safely skips. Training takes a few minutes.

In Telegram, send `/skills` to see every task you can run on demand this way.

> **Want intraday signals too?** Also run **`backfill-intraday`** before retraining — it seeds 5-minute history so the intraday model can train alongside the daily (swing) one. The default "balanced" strategy works fine with just the daily model to start, so this is optional.

After the model is trained, the next trading cycle starts producing signals — and from then on the **weekly retrain keeps the model fresh automatically**, so you won't need to repeat these steps. Now you're paper trading with free market data.

---

## Daily Kite Re-Authentication

Zerodha expires its API access every morning (around 6 AM IST) for security. If you've connected Zerodha (for live trading, or for paid real-time data), you re-authorize once per trading day:

1. Each morning the Telegram bot sends you a Zerodha login link (default 8:30 AM IST).
2. Tap it, log in to Zerodha, and you're done — the authorization is captured automatically.
3. If that ever doesn't go through, you can paste the login token into Telegram or the dashboard's Integrations page as a fallback.

The authorization is remembered for the rest of the day, even if you restart the app. Paper mode with free data needs none of this.

---

## Using YoloVest

### The Dashboard

A web dashboard gives you a full view of the system, organized into a few areas:

- **Trading** — your portfolio and equity curve, open positions with live P&L, trade history, and a detailed breakdown of any single trade (why it was taken, how it was executed, and how it turned out).
- **Research** — sentiment-scored news, a market calendar (holidays and economic events), the watchlist/shortlist, brokerage holdings, an on-demand **screener** that runs the model against any stocks you choose, your own **price alerts**, institutional (FII/DII) flows, and a cross-stock correlation view.
- **Models & predictions** — how accurate the system has been, model versions and their shadow-test results, and a one-click **dry run** that previews today's signals without trading.
- **Analysis** — win rate, returns, risk-adjusted performance, execution/slippage quality, and a risk simulator that replays history under different settings.
- **Admin** — settings, data management and backups, an audit log, and manual controls: place and manage your own orders, close positions, tighten stop-losses, lock holdings so the system won't touch them, and run any pipeline step on demand or toggle its schedule.

### Telegram Bot

Once connected, the bot sends real-time alerts (trade entries/exits, daily summaries, warnings) and takes commands:

| Command | What it does |
|---|---|
| `/start` | Quick status: mode, positions, P&L, anything awaiting approval |
| `/help` | Full command reference |
| `/status` | System health check |
| `/pnl` | Today's profit and loss |
| `/positions` | Open positions with targets and stop-losses |
| `/pending` | Trades waiting for your approval |
| `/approve SYMBOL` | Approve a pending trade (overrides supported) |
| `/reject SYMBOL` | Reject a pending trade |
| `/trade BUY SYMBOL entry target sl` | Place a manual trade |
| `/stop` | Pause new trading (keep existing positions) |
| `/kill` | Close everything and pause |
| `/resume` | Resume trading |
| `/auth TOKEN` | Daily Zerodha re-authorization (fallback) |
| `/holiday` | View or edit the market-holiday list |
| `/dashboard` | High-level overview of portfolio, trades, and system |
| `/skills` | List the background tasks you can run on demand |
| `/run TASK` | Run a task now — e.g. `/run ingest-universe`, `/run model-retrain` |

### What You Can Tune

Almost everything is adjustable live from the **Settings** page — changes take effect immediately, no restart needed. The main levers:

- **Capital & risk** — starting capital, how much to risk per trade, how many positions to hold at once, the cap on total money deployed, daily and weekly loss limits, and how confident the model must be before acting.
- **Strategy** — your holding-period style (intraday, short-term, balanced, or long-term), which technical indicators to use, and market-condition filters.
- **What to trade** — which universe of stocks to scan, how many to evaluate each cycle, and minimum liquidity.
- **Execution** — simulated slippage for paper mode, order retry behavior, and how far the price may drift before a live order is rejected.
- **Costs** — brokerage and tax rates (pre-filled with current Zerodha/NSE figures) so simulated and real P&L are honest.
- **Services** — turn the AI reviewer, news, scrapers, paid data, live price feed, and Telegram on or off independently.
- **Schedules** — how often the trading cycle runs, when reports are sent, and when weekly retraining happens.

### Safety Mechanisms

- **Kill switch** — `/stop` pauses new trades; `/kill` closes everything immediately; `/resume` re-enables. The state survives restarts.
- **Circuit breakers** — hit your daily loss limit and new trading halts until the next market day; hit the weekly limit and position sizes are automatically cut.
- **Always-on protections** — a stop-loss on every trade (can't be turned off), per-symbol cooldowns, automatic blocking of symbols with unreliable data, rejection of trades when the price has moved too far from the signal, and protection against the system overrunning itself.

---

## Recommended First Month

A gentle on-ramp from "just watching" to "trading with confidence":

**Week 1 — Set up and observe.** Deploy in paper mode (no Zerodha needed). Connect Telegram so you can watch from your phone. Use the **dry run** to see what the system would do each day. Don't change any risk settings yet.

**Week 2 — Paper trade.** Let it run for several market days. Watch the **predictions** to see how accuracy holds up, and the **trade history** to understand its entry/exit and sizing behavior. Try the **risk simulator** to see how different settings would have changed outcomes.

**Week 3 — Let it learn.** Once enough history accumulates, the weekly retrain runs automatically (you can also trigger it manually). New models shadow-test for a week and are promoted only if they're better. Start nudging risk settings — begin conservative and loosen slowly.

**Week 4 — Decide.** Review win rate, risk-adjusted returns, and drawdowns. Confirm paper results are positive *after* costs across different market days. Test the kill switch. Get comfortable with the daily Zerodha re-auth.

Only then, if you're convinced, switch to live mode — pull your real account capital, start small, and set a daily loss limit you can genuinely afford.

---

## Indian Market Context (good to know)

- **Intraday vs delivery** — Intraday positions are auto-closed by the broker at end of day (the system squares off a bit early, at 3:15 PM IST, to stay ahead of that). Delivery positions are held overnight or longer, used by the short- and long-term strategy styles.
- **Market hours** — 9:15 AM to 3:30 PM IST, Monday to Friday, minus ~15 holidays a year (managed from the Calendar page or Telegram).
- **Short selling** — Indian rules don't allow retail overnight short selling, so any "sell" idea on a stock you don't own is handled as an intraday trade.

---

## Documentation

Curious about the internals? Start with **[CLAUDE.md](CLAUDE.md)** for the high-level architecture overview, then dive into the [`docs/`](docs/) folder:

- **[docs/architecture.md](docs/architecture.md)** — subsystems, heartbeat pipeline, skills, risk gates, exit paths, inter-skill contracts
- **[docs/database.md](docs/database.md)** — key tables, quarantine, universe resolution
- **[docs/configuration.md](docs/configuration.md)** — file-only keys, config sections, service toggles
- **[docs/key-files.md](docs/key-files.md)** — file-by-file backend / frontend / infra map
- **[docs/domain-context.md](docs/domain-context.md)** — Indian-market trading specifics
- **[docs/conventions.md](docs/conventions.md)** — coding conventions
- **[docs/telegram-commands.md](docs/telegram-commands.md)** — full Telegram bot command reference
- **[docs/tls.md](docs/tls.md)** — TLS / nginx-proxy reliability overview
- **[docs/tls-recovery.md](docs/tls-recovery.md)** — TLS / nginx-proxy recovery runbook
