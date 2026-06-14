> Detail extracted from the project overview (`CLAUDE.md`), which links here. Refer to it when you need this depth.

## Telegram Commands

| Command | Purpose |
|---------|---------|
| `/start` | Quick status summary |
| `/help` | Full command reference |
| `/status` | System health + integration checks |
| `/pnl` | Today's PnL summary |
| `/positions` | Open positions |
| `/pending` | Show pending trades |
| `/approve SYMBOL [overrides]` | Approve pending trade (supports full/partial overrides) |
| `/reject SYMBOL` | Reject pending trade |
| `/trade BUY SYMBOL ENTRY TARGET SL [PRODUCT] [QTY]` | Manual trade |
| `/clear` | Clear today's signals + pending trades for regeneration |
| `/review [SYMBOL ...]` | ML review of any symbol or all holdings |
| `/skills` | List all registered skills |
| `/run SKILL_NAME` | Execute a skill |
| `/stop` | Pause trading (kill switch) |
| `/kill` | Emergency square-off + pause |
| `/resume` | Resume trading |
| `/auth TOKEN` | Daily Kite re-auth (also syncs to `KiteDataProvider`) |
| `/holiday [add\|rm DATE]` | Manage market holidays |
| `/watch [add\|rm SYM ...]` | List or mutate `user_watchlist` |
| `/quarantine [unblock\|replace SYM ...]` | List quarantined symbols / clear / route to replacement |
| `/lock SYM [SYM ...]` / `/unlock SYM` | Protect / unprotect holdings from auto-management |
| `/mode [auto\|manual]` | Show or hot-flip `execution.transaction_mode` (uses same `apply_db_config` path as the dashboard) |
| `/symbol SYM` | One-stop snapshot: price + day-change, quarantine, 5d avg delivery %, latest signal + top-5 attribution, last 5 trades, last 5 bulk deals |

