> Detail extracted from the project overview (`CLAUDE.md`), which links here. Refer to it when you need this depth.

## Configuration

Config is split between a YAML file (file-only keys) and a SQLite `config` table (everything else, editable via Settings UI). On first start, code defaults are populated into the `config` table. Thereafter, changes are made via UI or API and hot-applied to the running config.

**Bootstrap ordering** (`main.async_main`): DB is initialized and DB-config is applied to the in-memory `AppConfig` **before** `build_context()` constructs the broker / market_data / LLM. This ensures runtime-toggle changes (e.g. `kite_data_enabled`) actually take effect on next restart — previously they were ignored because the ingester chain was frozen during build.

### File-only keys (config.yaml)

Secrets, filesystem paths, and server binding:
- `broker.api_key`, `broker.api_secret`, `llm.api_key`
- `database.path`, `database.backup_dir`, `market_data.bhavcopy_dir`
- `dashboard.host`, `dashboard.port`, `dashboard.password`
- `log.log_dir`, `log.max_bytes`, `log.backup_count`
- `notifications.telegram.bot_token`, `notifications.telegram.chat_id`

### Key Config Sections

| Section | Notable fields |
|---------|----------------|
| `mode` | `"paper"` or `"live"` |
| `strategy.mode` | `"balanced"` / `"intraday"` / `"short_term"` / `"long_term"` |
| `strategy.holding_periods.{intraday,short_swing,week,long}.{target,stop_loss}` | ATR multipliers per holding bucket |
| `strategy.intraday_label_mode` | `"triple_barrier"` (default) — 1-min path-resolved hit-target-before-SL to session close. `"relative"` — per 5-min decision instant, forward returns-to-close ranked across the universe (assigned post-concat in `_build_intraday_matrix`; per-chunk cross-sections are too thin). Validate via `scripts/experiment.py --lanes intraday` before flipping. |
| `strategy.swing_label_mode` | `"relative"` (default) — cross-sectional relative-momentum label: per date, forward 10-bar returns ranked across the universe; top `relative_label_quantile` → BUY, bottom → SELL. Market-drift-neutral by construction; the backtest exits at the LIVE ATR geometry (breaking barrier mode's label/exit circularity). `"barrier"` = legacy absolute triple-barrier. Intraday has its own `intraday_label_mode` (triple_barrier default / relative). |
| `strategy.swing_horizon_cap_days` | Default 15 — caps the holding days the chooser may assign to ML swing trades (the swing label only measures a ~10-bar window; long_term/swing modes' 66-day tails clamp to it). 0 disables. |
| `risk` | `max_risk_per_trade_pct`, `risk_uplift_cap` (default 1.5 — ceiling on the stacked conviction/regime/flow multipliers), `max_open_positions`, `max_single_stock_pct`, `max_portfolio_exposure_pct` (counts pending notional), `max_trades_per_day` + per-product `max_mis_trades_per_day` / `max_cnc_trades_per_day` (optional), `daily_loss_limit_pct`, `weekly_loss_limit_pct`, `max_same_sector_positions`, `target_early_exit_pct` (default 0.0015), `loss_cooldown_minutes` (portfolio + per-symbol), `max_risk_rejected_retries_per_day` (default 5), `margin_usage_enabled` (default `false`), `earnings_blackout_days` (default 0), `max_portfolio_beta` (default 0), `drift_auto_suspend_enabled` (default false) |
| `risk.regime_gate` / `risk.liquidity_gate` / `risk.depth_gate` / `risk.institutional_flow` | All default-disabled. See `### Optional Risk Gates`. depth_gate is a size-multiplier, not a block. |
| `risk.reentry` | Smart re-entry after cooldown. `require_higher_confidence` now uses `confidence_tolerance` (0.85) × original AND a `min_reentry_confidence` floor (0.55) instead of strict `>` — ML confidence decays as a trend matures, so strict-greater rejected most valid re-entries. |
| `risk.exit_tweaks` | Time-stop / volume-exhaustion (default off), trailing-SL tighten step-up curve (default on). |
| `strategy` (ML training) | `class_balance_enabled` (default on), `label_cost_floor_enabled` (default on — floor the triple-barrier target at round-trip cost + slippage so a labelled win clears costs), `time_decay_last_weight` (default 1.0 = off; lower to tilt training toward recent regimes), `indicators.extended_momentum` (default on — daily/swing multi-horizon momentum + vol-regime + fracdiff features) |
| `retraining` | `max_training_days`, `min_argmax_sharpe_for_promotion`, `cv_embargo_frac` (default 0.01 — embargo on top of the label-overlap purge), and `retraining.xgb.*` XGBoost hyperparameters (`max_depth`, `learning_rate`, `n_estimators` (upper bound), `min_child_weight`, `subsample`, `colsample_bytree`, `gamma`, `reg_lambda`, `reg_alpha`, `early_stopping_rounds`, `early_stopping_min_samples`) |
| `market_hours` | `open`, `close`, `square_off`, `intraday_cutoff` |
| `execution` | `transaction_mode` (`auto`/`manual`), `max_order_retries`, `price_drift_max_pct`, `pending_expiry_minutes` (default 30) |
| `scanning` | `universe`, `shortlist_size`, `min_avg_daily_volume`, `seed_symbols` (cold-start fallback only) |
| `market_data` | `kite_data_enabled`, `news_enabled`, `scrapers_enabled`, `backfill_days` (daily, used by both ingest-universe and backfill-data), `intraday_backfill_days` (5-minute) |

### Service Toggles

| Service | Config key | Default |
|---------|-----------|---------|
| Gemini LLM | `llm.enabled` | `false` |
| News sources | `market_data.news_enabled` | `true` |
| Scrapers | `market_data.scrapers_enabled` | `true` |
| Kite data plan | `market_data.kite_data_enabled` | `false` |
| Telegram | `notifications.telegram.enabled` | `false` |
| LLM review gate | `risk.llm_review_enabled` | `true` |

