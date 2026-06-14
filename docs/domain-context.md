> Detail extracted from the project overview (`CLAUDE.md`), which links here. Refer to it when you need this depth.

## Domain Context

- **MIS** = Margin Intraday (auto-squared by broker at EOD). **CNC** = Cash and Carry (delivery, held overnight).
- **SL** order = stop-loss limit (price + trigger_price). **SL-M** would be stop-loss market but Zerodha disabled it for retail API; `ZerodhaBroker` auto-converts incoming `SL-M` into `SL` with a 0.5% buffer past the trigger.
- **MARKET** orders auto-convert to **LIMIT** at LTP ± buffer (1% without paid data, 0.5% with paid data) — Zerodha API restriction.
- **Tick rounding** is applied automatically to every `price` and `trigger_price` in `_live_place_order` (default 0.05 tick).
- Market hours: 9:15 AM – 3:30 PM IST. Default `intraday_cutoff` = 14:30 IST (configurable; no MIS signals after).
- **GTT** (Good Till Triggered) is CNC-only at Zerodha. MIS positions get a broker-side resting LIMIT-target + SL pair (OCO enforced by position-monitor) instead.
- Kite Connect daily re-auth required (paste request_token via Telegram `/auth` or dashboard).
- SELL signals for stocks not in holdings are forced to MIS/intraday (Indian equity rules: no overnight short selling for retail).

