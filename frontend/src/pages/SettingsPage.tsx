import { useState, useEffect, useCallback, useMemo, useRef, createContext, useContext } from "react";
import { useConfig, useConfigDefaults, useUpdateConfig, useImportConfig } from "../hooks/queries";
import { api } from "../api/endpoints";
import clsx from "clsx";

// Defaults flow into the InfoIcon tooltip via context so we don't have
// to thread the value through 5 field-component layers. SettingsPage
// populates this once defaults are loaded.
const DefaultsContext = createContext<Record<string, unknown>>({});

// Field type registry built from the originally-loaded /api/config
// response. JSON loses Python's int/float distinction (both → Number
// in JS), but `_parse_db_value` on the backend preserves int values as
// JSON integers and floats as JSON numbers with a fractional part. We
// capture that distinction at first load so NumberField knows whether
// to allow decimals — preventing leaks like 10.5 being saved to a
// max_trades_per_day field and crashing Pydantic.
type FieldKind = "int" | "float";
const FieldTypesContext = createContext<Record<string, FieldKind>>({});

// Explicit fallback list for fields whose value is null on both the
// live config and the defaults endpoint, so the heuristic can't
// classify them. Currently the Optional[int] caps; extend whenever a
// new Optional[int] field is added.
const EXPLICIT_INT_KEYS: ReadonlySet<string> = new Set([
  "risk.max_mis_trades_per_day",
  "risk.max_cnc_trades_per_day",
]);

function inferFieldType(value: unknown): FieldKind | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Number.isInteger(value) ? "int" : "float";
}

// Per-key fallback semantics for Optional fields. When the field's
// own default is None, the system uses one of these at runtime:
//   - a reference to another config key (string starting with "ref:")
//   - a fixed numeric default (number)
//   - a descriptive note (string)
// The InfoIcon tooltip surfaces this so "not set" doesn't read as a
// dead end.
const FALLBACK_DEFAULTS: Record<string, string | number> = {
  "risk.min_confidence_buy_intraday": "ref:risk.min_confidence_buy",
  "risk.min_confidence_sell_intraday": "ref:risk.min_confidence_sell",
  "risk.min_confidence_buy_swing": "ref:risk.min_confidence_buy",
  "risk.min_confidence_sell_swing": "ref:risk.min_confidence_sell",
  "risk.buy_threshold_override": "uses the model's bootstrap-tuned threshold from the saved artifact",
  "risk.sell_threshold_override": "uses the model's bootstrap-tuned threshold from the saved artifact",
  "risk.max_mis_trades_per_day": "disabled — only the combined Max Trades / Day cap applies",
  "risk.max_cnc_trades_per_day": "disabled — only the combined Max Trades / Day cap applies",
};

function formatDefaultValue(v: unknown): string {
  // Optional fields default to None. What "not set" means at runtime
  // depends on the field (some fall back to a general value, some
  // disable the gate entirely) — the field's description explains it.
  if (v === null || v === undefined) return "not set";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "string") return v.length === 0 ? '""' : v;
  if (Array.isArray(v)) return `[${v.map((x) => formatDefaultValue(x)).join(", ")}]`;
  if (typeof v === "object") {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return String(v);
}

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------

interface Tab {
  id: string;
  label: string;
  sections: string[]; // ordered list of section keys to show
}

const TABS: Tab[] = [
  {
    id: "general",
    label: "General",
    sections: ["_general_top", "llm", "market_data", "heartbeat", "news_digest", "dashboard"],
  },
  {
    id: "strategy",
    label: "Strategy",
    sections: [
      "_strategy_top",
      "_strategy_mis",
      "_strategy_cnc",
      "_strategy_features",
      "strategy",
      "scanning",
      "retraining",
      "scoring",
    ],
  },
  {
    id: "risk",
    label: "Risk & Execution",
    sections: ["risk", "execution", "_risk_mis", "_risk_cnc", "transaction_costs"],
  },
  {
    id: "schedule",
    label: "Schedules",
    sections: ["_cron_schedules", "market_hours", "database"],
  },
  {
    id: "notifications",
    label: "Notifications",
    sections: ["notifications"],
  },
];

// ---------------------------------------------------------------------------
// Virtual sections — pull keys from multiple real sections
// ---------------------------------------------------------------------------

// Keys shown in the "General > Top" card (mode + capital)
const GENERAL_TOP_KEYS = ["mode", "capital.initial_amount", "log.level", "log.file_level"];

// Strategy tab top card — strategy.mode is the master control for the
// whole tab so it sits first instead of buried inside the long strategy
// section; scanning.universe is the other top-of-funnel choice.
const STRATEGY_TOP_KEYS = [
  "strategy.mode",
  "scanning.universe",
  "scanning.shortlist_size",
  "scanning.min_avg_daily_volume",
];

// Per-product strategy settings. ATR target/stop multipliers and the
// intraday-only eligibility / bias knobs live here. Swing buckets
// (short_swing / week / long) all map to CNC at the broker so they
// share the CNC section.
const STRATEGY_MIS_KEYS = [
  "strategy.holding_periods.intraday.target",
  "strategy.holding_periods.intraday.stop_loss",
  "strategy.holding_periods.intraday.max_atr_pct_for_target",
  "strategy.max_atr_pct_for_intraday_eligibility",
  "strategy.bull_bias_intraday_pct",
];

const STRATEGY_CNC_KEYS = [
  "strategy.holding_periods.short_swing.target",
  "strategy.holding_periods.short_swing.stop_loss",
  "strategy.holding_periods.week.target",
  "strategy.holding_periods.week.stop_loss",
  "strategy.holding_periods.long.target",
  "strategy.holding_periods.long.stop_loss",
];

// Optional support feature groups the model trains on (price/technical
// features are always the primary core). Grouped into their own card so
// it's clear these are toggleable add-ons.
const STRATEGY_FEATURE_KEYS = [
  "strategy.feature_groups.regime",
  "strategy.feature_groups.sector",
  "strategy.feature_groups.institutional",
  "strategy.feature_groups.news",
  "strategy.feature_groups.vix",
  "strategy.feature_groups.fno",
  "strategy.feature_groups.feedback",
];

// Per-product risk settings — intraday/swing in the model maps 1:1 to
// MIS/CNC at the broker, so these virtual sections group the knobs the
// user actually thinks about as "MIS rules" vs "CNC rules".
const RISK_MIS_KEYS = [
  "risk.max_mis_trades_per_day",
  "risk.min_confidence_buy_intraday",
  "risk.min_confidence_sell_intraday",
  "risk.trailing_sl_trigger_target_pct_intraday",
  "risk.exit_tweaks.time_stop_enabled",
  "risk.exit_tweaks.intraday_stop_after_min",
  "risk.exit_tweaks.intraday_stop_progress_threshold",
];

const RISK_CNC_KEYS = [
  "risk.max_cnc_trades_per_day",
  "risk.min_confidence_buy_swing",
  "risk.min_confidence_sell_swing",
  "risk.trailing_sl_trigger_target_pct_swing",
];

// All cron/schedule-related keys, pulled from various sections into one card
const CRON_KEYS = [
  "heartbeat.auth_broker_cron",
  "heartbeat.ingest_premarket_cron",
  "scanning.universe_cron",
  "news_digest.schedule_cron",
  "reports.daily_report_time",
  "reports.weekly_report_cron",
  "retraining.schedule_cron",
  "database.backup_cron",
  "scoring.auto_score_cron",
];

// Keys to hide from their original sections (shown in virtual sections instead)
const RELOCATED_KEYS = new Set([
  ...GENERAL_TOP_KEYS,
  ...CRON_KEYS,
  ...STRATEGY_TOP_KEYS,
  ...STRATEGY_MIS_KEYS,
  ...STRATEGY_CNC_KEYS,
  ...STRATEGY_FEATURE_KEYS,
  ...RISK_MIS_KEYS,
  ...RISK_CNC_KEYS,
]);

// ---------------------------------------------------------------------------
// Enum options for select fields
// ---------------------------------------------------------------------------

const SELECT_OPTIONS: Record<string, { value: string; label: string }[]> = {
  "mode": [
    { value: "paper", label: "Paper Trading" },
    { value: "live", label: "Live Trading" },
  ],
  "strategy.mode": [
    { value: "intraday", label: "Intraday" },
    { value: "short_term", label: "Short Term" },
    { value: "balanced", label: "Balanced" },
    { value: "long_term", label: "Long Term" },
    { value: "swing", label: "Swing (Short + Long, no MIS)" },
  ],
  "scanning.universe": [
    { value: "nifty50", label: "Nifty 50" },
    { value: "nifty100", label: "Nifty 100" },
    { value: "nifty200", label: "Nifty 200" },
    { value: "nifty500", label: "Nifty 500" },
  ],
  "execution.transaction_mode": [
    { value: "auto", label: "Auto (execute immediately)" },
    { value: "manual", label: "Manual (require approval)" },
  ],
  "risk.holding_expiry.action": [
    { value: "tighten_or_close", label: "Tighten or Close" },
    { value: "force_close", label: "Force Close" },
    { value: "ignore", label: "Ignore" },
  ],
  "risk.weekly_reset_day": [
    { value: "monday", label: "Monday" },
    { value: "tuesday", label: "Tuesday" },
    { value: "wednesday", label: "Wednesday" },
    { value: "thursday", label: "Thursday" },
    { value: "friday", label: "Friday" },
  ],
  "log.level": [
    { value: "DEBUG", label: "Debug" },
    { value: "INFO", label: "Info" },
    { value: "WARNING", label: "Warning" },
    { value: "ERROR", label: "Error" },
  ],
  "log.file_level": [
    { value: "DEBUG", label: "Debug" },
    { value: "INFO", label: "Info" },
    { value: "WARNING", label: "Warning" },
    { value: "ERROR", label: "Error" },
  ],
};

// Read-only informational fields (current provider implementations)
const READ_ONLY_KEYS = new Set([
  "market_data.daily_provider",
  "market_data.daily_fallback",
  "market_data.intraday_provider",
  "market_hours.timezone",
]);

// Nullable numeric fields. When null, the model falls back to a global
// default (documented in the tooltip). Renders as an empty input with a
// "(global)" placeholder; clearing the input sends null back to the API.
const NULLABLE_NUMBER_KEYS = new Set([
  "risk.min_confidence_buy_intraday",
  "risk.min_confidence_sell_intraday",
  "risk.min_confidence_buy_swing",
  "risk.min_confidence_sell_swing",
  "risk.buy_threshold_override",
  "risk.sell_threshold_override",
  "risk.trailing_sl_trigger_target_pct_intraday",
  "risk.trailing_sl_trigger_target_pct_swing",
  "risk.max_mis_trades_per_day",
  "risk.max_cnc_trades_per_day",
]);

// ---------------------------------------------------------------------------
// Section labels
// ---------------------------------------------------------------------------

const SECTION_LABELS: Record<string, string> = {
  _general_top: "General",
  _strategy_top: "Strategy — Core",
  _strategy_mis: "MIS (Intraday) — Holding Geometry",
  _strategy_cnc: "CNC (Delivery) — Holding Geometry",
  _strategy_features: "Feature Groups (price/technical always on)",
  _cron_schedules: "Cron Schedules",
  _risk_mis: "MIS (Intraday) Specific",
  _risk_cnc: "CNC (Delivery) Specific",
  capital: "Capital",
  llm: "LLM (Gemini)",
  market_data: "Market Data",
  heartbeat: "Heartbeat",
  scanning: "Scanning",
  strategy: "Strategy",
  risk: "Risk Management",
  market_hours: "Market Hours",
  execution: "Execution",
  transaction_costs: "Transaction Costs",
  database: "Data Retention & Backups",
  retraining: "Model Retraining",
  scoring: "Auto-Scoring",
  reports: "Reports",
  dashboard: "Dashboard",
  notifications: "Notifications",
  news_digest: "News Digest",
};

// Full-key labels checked first, then last-segment fallback
const FULL_KEY_LABELS: Record<string, string> = {
  "mode": "Trading Mode",
  "capital.initial_amount": "Initial Capital (INR)",
  "log.level": "Console Log Level",
  "log.file_level": "File Log Level",
  "llm.enabled": "Gemini LLM",
  "llm.model": "Gemini Model",
  "market_data.daily_provider": "Daily Provider",
  "market_data.daily_fallback": "Daily Fallback",
  "market_data.intraday_provider": "Intraday Provider",
  "market_data.kite_data_enabled": "Kite Data (paid plan)",
  "market_data.depth_snapshots_enabled": "Archive order-book depth (bid/ask, full + top-5 quantities) for the watchlist each heartbeat via one batched Kite quote call. Pure data collection — nothing trades on it. Builds the order-flow dataset that can eventually make an intraday model viable. Requires Kite data.",
  "market_data.depth_snapshot_retention_days": "Self-pruned retention for depth snapshots. Keep >= ~400 so a year of history survives for offline experiments.",
  "market_data.kite_websocket_enabled": "Kite WebSocket Feed",
  "market_data.max_signal_data_age_trading_days": "Max Signal Data Age (trading days)",
  "market_data.news_enabled": "News Sources",
  "market_data.scrapers_enabled": "Web Scrapers",
  "market_data.cache_ttl_minutes": "Cache TTL (min)",
  "market_data.stale_threshold_minutes": "Stale Threshold (min)",
  "market_data.sentiment_ttl_hours": "Sentiment TTL (hrs)",
  "market_data.backfill_days": "Backfill History — Daily (days)",
  "market_data.intraday_backfill_days": "Backfill History — Intraday (days)",
  "heartbeat.market_hours_interval_min": "Market Hours Interval (min)",
  "heartbeat.off_hours_interval_min": "Off Hours Interval (min)",
  "heartbeat.max_consecutive_skips": "Max Consecutive Skips",
  "scanning.universe": "Stock Universe",
  "scanning.shortlist_size": "Shortlist Size",
  "scanning.min_avg_daily_volume": "Min Avg Daily Volume",
  "scanning.seed_symbols": "Seed Symbols",
  "scanning.weights.technical": "Weight: Technical",
  "scanning.weights.volume_momentum": "Weight: Volume Momentum",
  "scanning.weights.news_sentiment": "Weight: News Sentiment",
  "scanning.weights.fundamental": "Weight: Fundamental",
  "scanning.weights.volatility": "Weight: Volatility",
  "scanning.rotation_enabled": "Rotate Stale Symbols",
  "scanning.rotation_no_signal_threshold": "Rotation Threshold (heartbeats)",
  "scanning.rotation_cooldown_hours": "Rotation Cooldown (hrs)",
  "strategy.mode": "Strategy Mode",
  "strategy.min_training_samples": "Min Training Samples",
  "strategy.ema_periods": "EMA Periods",
  "strategy.allowed_holding_periods": "Allowed Holding Periods",
  "strategy.holding_periods.intraday.target": "Intraday Target (ATR×)",
  "strategy.holding_periods.intraday.stop_loss": "Intraday Stop Loss (ATR×)",
  "strategy.holding_periods.intraday.max_atr_pct_for_target": "Intraday ATR Cap (target geometry)",
  "strategy.holding_periods.short_swing.target": "Short Swing Target (ATR×)",
  "strategy.holding_periods.short_swing.stop_loss": "Short Swing Stop Loss (ATR×)",
  "strategy.holding_periods.week.target": "Weekly Target (ATR×)",
  "strategy.holding_periods.week.stop_loss": "Weekly Stop Loss (ATR×)",
  "strategy.holding_periods.long.target": "Long Target (ATR×)",
  "strategy.holding_periods.long.stop_loss": "Long Stop Loss (ATR×)",
  "strategy.volatility.min_atr_pct": "Min ATR%",
  "strategy.volatility.max_atr_pct": "Max ATR%",
  "strategy.volatility.ideal_min_atr_pct": "Ideal Min ATR%",
  "strategy.volatility.ideal_max_atr_pct": "Ideal Max ATR%",
  "strategy.volatility.max_atr_pct_for_intraday_eligibility": "Max ATR% for Intraday Eligibility",
  "strategy.indicators.rsi": "RSI",
  "strategy.indicators.macd": "MACD",
  "strategy.indicators.bollinger_bands": "Bollinger Bands",
  "strategy.indicators.vwap": "VWAP",
  "strategy.indicators.atr": "ATR",
  "strategy.indicators.volume_profile": "Volume Profile",
  "strategy.indicators.obv": "OBV",
  "strategy.indicators.supertrend": "SuperTrend",
  // Market Regime
  "strategy.market_regime.enabled": "Market Regime Detection",
  "strategy.market_regime.index_symbol": "Benchmark Index",
  "strategy.market_regime.lookback_days": "Regime Lookback (days)",
  "strategy.market_regime.bull_bias_intraday_pct": "Bull Intraday Bias",
  "strategy.market_regime.bear_max_holding_days": "Bear Max Holding Days",
  "strategy.market_regime.range_prefer_mean_reversion": "Range: Prefer Mean Reversion",
  // Feedback
  "strategy.feature_groups.regime": "Support: Market Regime",
  "strategy.feature_groups.sector": "Support: Sector-Relative",
  "strategy.feature_groups.institutional": "Support: Bulk Deals / Delivery",
  "strategy.feature_groups.news": "Support: News Sentiment",
  "strategy.feature_groups.vix": "Support: India VIX",
  "strategy.feature_groups.fno": "Support: F&O Option Chain",
  "strategy.feature_groups.feedback": "Support: Feedback Loop",
  "strategy.feedback.enabled": "ML Feedback Loop",
  "strategy.feedback.lookback_days": "Feedback Lookback (days)",
  "strategy.feedback.sample_weight_boost": "Sample Weight Boost",
  "strategy.feedback.sources.predictions": "Source: Predictions",
  "strategy.feedback.sources.dry_runs": "Source: Dry Runs",
  "strategy.feedback.sources.trades": "Source: Trades",
  "strategy.class_balance_enabled": "Class-Balanced Training",
  "strategy.class_balance_min_pct": "Class Balance Min Share",
  "strategy.post_train_class_check_enabled": "Post-Train Class Check",
  "risk.max_risk_per_trade_pct": "Max Risk / Trade",
  "risk.max_portfolio_exposure_pct": "Max Portfolio Exposure",
  "risk.max_open_positions": "Max Open Positions",
  "risk.max_single_stock_pct": "Max Single Stock Exposure",
  "risk.max_pct_per_signal": "Per-Signal Allocation Cap",
  "risk.daily_loss_limit_pct": "Daily Loss Limit",
  "risk.weekly_loss_limit_pct": "Weekly Loss Limit",
  "risk.weekly_loss_sizing_reduction": "Weekly Loss Size Reduction",
  "risk.mandatory_stop_loss": "Mandatory Stop Loss",
  "risk.trailing_sl_enabled": "Trailing Stop Loss",
  "risk.trailing_sl_trigger_multiple": "Trailing SL Trigger (× risk, legacy)",
  "risk.trailing_sl_trigger_target_pct_intraday": "Trailing SL Trigger — Intraday (% of target)",
  "risk.trailing_sl_trigger_target_pct_swing": "Trailing SL Trigger — Swing (% of target)",
  "risk.trailing_sl_step_pct": "Trailing SL Step",
  "risk.target_early_exit_pct": "Target Early-Exit Buffer",
  "risk.min_confidence_buy": "Min Confidence (BUY)",
  "risk.min_confidence_sell": "Min Confidence (SELL)",
  "risk.min_confidence_buy_intraday": "Min Confidence (BUY · Intraday)",
  "risk.min_confidence_sell_intraday": "Min Confidence (SELL · Intraday)",
  "risk.min_confidence_buy_swing": "Min Confidence (BUY · Swing)",
  "risk.min_confidence_sell_swing": "Min Confidence (SELL · Swing)",
  "risk.skip_sell_on_holdings": "Skip SELL on Holdings",
  "risk.max_trades_per_day": "Max Trades / Day",
  "risk.max_mis_trades_per_day": "Max MIS Trades / Day",
  "risk.max_cnc_trades_per_day": "Max CNC Trades / Day",
  "risk.kill_switch_enabled": "Kill Switch",
  "risk.drift_auto_suspend_enabled": "Auto-Suspend on Model Drift",
  "risk.earnings_blackout_days": "Earnings Blackout (days)",
  "risk.max_portfolio_beta": "Max Portfolio Beta",
  "risk.llm_review_enabled": "LLM Trade Review",
  "risk.llm_fallback_to_rules": "Fallback to Rules if LLM Down",
  "risk.max_same_sector_positions": "Max Same Sector Positions",
  "risk.margin_usage_enabled": "Margin / Leverage",
  "risk.weekly_reset_day": "Weekly PnL Reset Day",
  "risk.loss_cooldown_minutes": "Loss Cooldown (min)",
  "risk.symbol_cooldown_days": "Symbol Cooldown (days)",
  "risk.symbol_repeat_lookback_days": "Repeat Symbol Lookback (days)",
  "risk.symbol_repeat_min_confidence": "Repeat Symbol Min Confidence",
  "risk.tuned_threshold_max_diff": "Tuned Threshold Asymmetry Cap",
  "risk.buy_threshold_override": "Tuned Threshold Override (BUY)",
  "risk.sell_threshold_override": "Tuned Threshold Override (SELL)",
  "risk.min_net_rr": "Min Cost-Adjusted R:R",
  // Regime gate (new)
  "risk.max_risk_rejected_retries_per_day": "Risk-Rejected Retry Cap / Day",
  "risk.regime_gate.enabled": "Regime Gate",
  "risk.regime_gate.min_breadth_for_buy": "Regime: Min Breadth for BUY",
  "risk.regime_gate.max_breadth_for_sell": "Regime: Max Breadth for SELL",
  "risk.regime_gate.bullish_breadth_threshold": "Regime: Bullish Threshold",
  "risk.regime_gate.bearish_breadth_threshold": "Regime: Bearish Threshold",
  "risk.regime_gate.bullish_size_multiplier": "Regime: Bullish Size Multiplier",
  "risk.regime_gate.bearish_size_multiplier": "Regime: Bearish Size Multiplier",
  // Liquidity gate (new)
  "risk.liquidity_gate.enabled": "Liquidity Gate",
  "risk.liquidity_gate.max_pct_of_top5": "Liquidity: Max % of Top-5 Depth",
  // Depth gate (new)
  "risk.depth_gate.enabled": "Depth Imbalance Gate",
  "risk.depth_gate.min_imbalance_for_buy": "Depth: Min Imbalance for BUY",
  "risk.depth_gate.max_imbalance_for_sell": "Depth: Max Imbalance for SELL",
  // Institutional flow (new)
  "risk.institutional_flow.enabled": "Institutional Flow Sizing",
  "risk.institutional_flow.bulk_deal_lookback_days": "Inst. Flow: Bulk-Deal Lookback (days)",
  "risk.institutional_flow.bulk_deal_size_multiplier": "Inst. Flow: Bulk-Deal Multiplier",
  "risk.institutional_flow.fii_net_threshold_cr": "Inst. Flow: FII Net Threshold (₹ Cr)",
  "risk.institutional_flow.fii_aligned_size_multiplier": "Inst. Flow: FII Aligned Multiplier",
  "risk.market_trend_filter.enabled": "Market Trend Filter",
  "risk.market_trend_filter.ma_window": "Trend Filter: MA Window (days)",
  "scoring.auto_score_enabled": "Auto-Score Enabled",
  // Exit tweaks (new)
  "risk.exit_tweaks.time_stop_enabled": "Intraday Time-Stop",
  "risk.exit_tweaks.intraday_stop_after_min": "Time-Stop: Trigger After (min)",
  "risk.exit_tweaks.intraday_stop_progress_threshold": "Time-Stop: Progress Threshold",
  "risk.exit_tweaks.volume_exit_enabled": "Volume-Exhaustion Exit",
  "risk.exit_tweaks.volume_exit_lookback_bars": "Volume Exit: Lookback (5-min bars)",
  "risk.exit_tweaks.volume_exit_min_ratio": "Volume Exit: Min Ratio",
  "risk.exit_tweaks.tighten_trailing_enabled": "Trailing-SL Tighten Near Target",
  "risk.exit_tweaks.tighten_start_at_target_pct": "Tighten: Start at Target Progress",
  "risk.exit_tweaks.tighten_step_size": "Tighten: Step Size (target progress)",
  "risk.exit_tweaks.tighten_step_decay": "Tighten: Decay Per Step",
  "risk.exit_tweaks.tighten_min_multiplier": "Tighten: Min Multiplier (floor)",
  // Execution
  "execution.pending_expiry_minutes": "Pending Trade Auto-Expiry (min)",
  // Partial Profit Booking
  "risk.partial_profit.enabled": "Partial Profit Booking",
  "risk.partial_profit.first_target_pct": "First Target (%)",
  "risk.partial_profit.first_close_pct": "Close Portion (%)",
  "risk.partial_profit.move_sl_to_breakeven": "Move SL to Breakeven",
  // Conviction Sizing
  "risk.conviction_sizing.enabled": "Conviction Sizing",
  "risk.conviction_sizing.min_multiplier": "Min Size Multiplier",
  "risk.conviction_sizing.max_multiplier": "Max Size Multiplier",
  "risk.conviction_sizing.confidence_floor": "Confidence Floor",
  "risk.conviction_sizing.confidence_ceiling": "Confidence Ceiling",
  // Correlation Limits
  "risk.correlation_limit.enabled": "Correlation Limits",
  "risk.correlation_limit.max_correlated_positions": "Max Correlated Positions",
  "risk.correlation_limit.correlation_threshold": "Correlation Threshold",
  "risk.correlation_limit.lookback_days": "Correlation Lookback (days)",
  // Re-entry
  "risk.reentry.enabled": "Smart Re-entry",
  "risk.reentry.min_bars_after_exit": "Min Bars After Exit",
  "risk.reentry.min_price_move_pct": "Min Price Move",
  "risk.reentry.max_reentries_per_symbol": "Max Re-entries / Symbol / Day",
  "risk.reentry.require_higher_confidence": "Require Higher Confidence",
  // Holding Expiry
  "risk.holding_expiry.enabled": "Holding Expiry",
  "risk.holding_expiry.action": "Expiry Action",
  "risk.holding_expiry.breakeven_buffer_pct": "Breakeven Buffer",
  "risk.holding_expiry.loss_threshold_pct": "Loss Threshold",
  "risk.holding_expiry.max_holding_days": "Max Holding Days",
  "execution.max_order_retries": "Max Order Retries",
  "execution.retry_base_delay_sec": "Retry Base Delay (sec)",
  "execution.paper_slippage_pct": "Paper Slippage",
  "execution.order_timeout_sec": "Order Timeout (sec)",
  "execution.price_drift_max_pct": "Max Price Drift",
  "execution.transaction_mode": "Transaction Mode",
  "execution.rejection_cooldown_hours": "Rejection Cooldown (hours)",
  // Scaled Entry
  "execution.scaled_entry.enabled": "Scaled Entry",
  "execution.scaled_entry.legs": "Entry Legs",
  "execution.scaled_entry.second_leg_offset_pct": "2nd Leg Offset",
  "execution.scaled_entry.second_leg_delay_sec": "2nd Leg Delay (sec)",
  "transaction_costs.brokerage_per_leg_pct": "Brokerage / Leg",
  "transaction_costs.brokerage_cap_per_leg": "Brokerage Cap / Leg (INR)",
  "transaction_costs.stt_intraday_pct": "STT Intraday",
  "transaction_costs.stt_delivery_pct": "STT Delivery",
  "transaction_costs.other_charges_pct": "Other Charges",
  "market_hours.open": "Market Open",
  "market_hours.close": "Market Close",
  "market_hours.order_start": "Order Start",
  "market_hours.order_end": "Order End",
  "market_hours.square_off": "Square Off Time",
  "market_hours.square_off_extension": "Square Off Extension",
  "market_hours.intraday_cutoff": "Intraday Cutoff",
  "market_hours.timezone": "Timezone",
  "market_hours.holidays": "Market Holidays",
  "market_hours.early_close_days": "Early Close Days",
  "database.backup_enabled": "Backups Enabled",
  "database.retention.ohlcv_days": "Daily OHLCV Retention (days)",
  "database.retention.intraday_ohlcv_days": "Intraday OHLCV Retention (days)",
  "database.retention.audit_log_days": "Audit Log Retention (days)",
  "database.retention.predictions_days": "Predictions Retention (days)",
  "database.retention.news_days": "News Retention (days)",
  "database.retention.economic_events_days": "Economic Events Retention (days)",
  "retraining.shadow_mode_days": "Shadow Mode Duration (days)",
  "retraining.shadow_min_predictions": "Shadow Min Predictions",
  "retraining.retired_model_cleanup_days": "Retired Model Cleanup (days)",
  "retraining.max_training_days": "Max Training History (days)",
  "dashboard.show_degraded_banner": "Show Degraded Banner",
  "news_digest.enabled": "News Digest",
  "news_digest.max_headlines": "Max Headlines",
  "notifications.telegram.enabled": "Telegram Notifications",
  "notifications.telegram.alerts.trade_entry": "Alert: Trade Entry",
  "notifications.telegram.alerts.trade_exit": "Alert: Trade Exit",
  "notifications.telegram.alerts.daily_summary": "Alert: Daily Summary",
  "notifications.telegram.alerts.weekly_summary": "Alert: Weekly Summary",
  "notifications.telegram.alerts.errors": "Alert: Errors",
  "notifications.telegram.alerts.kill_switch": "Alert: Kill Switch",
  // Previously unlabeled (rendered with humanized fallback)
  "retraining.min_argmax_sharpe_for_promotion": "Min Edge (argmax) Sharpe to Promote",
  "risk.depth_gate.min_size_multiplier": "Depth: Min Size Multiplier",
  "risk.reentry.confidence_tolerance": "Re-entry: Confidence Tolerance",
  "risk.reentry.min_reentry_confidence": "Re-entry: Min Confidence Floor",
  "risk.risk_uplift_cap": "Risk Uplift Cap (multiplier ceiling)",
  "risk.tuned_min_signal_rate": "Tuning: Min Signal Rate",
  "risk.tuned_threshold_max_value": "Tuning: Max Threshold Value",
  "strategy.holding_periods.short_swing.max_atr_pct_for_target": "Short-Swing ATR Cap (target geometry)",
  "strategy.holding_periods.week.max_atr_pct_for_target": "Week ATR Cap (target geometry)",
  "strategy.holding_periods.long.max_atr_pct_for_target": "Long ATR Cap (target geometry)",
  "strategy.post_train_min_signal_rate": "Post-Train Min Signal Rate",
  "strategy.signal_generation_concurrency": "Signal Generation Concurrency",
};

// Info descriptions for (i) tooltip
const KEY_DESCRIPTIONS: Record<string, string> = {
  "mode": "Paper mode simulates trades without real money. Live mode executes real orders via Zerodha.",
  "capital.initial_amount": "Starting capital in INR. Position sizes are calculated as a percentage of this.",
  "log.level": "Console output verbosity. DEBUG shows everything, ERROR shows only errors.",
  "log.file_level": "Log file verbosity. Can be more verbose than console for debugging.",
  "llm.enabled": "Enable Google Gemini for sentiment analysis, trade review, and failure analysis.",
  "llm.model": "Gemini model to use. Flash is faster/cheaper, Pro is higher quality.",
  "market_data.daily_provider": "Primary source for daily OHLCV data. Only jugaad-data is currently supported.",
  "market_data.daily_fallback": "Fallback if primary provider fails. Only yfinance is currently supported.",
  "market_data.intraday_provider": "Source for intraday data. Only tvDatafeed is currently supported.",
  "market_data.kite_data_enabled": "Use paid Kite Connect data plan as primary data source.",
  "market_data.kite_websocket_enabled": "Subscribe to KiteTicker WebSocket for sub-second LTP updates. Position-monitor reads from the WS cache before falling back to REST, reducing per-cycle latency and rate-limiter pressure. Requires Kite to be authenticated; falls back silently if not.",
  "market_data.max_signal_data_age_trading_days": "Reject signals whose latest daily bar is older than this many trading days. Catches stale-data flow (provider outages, weekend gaps, dead symbols) before they reach risk-check. Set to 1 for strict freshness; 2 to tolerate a one-day NSE bhav-copy delay.",
  "market_data.news_enabled": "Fetch news from MoneyControl, ET Markets, and LiveMint RSS feeds.",
  "market_data.scrapers_enabled": "Fetch data from Screener.in, Trendlyne, Google Finance, and NSE.",
  "market_data.cache_ttl_minutes": "How long to cache fetched data before re-fetching.",
  "market_data.stale_threshold_minutes": "Reject data older than this. Prevents trading on stale prices.",
  "market_data.sentiment_ttl_hours": "Ignore sentiment data older than this during scanning.",
  "market_data.backfill_days": "Daily-bar history window for backfill-data and ingest-universe.",
  "market_data.intraday_backfill_days": "5-minute-bar history window for backfill-intraday. Intraday bars are much heavier than daily, so this is typically shorter.",
  "heartbeat.market_hours_interval_min": "How often the heartbeat pipeline runs during market hours.",
  "heartbeat.off_hours_interval_min": "How often the heartbeat runs outside market hours.",
  "heartbeat.max_consecutive_skips": "Alert if this many heartbeats are skipped due to overrun.",
  "scanning.universe": "Which stock universe to scan. Nifty 500 covers most liquid stocks.",
  "scanning.shortlist_size": "Number of candidate stocks from daily scan to evaluate for signals.",
  "scanning.min_avg_daily_volume": "Filter out stocks below this average daily volume.",
  "scanning.seed_symbols": "Bootstrap symbols used before the first universe refresh.",
  "scanning.weights.technical": "Weight for technical indicators (RSI, MACD, etc.) in composite score.",
  "scanning.weights.volume_momentum": "Weight for volume and momentum signals in composite score.",
  "scanning.weights.news_sentiment": "Weight for news sentiment in composite score.",
  "scanning.weights.fundamental": "Weight for fundamental data (PE, promoter holding) in composite score.",
  "scanning.weights.volatility": "Weight for ATR% volatility preference in composite score. All weights must sum to 1.0.",
  "scanning.rotation_enabled": "Evict symbols from the watchlist after consecutive heartbeats with no actionable signal so fresh candidates get a turn.",
  "scanning.rotation_no_signal_threshold": "How many consecutive heartbeats a symbol can go without producing a signal before being placed on cooldown.",
  "scanning.rotation_cooldown_hours": "How long an evicted symbol stays out of the watchlist before market-scan can re-add it.",
  "strategy.mode": "Controls which holding periods are allowed and how stocks are selected.",
  "strategy.swing_label_mode": "How swing training labels are made. 'relative' (default): per date, rank every stock's forward 10-bar return across the universe — top quantile = BUY, bottom = SELL. Subtracts the market's own drift from the label and targets cross-sectional momentum (the best-documented Indian-equity edge); the backtest exits at the live ATR geometry, not at label barriers. 'barrier': legacy absolute hit-target-before-SL label.",
  "strategy.relative_label_quantile": "Top/bottom quantile for the relative label. 0.20 = top/bottom 20%, giving a ~20/60/20 BUY/HOLD/SELL class mix by construction.",
  "strategy.intraday_label_mode": "How intraday training labels are made. 'triple_barrier' (default): did price hit the ATR target before the SL by session close (1-min resolution). 'relative': per 5-min instant, rank every stock's forward return-to-close across the universe — top quantile = BUY, bottom = SELL (the intraday edition of the swing relative label). Validate with the offline experiment harness before flipping.",
  "strategy.swing_horizon_cap_days": "Cap on the holding days the chooser may assign to ML swing trades. The swing model's label only measures a ~10-bar (2-week) window — horizons far beyond it ride an edge the model never measured. Default 15; long_term/swing modes' 66-day tails clamp to this. Raise or set 0 to disable knowingly.",
  "strategy.min_training_samples": "Minimum data points required to train an ML model.",
  "strategy.ema_periods": "Exponential moving average periods used in technical analysis.",
  "strategy.allowed_holding_periods": "Which holding periods the strategy is allowed to use.",
  "strategy.holding_periods.intraday.target": "ATR multiplier for intraday profit target. target = entry ± N × ATR.",
  "strategy.holding_periods.intraday.max_atr_pct_for_target": "Cap on the daily ATR (as fraction of entry price) used when computing the intraday target / SL distance. High-ATR stocks would otherwise get unreachable targets (e.g. an 11% ATR stock at the default 0.6× multiplier asks for a 6-7% intraday move). Capping at 0.035 (default) limits the implied target distance to ~2.1% while leaving median large-caps (1-2% ATR) untouched. Set to 0 to disable the cap.",
  "strategy.holding_periods.intraday.stop_loss": "ATR multiplier for intraday stop loss. SL = entry ∓ N × ATR.",
  "strategy.holding_periods.short_swing.target": "ATR multiplier for 2–5 day swing profit target.",
  "strategy.holding_periods.short_swing.stop_loss": "ATR multiplier for 2–5 day swing stop loss.",
  "strategy.holding_periods.week.target": "ATR multiplier for ~1 week hold profit target.",
  "strategy.holding_periods.week.stop_loss": "ATR multiplier for ~1 week hold stop loss.",
  "strategy.holding_periods.long.target": "ATR multiplier for 2+ week hold profit target. Wider for trending stocks.",
  "strategy.holding_periods.long.stop_loss": "ATR multiplier for 2+ week hold stop loss.",
  "strategy.volatility.min_atr_pct": "Minimum ATR% — below this the stock doesn't move enough to trade.",
  "strategy.volatility.max_atr_pct": "Maximum ATR% — above this the stock is too volatile/risky.",
  "strategy.volatility.ideal_min_atr_pct": "Ideal range lower bound. Stocks in the ideal range score highest.",
  "strategy.volatility.ideal_max_atr_pct": "Ideal range upper bound. ATR% = ATR / price (e.g. 0.02 = 2%).",
  "strategy.volatility.max_atr_pct_for_intraday_eligibility": "Hard eligibility cap for the intraday bucket. Stocks with daily ATR% above this are refused as intraday material (routed to swing in balanced mode, dropped in pure intraday mode). Different lever from the per-bucket ATR cap above — that one clamps the geometry on a stock that still trades intraday; this one refuses intraday entirely for stocks too volatile to square off in a half-day session. Default 0.05 (5%). Set to 0 to disable.",
  "strategy.indicators.rsi": "Relative Strength Index — momentum oscillator (overbought/oversold).",
  "strategy.indicators.macd": "Moving Average Convergence Divergence — trend and momentum.",
  "strategy.indicators.bollinger_bands": "Bollinger Bands — volatility bands around moving average.",
  "strategy.indicators.vwap": "Volume Weighted Average Price — intraday fair value benchmark.",
  "strategy.indicators.atr": "Average True Range — volatility measure for position sizing and SL.",
  "strategy.indicators.volume_profile": "Volume Profile — price levels with highest trading activity.",
  "strategy.indicators.obv": "On Balance Volume — cumulative volume flow indicator.",
  "strategy.indicators.supertrend": "SuperTrend — trend-following indicator based on ATR.",
  // Market Regime
  "strategy.market_regime.enabled": "Auto-detect bull/bear/range and adjust scanning weights and holding periods.",
  "strategy.market_regime.index_symbol": "Benchmark index for regime detection (e.g. NIFTY 50).",
  "strategy.market_regime.lookback_days": "Days of index data to analyze for regime detection.",
  "strategy.market_regime.bull_bias_intraday_pct": "In bull regime, bias this fraction of signals toward shorter holds.",
  "strategy.market_regime.bear_max_holding_days": "In bear regime, cap holding days at this value.",
  "strategy.market_regime.range_prefer_mean_reversion": "In range regime, prefer oversold/overbought mean-reversion entries.",
  // Feedback
  "strategy.feature_groups.regime": "Train on universe breadth / average return. Off = leaner price-primary model. Takes effect on next retrain; doesn't affect the current model.",
  "strategy.feature_groups.sector": "Train on sector breadth / relative momentum. Off = price-primary. Next-retrain only.",
  "strategy.feature_groups.institutional": "Train on bulk-deal counts + delivery %. Sparse data — candidate to disable if it adds noise. Next-retrain only.",
  "strategy.feature_groups.news": "Train on news-sentiment features. Sparse for many symbols. Next-retrain only.",
  "strategy.feature_groups.vix": "Train on India VIX features. Next-retrain only.",
  "strategy.feature_groups.fno": "Train on F&O option-chain features. Forward-only data (very little history yet) — off by default until months accumulate. Next-retrain only.",
  "strategy.feature_groups.feedback": "Train on the fb_* prediction/trade feedback loop. Next-retrain only.",
  "strategy.feedback.enabled": "Enable ML feedback loop — model learns from its own performance.",
  "strategy.feedback.lookback_days": "How far back to aggregate feedback data for retraining.",
  "strategy.feedback.sample_weight_boost": "Weight multiplier for symbols where model performed poorly.",
  "strategy.feedback.sources.predictions": "Include scored prediction outcomes in feedback.",
  "strategy.feedback.sources.dry_runs": "Include scored dry run results in feedback.",
  "strategy.feedback.sources.trades": "Include closed trade PnL and slippage in feedback.",
  "strategy.class_balance_enabled": "Apply inverse-frequency class weights at training time so rare classes (typically BUY under 2:1 R/R path-aware labelling) aren't buried by the HOLD majority. Disable to recover the unweighted classifier.",
  "strategy.class_balance_min_pct": "Refuse to save a freshly-trained model when any of {BUY, HOLD, SELL} accounts for less than this percentage of training labels. Catches the 'BUY is functionally extinct in this data' failure mode at train time instead of letting a sterile model reach production. Units: percentage (0–33). Default 3.0 = each class must be at least 3% of labels. Set to 0 to disable.",
  "strategy.post_train_class_check_enabled": "After saving a fresh model, run inference on recent in-training samples and verify each of {BUY, HOLD, SELL} wins argmax at least once. Belt-and-braces for cases where label balance is fine but the model still never predicts a class (calibration collapse, feature dominance). Cheap — one matmul on a few hundred samples.",
  "risk.max_risk_per_trade_pct": "Maximum capital risked per trade (e.g. 0.02 = 2%).",
  "risk.max_portfolio_exposure_pct": "Maximum total portfolio exposure. Remainder stays as cash.",
  "risk.max_open_positions": "Maximum simultaneous open positions.",
  "risk.max_single_stock_pct": "Maximum capital allocated to any single stock (safety cap).",
  "risk.max_pct_per_signal": "Pacing cap — fraction of capital any one signal can claim. Smaller than max_single_stock_pct so several signals fit under max_portfolio_exposure_pct without saturating it on heartbeat 1.",
  "risk.daily_loss_limit_pct": "Stop trading for the day if portfolio drops this much.",
  "risk.weekly_loss_limit_pct": "Weekly circuit breaker — reduces sizing when hit.",
  "risk.weekly_loss_sizing_reduction": "Reduce position sizes by this factor when weekly breaker triggers.",
  "risk.mandatory_stop_loss": "Every trade must have a stop loss. Cannot be disabled in production.",
  "risk.trailing_sl_enabled": "Automatically trail stop loss upward as price moves in your favor.",
  "risk.trailing_sl_trigger_multiple": "Legacy: activate trailing SL when profit reaches this multiple of risk_per_share. Hard to reason about because the threshold depends on each signal's R:R ratio (1.5 fires at 75% of target for a 2:1 setup but at 150% — never — for a 1:1). Kept for deployments that explicitly tuned it; the per-bucket target-% knobs below take precedence when set.",
  "risk.trailing_sl_trigger_target_pct_intraday": "Start trailing the stop loss when an intraday position has covered this fraction of the entry-to-target distance (0–1). 0.35 (default) = SL starts ratcheting once price has moved 35% of the way to target. Intraday default is more eager than swing because the session is short and you can't afford to wait until 50% of target to start locking gains. Leave blank to fall back to the legacy × risk knob above.",
  "risk.trailing_sl_trigger_target_pct_swing": "Start trailing the stop loss when a swing position (short_term / week / long) has covered this fraction of the entry-to-target distance (0–1). 0.50 (default) = halfway to target. Swing horizons are 2-66 days so the trigger can sit higher than intraday without missing the move. Leave blank to fall back to the legacy × risk knob above.",
  "risk.trailing_sl_step_pct": "Trail the stop loss in steps of this percentage.",
  "risk.target_early_exit_pct": "Exit when price is within this percentage of target. Heartbeats run every 15 min; without a buffer a price that gets within a paisa of target but never touches it waits a full cycle and may reverse. Default 0.15% catches ~₹0.15 on a ₹100 stock.",
  "risk.min_confidence_buy": "Global fallback: minimum ML confidence for a BUY signal (0–1). Used when the per-mode floor below is unset.",
  "risk.min_confidence_sell": "Global fallback: minimum ML confidence for a SELL signal (0–1). Used when the per-mode floor below is unset. Set higher than BUY to avoid exit noise.",
  "risk.min_confidence_buy_intraday": "Intraday BUY floor (0–1). Applied on top of the model's tuned threshold for intraday signals. Leave blank to fall back to the global Min Confidence (BUY).",
  "risk.min_confidence_sell_intraday": "Intraday SELL floor (0–1). Applied on top of the model's tuned threshold for intraday signals. Indian retail intraday is BUY-biased (no overnight short, positive index drift) — a higher SELL floor here filters borderline shorts. Leave blank to fall back to the global Min Confidence (SELL).",
  "risk.min_confidence_buy_swing": "Swing BUY floor (0–1). Applied on top of the model's tuned threshold for short_swing / week / long holding signals. Leave blank to fall back to the global Min Confidence (BUY).",
  "risk.min_confidence_sell_swing": "Swing SELL floor (0–1). Applied on top of the model's tuned threshold for short_swing / week / long holding signals. Leave blank to fall back to the global Min Confidence (SELL).",
  "risk.skip_sell_on_holdings": "Don't generate SELL signals for symbols you already hold — position-monitor handles exits.",
  "risk.max_trades_per_day": "Maximum combined trades per day across MIS and CNC, including re-entries. Acts as an overall cap on top of the per-product limits below.",
  "risk.max_mis_trades_per_day": "Optional per-product cap on intraday (MIS) entries per day. When blank, only the combined Max Trades / Day applies. Useful when you want a different MIS budget than CNC — e.g. 10 MIS entries for an active intraday workflow.",
  "risk.max_cnc_trades_per_day": "Optional per-product cap on delivery (CNC) entries per day. When blank, only the combined Max Trades / Day applies. Useful for users who hold inventory deliberately and want a tighter CNC budget — e.g. 1 CNC entry per day.",
  "risk.kill_switch_enabled": "Allow /stop and /kill commands to halt all trading.",
  "risk.drift_auto_suspend_enabled": "When drift-watch detects a >15pp win-rate decay or a signal-class collapse at 16:30 IST, automatically suspend signal generation until the next successful model-retrain. Off by default — opt in for unattended live trading. The suspension flag can also be cleared manually via the dashboard.",
  "risk.earnings_blackout_days": "Block new entries in symbols with a scheduled earnings or board-meeting announcement within this many days. Earnings reactions routinely move stocks ±5-20% overnight, wider than any ATR-based SL. Sources from the NSE Corporate Filings calendar (populated by ingest-data). 0 disables the gate; 1-2 is typical.",
  "risk.max_portfolio_beta": "Cap the portfolio's beta-weighted notional exposure as a multiple of capital. Sum of (position notional × |beta|) over all open positions + the candidate signal must stay under this × capital. Beta is computed against a cross-sectional market-return proxy over the last 60 days. 0 disables the gate. 1.5 is the standard 'diversified' ceiling; 2.0 lets you concentrate more.",
  "risk.llm_review_enabled": "Gemini reviews each trade before execution (APPROVE/REJECT/RESIZE).",
  "risk.llm_fallback_to_rules": "Use rules-only risk check if LLM is unavailable.",
  "risk.max_same_sector_positions": "Maximum open positions in the same sector (correlation limit).",
  "risk.margin_usage_enabled": "When disabled, position value is capped by available cash (no leverage).",
  "risk.weekly_reset_day": "Day when weekly circuit breaker PnL counter resets.",
  "risk.loss_cooldown_minutes": "Wait this long after a losing trade before entering the next one.",
  "risk.symbol_cooldown_days": "Hard block on re-trading a symbol for this many days after last trade.",
  "risk.symbol_repeat_lookback_days": "Window during which repeat symbols need elevated confidence.",
  "risk.symbol_repeat_min_confidence": "Confidence required to re-trade a symbol within the lookback window — set higher than the per-direction BUY/SELL thresholds.",
  "risk.tuned_threshold_max_diff": "Cap on how far apart the model's PnL-tuned BUY and SELL probability thresholds may be at inference. The walk-forward sweep can land on highly asymmetric pairs (e.g. BUY=0.80 SELL=0.70) when one class happens to pay better on the holdout, and then the production model never fires the other class. Pulls both thresholds toward their midpoint until the gap is at most this value. Default 0.05 (5 pp). Set to 1.0 to disable; 0.0 to force exactly symmetric tuned values.",
  "risk.buy_threshold_override": "Hard override of the model's tuned BUY probability threshold. When set, REPLACES the saved tuned value entirely (the asymmetry cap above no longer applies). Use when the model's saved threshold is unreachable in production — e.g. tuner saved 0.80 but the calibrator never outputs P(BUY) > 0.50. Still ANDed with the per-mode Min Confidence floor. Leave blank to use the model's saved tuned threshold.",
  "risk.sell_threshold_override": "Hard override of the model's tuned SELL probability threshold. Same semantics as the BUY override. Leave blank to use the model's saved tuned threshold.",
  "risk.min_net_rr": "Minimum cost-adjusted reward:risk ratio required to take a signal. Computes (target − entry) × qty − round-trip-costs as net win and (entry − sl) × qty + costs as net loss (sign-flipped for SELL), then rejects when net_win / net_loss < this threshold. Catches signals where the gross 2:1 R:R collapses to 1.3:1 after brokerage + STT + GST, leaving no margin for slippage. Default 1.5. Set to 0 to disable.",
  "risk.max_risk_rejected_retries_per_day": "Cap how many times a symbol with retryable dispositions (risk_rejected, expired, trade_execute_failed, skill_error) can regenerate per day. Prevents log spam from chronically-failing setups; default 5.",
  // Regime gate
  "risk.regime_gate.enabled": "Refuse BUYs on broadly-red days and SELLs on broadly-green days. Computed once per heartbeat from today's cross-sectional breadth. Default off — calibrate against your universe first.",
  "risk.regime_gate.min_breadth_for_buy": "Reject BUYs when universe breadth (fraction of symbols up) falls below this. 0.40 = at least 40% of universe must be up.",
  "risk.regime_gate.max_breadth_for_sell": "Reject SELLs when universe breadth exceeds this. 0.60 = at least 60% of universe up = bad day to be short.",
  "risk.regime_gate.bullish_breadth_threshold": "Above this breadth, multiply BUY position size by the bullish multiplier.",
  "risk.regime_gate.bearish_breadth_threshold": "Below this breadth, multiply SELL position size by the bearish multiplier.",
  "risk.regime_gate.bullish_size_multiplier": "Position size multiplier for BUYs on strongly-bullish days (capped by max_single_stock_pct).",
  "risk.regime_gate.bearish_size_multiplier": "Position size multiplier for SELLs on strongly-bearish days (capped by max_single_stock_pct).",
  // Liquidity gate
  "risk.liquidity_gate.enabled": "Refuse orders whose size would consume more than max_pct_of_top5 of the relevant side of the Kite top-5 book. Stops you eating your own slippage on thin names. Requires Kite paid data.",
  "risk.liquidity_gate.max_pct_of_top5": "Maximum fraction of the top-5 depth quantity your order may represent (0.10 = 10%).",
  // Depth gate
  "risk.depth_gate.enabled": "Refuse signals when (total_buy_qty − total_sell_qty) / (sum) strongly opposes the signal direction. Off by default; the book is noisy near market open. Requires Kite paid data.",
  "risk.depth_gate.min_imbalance_for_buy": "Reject BUYs when imbalance falls below this (negative = more sell pressure). −0.30 = book is 65% sell.",
  "risk.depth_gate.max_imbalance_for_sell": "Reject SELLs when imbalance exceeds this (positive = more buy pressure). +0.30 = book is 65% buy.",
  // Institutional flow
  "risk.institutional_flow.enabled": "Sizing multiplier based on (a) recent bulk/block deals on the symbol and (b) today's FII net flow. Aligning direction scales up; opposing scales down. Reads bulk_deals + fii_dii_daily tables populated by ingest-data.",
  "risk.institutional_flow.bulk_deal_lookback_days": "How many days back to count BUY vs SELL bulk deals on the candidate symbol.",
  "risk.institutional_flow.bulk_deal_size_multiplier": "Position size multiplier when bulk deals (in lookback) align with signal direction. Opposing direction divides by this.",
  "risk.institutional_flow.fii_net_threshold_cr": "FII net flow (₹ crore) above which the day counts as 'buying'; below the negative of this, 'selling'.",
  "risk.institutional_flow.fii_aligned_size_multiplier": "Position size multiplier when FII direction agrees with signal direction.",
  // Market trend filter (long-only circuit breaker)
  "risk.market_trend_filter.enabled": "Long-only circuit breaker: refuse NEW BUY entries when the equal-weight universe index sits below its moving average (a downtrend). SELLs and closing existing positions are never blocked. The standard drawdown protection for a long-biased swing book — enable before running auto unattended. Default off.",
  "risk.market_trend_filter.ma_window": "Lookback (trading days) for the index moving average the trend is measured against. 50 ≈ 10 trading weeks. Higher = slower, fewer regime flips.",
  // Exit tweaks
  "risk.exit_tweaks.time_stop_enabled": "Intraday positions still open after intraday_stop_after_min with target-progress below threshold get market-exited. Catches the chop trade that neither works nor breaks. Applies to client-side-managed positions only.",
  "risk.exit_tweaks.intraday_stop_after_min": "Minutes a stuck intraday position can stay open before time-stop considers it.",
  "risk.exit_tweaks.intraday_stop_progress_threshold": "Target-progress fraction below which the time-stop fires. 0.30 = exit if we've covered less than 30% of entry-to-target distance.",
  "risk.exit_tweaks.volume_exit_enabled": "Exit when the last 5-min bar volume drops below volume_exit_min_ratio × average of previous N bars AND the position is in 0.5R–2R profit. Trend-is-dying signal.",
  "risk.exit_tweaks.volume_exit_lookback_bars": "Number of prior 5-minute bars to average for the volume comparison.",
  "risk.exit_tweaks.volume_exit_min_ratio": "Latest 5-min volume vs lookback average — below this triggers the exit. 0.30 = below 30% of recent average.",
  "risk.exit_tweaks.tighten_trailing_enabled": "Once profit covers tighten_start_at_target_pct of the entry-to-target distance, shrink the trailing-SL step in a step-up curve. Applies to client-side, GTT, and MIS-OCO trailing paths.",
  "risk.exit_tweaks.tighten_start_at_target_pct": "First tightening fires at this fraction of target progress (0.50 = halfway to target).",
  "risk.exit_tweaks.tighten_step_size": "Every additional target-progress bucket of this size applies another tightening step (0.10 = each 10% of progress).",
  "risk.exit_tweaks.tighten_step_decay": "Trailing-SL step shrinks by this fraction per bucket (0.15 = 15% smaller step per 10% of progress).",
  "risk.exit_tweaks.tighten_min_multiplier": "Floor on the trailing-SL step multiplier — never shrinks below this fraction of the original step.",
  // Execution
  "execution.pending_expiry_minutes": "Pending trades auto-expire after this many minutes; heartbeat sweeps them so abandoned approvals don't lock max_open_positions / max_trades_per_day / exposure budgets.",
  // Partial Profit
  "risk.partial_profit.enabled": "Close part of the position when an intermediate profit target is hit.",
  "risk.partial_profit.first_target_pct": "Book profits at this % of the way to target (0.5 = halfway).",
  "risk.partial_profit.first_close_pct": "Fraction of position to close (0.5 = close half).",
  "risk.partial_profit.move_sl_to_breakeven": "After partial booking, move SL to entry price to protect remaining.",
  // Conviction Sizing
  "risk.conviction_sizing.enabled": "Scale position size based on ML confidence. Higher confidence → larger position.",
  "risk.conviction_sizing.min_multiplier": "Size multiplier at the confidence floor (e.g. 0.6 = 60% normal size).",
  "risk.conviction_sizing.max_multiplier": "Size multiplier at the confidence ceiling (e.g. 1.5 = 150% normal size).",
  "risk.conviction_sizing.confidence_floor": "Confidence score that maps to min_multiplier.",
  "risk.conviction_sizing.confidence_ceiling": "Confidence score that maps to max_multiplier.",
  // Correlation Limits
  "risk.correlation_limit.enabled": "Limit positions in highly correlated stocks (beyond sector check).",
  "risk.correlation_limit.max_correlated_positions": "Max open positions that are highly correlated with a new signal.",
  "risk.correlation_limit.correlation_threshold": "Pearson correlation above this = 'highly correlated' (0.7 typical).",
  "risk.correlation_limit.lookback_days": "Days of price history used to compute correlations.",
  // Re-entry
  "risk.reentry.enabled": "Allow re-entering a stock after SL hit if conditions improve.",
  "risk.reentry.min_bars_after_exit": "Minimum daily bars to wait after exit before re-entry.",
  "risk.reentry.min_price_move_pct": "Price must move this much from exit price before re-entry.",
  "risk.reentry.max_reentries_per_symbol": "Max re-entries for the same symbol in one day.",
  "risk.reentry.require_higher_confidence": "New signal must have higher ML confidence than the original trade.",
  // Holding Expiry
  "risk.holding_expiry.enabled": "Enable time-based position management for expired holdings.",
  "risk.holding_expiry.action": "What to do when a position exceeds its expected holding period.",
  "risk.holding_expiry.breakeven_buffer_pct": "In-profit positions get SL tightened to entry + this buffer.",
  "risk.holding_expiry.loss_threshold_pct": "Below this PnL% the position is 'at a loss' — close immediately on expiry.",
  "risk.holding_expiry.max_holding_days": "Absolute maximum holding period in trading days (~3 months = 66).",
  "execution.max_order_retries": "Retry failed orders this many times before giving up.",
  "execution.retry_base_delay_sec": "Exponential backoff base delay between retries (2s, 4s, 8s).",
  "execution.paper_slippage_pct": "Simulated slippage for paper trading (0.001 = 0.1%).",
  "execution.order_timeout_sec": "Cancel unfilled order remainder after this many seconds.",
  "execution.price_drift_max_pct": "Reject signal if current price drifted more than this from entry price.",
  "execution.transaction_mode": "Auto executes immediately. Manual requires approval via Telegram/UI.",
  "execution.rejection_cooldown_hours": "After rejecting a trade, don't re-queue the same symbol+side for this many hours. 0 = no cooldown, 168 = 7 days.",
  "execution.scaled_entry.enabled": "Split orders into multiple legs for better average entry price.",
  "execution.scaled_entry.legs": "Number of entry legs (2 = split into two orders).",
  "execution.scaled_entry.second_leg_offset_pct": "Second leg limit price offset from entry (0.005 = 0.5% lower for BUY).",
  "execution.scaled_entry.second_leg_delay_sec": "Seconds to wait between first and second leg placement.",
  "transaction_costs.brokerage_per_leg_pct": "Brokerage per order leg (0.0003 = 0.03%). Zerodha default.",
  "transaction_costs.brokerage_cap_per_leg": "Maximum brokerage per order in INR. Zerodha caps at ₹20.",
  "transaction_costs.stt_intraday_pct": "Securities Transaction Tax on sell side for intraday (MIS).",
  "transaction_costs.stt_delivery_pct": "Securities Transaction Tax on sell side for delivery (CNC).",
  "transaction_costs.other_charges_pct": "Stamp duty + GST + exchange fees combined.",
  "market_hours.open": "NSE market opening time (IST).",
  "market_hours.close": "NSE market closing time (IST).",
  "market_hours.order_start": "Earliest time for new orders. Skips opening volatility.",
  "market_hours.order_end": "Latest time for new orders.",
  "market_hours.square_off": "Auto square-off time for intraday (MIS) positions.",
  "market_hours.square_off_extension": "Extra window for square-off orders after order_end.",
  "market_hours.intraday_cutoff": "No new intraday (MIS) signals after this time. Swing/CNC signals are unaffected.",
  "market_hours.timezone": "Timezone for all market hour calculations.",
  "market_hours.holidays": "JSON list of NSE holiday dates (YYYY-MM-DD strings). Heartbeat skips these. Also editable via the /holiday Telegram command.",
  "market_hours.early_close_days": "JSON list of half-day sessions: [{\"date\": \"YYYY-MM-DD\", \"close\": \"HH:MM\"}]. Used for Diwali muhurat and shortened sessions; market-hours checker honours the truncated close on these days.",
  "database.backup_enabled": "Enable daily automatic database backups.",
  "database.retention.ohlcv_days": "Keep DAILY OHLCV bars for this many days. Must be >= your max training history (retraining.max_training_days / market_data.backfill_days) or the nightly maintenance silently truncates the model's training data.",
  "database.retention.intraday_ohlcv_days": "Keep INTRADAY (5-minute etc.) OHLCV bars for this many days. Decoupled from daily because 5-min bars are ~75x heavier per day and aren't used for model training — only operationally (volume-exhaustion exits, live monitoring). Keep this short (defaults to the 365d intraday backfill window) to avoid bloating the DB.",
  "database.retention.audit_log_days": "Keep audit log entries for this many days.",
  "database.retention.predictions_days": "Keep ML prediction records for this many days.",
  "database.retention.news_days": "Keep news articles for this many days.",
  "database.retention.economic_events_days": "Keep economic calendar events for this many days.",
  "retraining.shadow_mode_days": "Run new model in shadow alongside production for this many days.",
  "retraining.shadow_min_predictions": "Minimum scored predictions before promotion decision.",
  "retraining.retired_model_cleanup_days": "Auto-delete retired model files after this many days.",
  "retraining.max_training_days": "Cap how far back daily bars are loaded for training. Default 730 (2 years). Training memory scales with days × symbols — raise this if the training host has memory to spare and you want the model to see deeper history.",
  "dashboard.show_degraded_banner": "Show warning banner when LLM or services are unavailable.",
  "news_digest.enabled": "Send daily news headlines summary to Telegram.",
  "news_digest.max_headlines": "Number of headlines to include in the daily digest.",
  "notifications.telegram.enabled": "Enable Telegram bot for notifications and commands.",
  "notifications.telegram.alerts.trade_entry": "Send Telegram alert when a trade is entered.",
  "notifications.telegram.alerts.trade_exit": "Send Telegram alert when a trade is exited.",
  "notifications.telegram.alerts.daily_summary": "Send daily trading summary via Telegram.",
  "notifications.telegram.alerts.weekly_summary": "Send weekly performance summary via Telegram.",
  "notifications.telegram.alerts.errors": "Send error notifications via Telegram.",
  "notifications.telegram.alerts.kill_switch": "Send alert when kill switch is triggered.",
  "heartbeat.auth_broker_cron": "When to attempt daily Kite Connect re-authentication.",
  "heartbeat.ingest_premarket_cron": "When to fetch pre-market global cues and overnight data.",
  "scanning.universe_cron": "When to refresh the stock universe list from NSE.",
  "news_digest.schedule_cron": "When to send the daily news digest to Telegram.",
  "reports.daily_report_time": "Time (HH:MM IST) to generate the daily trading report.",
  "reports.weekly_report_cron": "When to generate the weekly performance report.",
  "retraining.schedule_cron": "When to retrain ML models with recent data.",
  "database.backup_cron": "When to run the daily database backup.",
  // Previously undocumented
  "retraining.min_argmax_sharpe_for_promotion": "Edge gate: a freshly-trained shadow model must clear this argmax (untuned) backtest Sharpe to be promoted to production. Argmax is the honest edge of the model's natural decisions, before any threshold tuning — a model whose backtest profit lives entirely in a threshold-selected tail (high tuned Sharpe but negative argmax) is blocked here. 0.0 = require non-negative edge. Negative = disable the gate.",
  "risk.depth_gate.min_size_multiplier": "Floor for the depth-gate size multiplier. The order-book imbalance maps to a position-size multiplier between this floor and 1.0: a neutral/favourable book → full size, the worst-possible opposing book → this fraction (0.4 = 40%). The depth gate sizes down rather than blocking outright.",
  "risk.reentry.confidence_tolerance": "After a stop-out, a re-entry signal's confidence must be at least this fraction of the ORIGINAL entry's confidence (0.85 = within 15% of it). ML confidence naturally decays as a trend matures, so a strict 'must be higher' rejected most valid re-entries; this tolerance plus the absolute floor below replaces it.",
  "risk.reentry.min_reentry_confidence": "Absolute confidence floor a re-entry signal must exceed regardless of the original entry's confidence (0.55). Guards against re-entering on a weak signal just because the original was also weak.",
  "risk.risk_uplift_cap": "Ceiling on the stacked conviction / regime / institutional-flow size multipliers. They compose multiplicatively (e.g. 1.5 × 1.5 × 1.2 = 2.7×); after all of them, risk-check re-clamps so the effective rupees-at-risk never exceeds max_risk_per_trade_pct × this cap (1.5). Stops a strongly-favourable signal from silently running 5%+ risk.",
  "risk.tuned_min_signal_rate": "Minimum fraction of non-HOLD predictions a (BUY, SELL) threshold cell must produce during the tuning sweep to be eligible (0.02 = 2%). Stops the sweep from picking cutoffs so high the model would signal almost never — the 'every prediction collapses to HOLD' failure.",
  "risk.tuned_threshold_max_value": "Inference-time cap on a tuned class threshold (0.60). The live model clamps any tuned threshold above this so it stays reachable by the deployed model's probability scale. Bypassed per-side by buy_threshold_override / sell_threshold_override.",
  "strategy.holding_periods.short_swing.max_atr_pct_for_target": "Cap on the daily ATR (as fraction of entry price) used when computing the short-swing target / SL distance, so high-ATR names don't get unreachable targets. Set to 0 to disable.",
  "strategy.holding_periods.week.max_atr_pct_for_target": "Cap on the daily ATR (as fraction of entry price) used when computing the week-holding target / SL distance, so high-ATR names don't get unreachable targets. Set to 0 to disable.",
  "strategy.holding_periods.long.max_atr_pct_for_target": "Cap on the daily ATR (as fraction of entry price) used when computing the long-holding target / SL distance, so high-ATR names don't get unreachable targets. Set to 0 to disable.",
  "strategy.post_train_min_signal_rate": "Post-train production-path guard: a freshly-trained model must emit at least this fraction of non-HOLD signals when scored on its own training data (0.005 = 0.5%), or the retrain is rejected as a sterile / HOLD-only model. Runs the real production decision path (calibration + tuned thresholds), so it catches a model the live engine would never let signal.",
  "strategy.signal_generation_concurrency": "How many symbols generate-signals evaluates concurrently per chunk (default 10). The watchlist is processed in asyncio.gather chunks of this size — higher = faster heartbeats but more concurrent data/ML load.",
  "strategy.max_atr_pct_hard_reject": "Hard sanity ceiling on ATR% (= ATR ÷ entry price). A daily ATR above this fraction of price is implausible for an NSE equity (real ATRs run ~1–8%) and almost always means corrupt OHLCV (e.g. a wrong-symbol bar) — so the signal is rejected outright instead of being sized off a garbage ATR (which otherwise produces nonsense like a +189% target / −94% SL). Default 0.20 (20%). Set 0 to disable.",
  // Auto-scoring
  "scoring.auto_score_enabled": "Run the daily auto-score CRON. When on, a post-close job scores every dry-run with unscored signals and every elapsed prediction against the actuals on its OWN target date (path-aware over the holding window) — no manual 'Score' clicks needed. Partial by design: signals whose horizon hasn't fully elapsed are left pending for a later run.",
  "scoring.auto_score_cron": "When the auto-score job runs (cron, IST). Default 16:45 on weekdays — after the day's daily bars are ingested (~15:30–16:00) so target dates that closed today can be scored. Only matters when Auto-Score is enabled.",
  // Depth snapshots (order-book archive)
  "market_data.depth_snapshots_enabled": "Archive the Kite top-5 order-book (bid/ask quantities) once per heartbeat via the depth-snapshot skill. Pure data collection for a future intraday order-flow feature set — nothing trades on it. Requires Kite paid data.",
  "market_data.depth_snapshot_retention_days": "How many days of order-book depth snapshots to keep before the nightly maintenance prunes them. Depth rows accumulate fast (one per open symbol per cycle), so keep this short.",
  // Training — extended features / labels
  "strategy.indicators.extended_momentum": "Train on the extended multi-horizon momentum feature set (1/3/6/9-month returns, risk-adjusted momentum quality, vol-regime ratio, fractional-differenced log-price). Daily/swing only — meaningless on 5-min intraday bars. 3–9 month momentum is the strongest documented Indian-equity anomaly. Takes effect on next retrain.",
  "strategy.label_cost_floor_enabled": "Floor the triple-barrier target at the round-trip transaction cost + slippage when labelling training data, so a labelled 'win' always clears costs. Bites hardest on the tight 0.6×ATR intraday geometry. The same effective target flows into the walk-forward backtest so labels and backtest agree on what's profitable. Next-retrain only.",
  "strategy.time_decay_last_weight": "Linear time-decay applied to training sample weights, oldest→newest. 1.0 = off (every bar weighted equally). Lower values (e.g. 0.5) tilt training toward recent regimes by down-weighting the oldest bars to this fraction. Next-retrain only.",
  // Retraining — cross-validation + XGBoost hyperparameters
  "retraining.cv_embargo_frac": "Embargo gap (as a fraction of the calendar span) inserted on top of the label-overlap purge between train and validation/holdout folds. Absorbs serial-correlation / delayed-reaction leakage beyond label overlap. Default ~0.01 (1% of the span).",
  "retraining.xgb.max_depth": "XGBoost maximum tree depth. Deeper trees fit more complex interactions but overfit noisy financial features faster. Lower = more regularized. Next-retrain only.",
  "retraining.xgb.learning_rate": "XGBoost learning rate (eta). Lower learns more slowly and generalizes better but needs more trees. Paired with early stopping, which picks the actual tree count. Next-retrain only.",
  "retraining.xgb.n_estimators": "Upper bound on the number of boosting rounds (trees). Early stopping on a purged + embargoed validation tail picks the real count; the deployed model then refits on ALL data at that count. Next-retrain only.",
  "retraining.xgb.min_child_weight": "Minimum sum of instance weight (hessian) needed in a leaf. Higher = more conservative splits = stronger regularization on noisy data. Next-retrain only.",
  "retraining.xgb.subsample": "Fraction of training rows sampled per boosting round. < 1.0 adds randomness that reduces variance/overfitting. A core variance-reduction knob for noisy features. Next-retrain only.",
  "retraining.xgb.colsample_bytree": "Fraction of features sampled per tree. < 1.0 de-correlates trees and curbs overfitting. A core variance-reduction knob. Next-retrain only.",
  "retraining.xgb.gamma": "Minimum loss reduction required to make a further split (complexity penalty). Higher = fewer, more conservative splits. Next-retrain only.",
  "retraining.xgb.reg_lambda": "L2 regularization on leaf weights. Higher shrinks weights toward zero, reducing overfitting. Next-retrain only.",
  "retraining.xgb.reg_alpha": "L1 regularization on leaf weights. Higher drives some weights to exactly zero (feature sparsity). Next-retrain only.",
  "retraining.xgb.early_stopping_rounds": "Stop boosting if the purged validation metric hasn't improved for this many consecutive rounds; the best round becomes the deployed tree count. Next-retrain only.",
  "retraining.xgb.early_stopping_min_samples": "Minimum training samples required before early stopping is used. Below this the probe is skipped and the full n_estimators is used (too little data to carve out a reliable validation tail). Next-retrain only.",
};

// Cron key labels (friendly names for the virtual cron section)
const CRON_LABELS: Record<string, string> = {
  "heartbeat.auth_broker_cron": "Broker Auth",
  "heartbeat.ingest_premarket_cron": "Pre-market Data",
  "scanning.universe_cron": "Universe Refresh",
  "news_digest.schedule_cron": "News Digest",
  "reports.daily_report_time": "Daily Report (time)",
  "reports.weekly_report_cron": "Weekly Report",
  "retraining.schedule_cron": "Model Retraining",
  "database.backup_cron": "Database Backup",
  "scoring.auto_score_cron": "Auto-Score Dry-Runs & Predictions",
};

function getKeyLabel(fullKey: string): string {
  // Full-key match first (handles duplicates like holding_periods.*.target)
  if (FULL_KEY_LABELS[fullKey]) return FULL_KEY_LABELS[fullKey];
  // Cron labels
  if (CRON_LABELS[fullKey]) return CRON_LABELS[fullKey];
  // Fallback: humanize last segment
  const last = fullKey.split(".").pop()!;
  return last.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function getKeyDescription(fullKey: string): string | undefined {
  return KEY_DESCRIPTIONS[fullKey];
}

function formatHint(fullKey: string): string | null {
  if (fullKey.includes("_pct")) return "0–1 (e.g. 0.02 = 2%)";
  if (fullKey.includes("_cron") || CRON_KEYS.includes(fullKey)) return "cron expression";
  return null;
}

// ---------------------------------------------------------------------------
// Field components
// ---------------------------------------------------------------------------

function InfoIcon({
  description, fullKey,
}: { description?: string; fullKey?: string }) {
  // Two independent open states. Hover reveals on desktop without
  // requiring a click; click pins so the tooltip stays open and works
  // on mobile (where hover doesn't exist). Either state being true
  // shows the tooltip.
  const [hovering, setHovering] = useState(false);
  const [pinned, setPinned] = useState(false);
  const visible = hovering || pinned;
  const hasDescription = !!description;
  const defaults = useContext(DefaultsContext);
  // Always render the icon — every setting should have one so the user
  // can at least see the canonical dotted key (useful for /run, docs,
  // /symbol contexts) even when we haven't written a description yet.
  const baseTooltip = hasDescription
    ? description!
    : `Config key: ${fullKey}\nDescription not yet written — file an issue if unclear.`;
  let defaultLine = "";
  if (fullKey && fullKey in defaults) {
    const raw = defaults[fullKey];
    if (raw !== null && raw !== undefined) {
      defaultLine = `Default: ${formatDefaultValue(raw)}`;
    } else if (fullKey in FALLBACK_DEFAULTS) {
      // Optional field whose default is None — surface the
      // documented fallback so "not set" isn't a dead end.
      const fallback = FALLBACK_DEFAULTS[fullKey];
      if (typeof fallback === "string" && fallback.startsWith("ref:")) {
        const refKey = fallback.slice(4);
        const refValue = defaults[refKey];
        defaultLine = refValue !== undefined && refValue !== null
          ? `Default: not set — falls back to ${refKey} (${formatDefaultValue(refValue)})`
          : `Default: not set — falls back to ${refKey}`;
      } else {
        defaultLine = `Default: ${fallback}`;
      }
    } else {
      defaultLine = `Default: not set`;
    }
  }
  const tooltip = defaultLine ? `${baseTooltip}\n\n${defaultLine}` : baseTooltip;
  return (
    <span
      className="relative inline-flex shrink-0"
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
    >
      <button
        type="button"
        className={clsx(
          "inline-flex items-center justify-center w-4 h-4 rounded-full text-[9px] font-bold cursor-help shrink-0 transition-colors",
          hasDescription
            ? "bg-gray-800 border border-gray-600 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
            : "bg-gray-900 border border-dashed border-gray-700 text-gray-600 hover:text-gray-400 hover:border-gray-500",
        )}
        onClick={(e) => { e.stopPropagation(); setPinned((v) => !v); }}
        onBlur={() => setPinned(false)}
        aria-label={hasDescription ? "Show description" : "Show config key"}
      >
        i
      </button>
      {visible && (
        <span className="absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 z-50 w-56 px-2.5 py-1.5 rounded bg-gray-700 border border-gray-600 text-[11px] text-gray-200 leading-snug shadow-lg whitespace-pre-line pointer-events-none">
          {tooltip}
        </span>
      )}
    </span>
  );
}

function FieldLabel({
  label, description, hint, dimmed, fullKey,
}: {
  label: string;
  description?: string;
  hint?: string | null;
  dimmed?: boolean;
  fullKey?: string;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={clsx("text-sm", dimmed ? "text-gray-500" : "text-gray-300")}>{label}</span>
      <InfoIcon description={description} fullKey={fullKey} />
      {hint && <span className="text-[10px] text-gray-600">({hint})</span>}
    </div>
  );
}

function ToggleField({
  label,
  description,
  fullKey,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  description?: string;
  fullKey?: string;
  checked: boolean;
  onChange: (val: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className={clsx("flex items-center justify-between py-2.5 group", !disabled && "cursor-pointer")}>
      <FieldLabel label={label} description={description} fullKey={fullKey} dimmed={disabled} />
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => !disabled && onChange(!checked)}
        className={clsx(
          "relative w-9 h-5 rounded-full transition-colors",
          checked ? "bg-blue-600" : "bg-gray-700",
          disabled && "opacity-50 cursor-not-allowed",
        )}
      >
        <span className={clsx("absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform", checked && "translate-x-4")} />
      </button>
    </div>
  );
}

function SelectField({
  label,
  description,
  fullKey,
  value,
  options,
  onChange,
}: {
  label: string;
  description?: string;
  fullKey?: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (val: string) => void;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <FieldLabel label={label} description={description} fullKey={fullKey} />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 focus:border-blue-500 focus:outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

function ReadOnlyField({
  label,
  description,
  fullKey,
  value,
}: {
  label: string;
  description?: string;
  fullKey?: string;
  value: string;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <FieldLabel label={label} description={description} fullKey={fullKey} dimmed />
      <span className="text-sm text-gray-500 bg-gray-800/50 border border-gray-800 rounded px-2.5 py-1.5">{value}</span>
    </div>
  );
}

function NumberField({
  label,
  description,
  fullKey,
  value,
  hint,
  onChange,
}: {
  label: string;
  description?: string;
  fullKey?: string;
  value: number;
  hint?: string | null;
  onChange: (val: number) => void;
}) {
  // Field type is captured once from the original server data so
  // edits can't corrupt the int/float classification. Falls back to
  // a heuristic on the current value when registry has no entry.
  const fieldTypes = useContext(FieldTypesContext);
  const kind: FieldKind =
    (fullKey && fieldTypes[fullKey]) ||
    (Number.isInteger(value) ? "int" : "float");
  const isInt = kind === "int";

  // Track the raw input string so the user can transiently clear the
  // box while typing without leaking NaN/null. Only commit when the
  // parsed value is finite AND matches the field's int/float kind.
  const [draft, setDraft] = useState<string>(String(value));
  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <FieldLabel label={label} description={description} fullKey={fullKey} hint={hint} />
      <input
        type="number"
        step={isInt ? 1 : value % 1 !== 0 ? 0.01 : 1}
        value={draft}
        onChange={(e) => {
          const v = e.target.value;
          setDraft(v);
          if (v === "") return;
          if (isInt) {
            // Decimals would parse via Math.floor / parseInt and
            // silently truncate the user's input — better to refuse
            // them outright so the backend doesn't get a value the
            // user didn't actually type. The committed value stays
            // until the user enters a valid integer.
            if (v.includes(".") || v.includes("e") || v.includes("E")) return;
            const parsed = parseInt(v, 10);
            if (Number.isFinite(parsed) && String(parsed) === v.replace(/^\+/, "")) {
              onChange(parsed);
            }
          } else {
            const parsed = v.includes(".") ? parseFloat(v) : parseInt(v, 10);
            if (Number.isFinite(parsed)) onChange(parsed);
          }
        }}
        onBlur={() => {
          // Snap visible draft back to committed value when invalid
          // so the input doesn't sit in a "10.5" state while the
          // committed integer is 10.
          if (draft === "" || !Number.isFinite(Number(draft))
              || (isInt && draft.includes("."))) {
            setDraft(String(value));
          }
        }}
        className="w-28 bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 text-right focus:border-blue-500 focus:outline-none"
      />
    </div>
  );
}

function TextField({
  label,
  description,
  fullKey,
  value,
  hint,
  onChange,
}: {
  label: string;
  description?: string;
  fullKey?: string;
  value: string;
  hint?: string | null;
  onChange: (val: string) => void;
}) {
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <FieldLabel label={label} description={description} fullKey={fullKey} hint={hint} />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-44 bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 text-right focus:border-blue-500 focus:outline-none"
      />
    </div>
  );
}

function NullableNumberField({
  label,
  description,
  fullKey,
  value,
  hint,
  onChange,
}: {
  label: string;
  description?: string;
  fullKey?: string;
  value: number | null;
  hint?: string | null;
  onChange: (val: number | null) => void;
}) {
  // For Optional[int] fields the registry pegs them as "int" even
  // when the current value is null; check it. Falls back to "float"
  // for the historical Optional[float] confidence/threshold keys.
  const fieldTypes = useContext(FieldTypesContext);
  const kind: FieldKind = (fullKey && fieldTypes[fullKey]) || "float";
  const isInt = kind === "int";
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <FieldLabel label={label} description={description} fullKey={fullKey} hint={hint} />
      <input
        type="number"
        step={isInt ? 1 : 0.05}
        placeholder="(global)"
        value={value === null ? "" : value}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "") {
            onChange(null);
            return;
          }
          if (isInt) {
            if (v.includes(".") || v.includes("e") || v.includes("E")) return;
            const parsed = parseInt(v, 10);
            if (Number.isFinite(parsed) && String(parsed) === v.replace(/^\+/, "")) {
              onChange(parsed);
            }
          } else {
            const parsed = v.includes(".") ? parseFloat(v) : parseInt(v, 10);
            if (Number.isFinite(parsed)) onChange(parsed);
          }
        }}
        className="w-28 bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 text-right focus:border-blue-500 focus:outline-none"
      />
    </div>
  );
}

function JsonField({
  label,
  description,
  fullKey,
  value,
  onChange,
}: {
  label: string;
  description?: string;
  fullKey?: string;
  value: unknown;
  onChange: (val: unknown) => void;
}) {
  return (
    <div className="py-2.5 space-y-1">
      <FieldLabel label={label} description={description} fullKey={fullKey} />
      <textarea
        value={JSON.stringify(value, null, 2)}
        onChange={(e) => { try { onChange(JSON.parse(e.target.value)); } catch { /* typing */ } }}
        rows={3}
        className="w-full bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 font-mono focus:border-blue-500 focus:outline-none"
      />
    </div>
  );
}

function ConfigField({
  fullKey,
  value,
  onChange,
}: {
  fullKey: string;
  value: unknown;
  onChange: (key: string, val: unknown) => void;
}) {
  const label = getKeyLabel(fullKey);
  const hint = formatHint(fullKey);
  const description = getKeyDescription(fullKey);

  if (READ_ONLY_KEYS.has(fullKey)) {
    return <ReadOnlyField label={label} description={description} fullKey={fullKey} value={String(value)} />;
  }
  if (SELECT_OPTIONS[fullKey] && typeof value === "string") {
    return <SelectField label={label} description={description} fullKey={fullKey} value={value} options={SELECT_OPTIONS[fullKey]} onChange={(v) => onChange(fullKey, v)} />;
  }
  if (NULLABLE_NUMBER_KEYS.has(fullKey)) {
    const numOrNull = value === null || value === undefined
      ? null
      : typeof value === "number" ? value : null;
    return <NullableNumberField label={label} description={description} fullKey={fullKey} value={numOrNull} hint={hint} onChange={(v) => onChange(fullKey, v)} />;
  }
  if (typeof value === "boolean") {
    return <ToggleField label={label} description={description} fullKey={fullKey} checked={value} onChange={(v) => onChange(fullKey, v)} />;
  }
  if (typeof value === "number") {
    return <NumberField label={label} description={description} fullKey={fullKey} value={value} hint={hint} onChange={(v) => onChange(fullKey, v)} />;
  }
  if (typeof value === "string") {
    return <TextField label={label} description={description} fullKey={fullKey} value={value} hint={hint} onChange={(v) => onChange(fullKey, v)} />;
  }
  return <JsonField label={label} description={description} fullKey={fullKey} value={value} onChange={(v) => onChange(fullKey, v)} />;
}

// ---------------------------------------------------------------------------
// Section card
// ---------------------------------------------------------------------------

function SectionCard({
  title,
  entries,
  edited,
  onChange,
}: {
  title: string;
  entries: [string, unknown][];
  edited: Record<string, unknown>;
  onChange: (key: string, val: unknown) => void;
}) {
  const changedCount = entries.filter(([k]) => k in edited).length;
  if (entries.length === 0) return null;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg mb-4 break-inside-avoid">
      <div className="flex items-center justify-between px-5 pt-4 pb-2">
        <h3 className="text-sm font-semibold text-gray-200">{title}</h3>
        {changedCount > 0 && (
          <span className="text-[10px] bg-amber-900/40 text-amber-400 px-1.5 py-0.5 rounded">
            {changedCount} changed
          </span>
        )}
      </div>
      <div className="px-5 pb-4 divide-y divide-gray-800/60">
        {entries.map(([key, value]) => (
          <ConfigField key={key} fullKey={key} value={value} onChange={onChange} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

// Compare two config values structurally. Booleans / numbers / strings
// are sometimes loaded from the DB as their string forms (e.g. "0.02"
// vs 0.02 from a default AppConfig), so the comparison normalises via
// JSON.stringify rather than ===.
function valuesEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a == null && b == null) return true;
  if (a == null || b == null) return false;
  // Tolerate numeric strings vs numbers
  if (typeof a === "number" && typeof b === "string") return String(a) === b;
  if (typeof a === "string" && typeof b === "number") return a === String(b);
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

export default function SettingsPage() {
  const { data, isLoading, error } = useConfig();
  const { data: defaultsData } = useConfigDefaults();
  const updateMutation = useUpdateConfig();
  const importConfigMutation = useImportConfig();
  const configUploadRef = useRef<HTMLInputElement>(null);

  const [activeTab, setActiveTab] = useState("general");
  const [edited, setEdited] = useState<Record<string, unknown>>({});
  const [localConfig, setLocalConfig] = useState<Record<string, Record<string, unknown>>>({});
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  // "Diff from default" mode is per-tab so toggling on the Risk tab
  // doesn't hide unchanged keys on the General tab when the user
  // navigates back.
  const [diffOnlyTabs, setDiffOnlyTabs] = useState<Record<string, boolean>>({});

  // Flatten all config into a single lookup for virtual sections
  const flatConfig: Record<string, unknown> = {};
  for (const section of Object.values(localConfig)) {
    for (const [k, v] of Object.entries(section)) {
      flatConfig[k] = v;
    }
  }

  // Field-type registry built once from server data so subsequent
  // local edits can't corrupt the int/float classification. Falls back
  // to defaults if the live config hasn't carried a value yet (some
  // Optional fields).
  const fieldTypes = useMemo<Record<string, FieldKind>>(() => {
    const out: Record<string, FieldKind> = {};
    // Authoritative kinds from the server's Pydantic annotations — JSON
    // erases int/float (1.0 -> 1), so the value heuristic below
    // misclassifies whole-valued float fields (e.g.
    // time_decay_last_weight = 1.0) and the input then rejects valid
    // decimals like 0.5. Server map wins; heuristic remains a fallback
    // for older backends that don't send field_kinds yet.
    const serverKinds = (defaultsData as { field_kinds?: Record<string, string> } | undefined)
      ?.field_kinds;
    if (serverKinds) {
      for (const [k, v] of Object.entries(serverKinds)) {
        if (v === "int" || v === "float") out[k] = v;
      }
    }
    const collect = (sections: Record<string, Record<string, unknown>> | undefined) => {
      if (!sections) return;
      for (const sec of Object.values(sections)) {
        for (const [k, v] of Object.entries(sec)) {
          if (k in out) continue;
          const t = inferFieldType(v);
          if (t) out[k] = t;
        }
      }
    };
    collect(data?.sections as Record<string, Record<string, unknown>> | undefined);
    collect(defaultsData?.sections as Record<string, Record<string, unknown>> | undefined);
    // Explicit overrides for fields the heuristic can't classify
    // (Optional[int] with default None has no carrying value).
    for (const k of EXPLICIT_INT_KEYS) {
      if (!(k in out)) out[k] = "int";
    }
    return out;
  }, [data, defaultsData]);

  // Flat lookup of defaults (same key format as flatConfig)
  const flatDefaults: Record<string, unknown> = useMemo(() => {
    const out: Record<string, unknown> = {};
    if (defaultsData?.sections) {
      for (const section of Object.values(defaultsData.sections)) {
        for (const [k, v] of Object.entries(section as Record<string, unknown>)) {
          out[k] = v;
        }
      }
    }
    return out;
  }, [defaultsData]);

  const isDifferentFromDefault = useCallback(
    (key: string): boolean => {
      if (!(key in flatDefaults)) return false; // unknown key — treat as "same"
      return !valuesEqual(flatConfig[key], flatDefaults[key]);
    },
    [flatConfig, flatDefaults],
  );

  useEffect(() => {
    if (data?.sections) {
      setLocalConfig(data.sections as Record<string, Record<string, unknown>>);
      setEdited({});
    }
  }, [data]);

  const handleChange = useCallback((key: string, value: unknown) => {
    setEdited((prev) => ({ ...prev, [key]: value }));
    setLocalConfig((prev) => {
      const section = key.split(".")[0];
      return { ...prev, [section]: { ...prev[section], [key]: value } };
    });
  }, []);

  const handleSave = useCallback(() => {
    if (Object.keys(edited).length === 0) return;
    setSaveMsg(null);
    updateMutation.mutate(edited, {
      onSuccess: (result) => {
        setEdited({});
        setSaveMsg(`Saved ${result.updated.length} setting(s)`);
        setTimeout(() => setSaveMsg(null), 3000);
      },
      onError: (err) => {
        setSaveMsg(`Error: ${err instanceof Error ? err.message : String(err)}`);
      },
    });
  }, [edited, updateMutation]);

  const handleDiscard = useCallback(() => {
    if (data?.sections) {
      setLocalConfig(data.sections as Record<string, Record<string, unknown>>);
      setEdited({});
    }
  }, [data]);

  const handleExport = useCallback(() => {
    api.exportConfig().catch((err) =>
      setSaveMsg(`Error: ${err instanceof Error ? err.message : String(err)}`),
    );
  }, []);

  const handleConfigImport = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setSaveMsg(null);
    importConfigMutation.mutate(file, {
      onSuccess: (result) => {
        setEdited({});
        setSaveMsg(`Imported ${result.imported} setting(s)`);
        setTimeout(() => setSaveMsg(null), 4000);
      },
      onError: (err) => {
        setSaveMsg(`Error: ${err instanceof Error ? err.message : String(err)}`);
      },
    });
  }, [importConfigMutation]);

  // Build entries for a section key — handles virtual sections
  const getEntries = useCallback((sectionKey: string): [string, unknown][] => {
    if (sectionKey === "_general_top") {
      return GENERAL_TOP_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_strategy_top") {
      return STRATEGY_TOP_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_cron_schedules") {
      return CRON_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_risk_mis") {
      return RISK_MIS_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_risk_cnc") {
      return RISK_CNC_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_strategy_mis") {
      return STRATEGY_MIS_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_strategy_cnc") {
      return STRATEGY_CNC_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    if (sectionKey === "_strategy_features") {
      return STRATEGY_FEATURE_KEYS.map((k) => [k, flatConfig[k]] as [string, unknown]).filter(([, v]) => v !== undefined);
    }
    // Normal section — filter out relocated keys
    const entries = Object.entries(localConfig[sectionKey] ?? {}).filter(([k]) => !RELOCATED_KEYS.has(k));
    if (sectionKey === "execution") {
      // Surface Transaction Mode at the very top of the Execution card
      // (stable sort keeps the remaining fields in their existing order).
      entries.sort(([a], [b]) =>
        a === "execution.transaction_mode" ? -1 : b === "execution.transaction_mode" ? 1 : 0,
      );
    }
    return entries;
  }, [localConfig, flatConfig]);

  if (isLoading) {
    return <div className="p-6 text-gray-400">Loading configuration...</div>;
  }
  if (error) {
    return (
      <div className="p-6 text-red-400">
        Failed to load configuration: {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const pendingCount = Object.keys(edited).length;
  const currentTab = TABS.find((t) => t.id === activeTab) || TABS[0];

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Sticky top region — header, tabs and the per-tab toolbar stay
          put while only the settings content below scrolls. */}
      <div className="shrink-0 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-100">Settings</h1>
          <p className="text-xs text-gray-500 mt-0.5">Changes take effect immediately after saving.</p>
        </div>
        <div className="flex items-center gap-2">
          <input
            ref={configUploadRef}
            type="file"
            accept=".json,application/json"
            onChange={handleConfigImport}
            className="hidden"
          />
          <button
            onClick={handleExport}
            className="px-3 py-1.5 rounded text-sm bg-gray-800 hover:bg-gray-700 text-gray-300"
            title="Download all settings as a JSON file (e.g. to copy to another instance)"
          >
            Export
          </button>
          <button
            onClick={() => configUploadRef.current?.click()}
            disabled={importConfigMutation.isPending}
            className="px-3 py-1.5 rounded text-sm bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-50"
            title="Import settings from an exported JSON file"
          >
            {importConfigMutation.isPending ? "Importing..." : "Import"}
          </button>
          {pendingCount > 0 && (
            <>
              <span className="text-xs text-amber-400">{pendingCount} unsaved</span>
              <button
                onClick={handleDiscard}
                className="px-3 py-1.5 rounded text-sm bg-gray-700 hover:bg-gray-600 text-gray-300"
              >
                Discard
              </button>
            </>
          )}
          <button
            onClick={handleSave}
            disabled={pendingCount === 0 || updateMutation.isPending}
            className="px-4 py-1.5 rounded text-sm font-medium bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {updateMutation.isPending ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {saveMsg && (
        <div className={clsx("text-sm px-3 py-2 rounded", saveMsg.startsWith("Error") ? "bg-red-900/30 text-red-400" : "bg-emerald-900/30 text-emerald-400")}>
          {saveMsg}
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-800 overflow-x-auto">
        {TABS.map((tab) => {
          const tabChanged = tab.sections.some((s) => {
            const entries = getEntries(s);
            return entries.some(([k]) => k in edited);
          });
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={clsx(
                "px-4 py-2.5 text-sm font-medium whitespace-nowrap border-b-2 transition-colors relative",
                activeTab === tab.id
                  ? "border-blue-500 text-blue-400"
                  : "border-transparent text-gray-500 hover:text-gray-300 hover:border-gray-700",
              )}
            >
              {tab.label}
              {tabChanged && <span className="absolute top-2 -right-0.5 w-1.5 h-1.5 rounded-full bg-amber-400" />}
            </button>
          );
        })}
      </div>

      {/* Per-tab toolbar */}
      <PerTabToolbar
        currentTab={currentTab}
        getEntries={getEntries}
        isDifferentFromDefault={isDifferentFromDefault}
        flatDefaults={flatDefaults}
        diffOnly={!!diffOnlyTabs[currentTab.id]}
        onToggleDiff={() =>
          setDiffOnlyTabs((prev) => ({
            ...prev,
            [currentTab.id]: !prev[currentTab.id],
          }))
        }
        onReset={(changedKeys) => {
          if (changedKeys.length === 0) return;
          if (
            !window.confirm(
              `Reset ${changedKeys.length} setting(s) on the "${currentTab.label}" tab back to default? ` +
                `Changes are staged and won't take effect until you press Save.`,
            )
          )
            return;
          changedKeys.forEach((k) => handleChange(k, flatDefaults[k]));
        }}
      />
      </div>

      {/* Scrollable content region — starts below the sticky tabs.
          The scroll lives on this wrapper (block, natural-height child)
          rather than on the columns element itself: CSS multi-column on a
          height-bounded box expands into extra columns horizontally
          instead of scrolling vertically. */}
      <div className="flex-1 overflow-y-auto min-h-0 pt-4">
      <DefaultsContext.Provider value={flatDefaults}>
      <FieldTypesContext.Provider value={fieldTypes}>
      {/* CSS columns instead of CSS grid so a tall card (e.g. the Risk
          Management list with 50+ rows) doesn't force its neighbours to
          grow with empty space below short cards. Each section uses
          break-inside-avoid so it never splits across columns mid-card. */}
      <div className="columns-1 lg:columns-2 gap-4 [column-fill:balance]">
        {currentTab.sections.map((sectionKey) => {
          let entries = getEntries(sectionKey);
          if (diffOnlyTabs[currentTab.id]) {
            // Keep keys currently in the unsaved-changes buffer even if
            // the user just typed them back to default — otherwise the
            // field vanishes mid-edit, which is jarring. After Save or
            // Discard, the buffer clears and the filter applies cleanly.
            entries = entries.filter(
              ([k]) => isDifferentFromDefault(k) || k in edited,
            );
          }
          if (entries.length === 0) return null;
          const title = SECTION_LABELS[sectionKey] ?? sectionKey;
          return (
            <SectionCard
              key={sectionKey}
              title={title}
              entries={entries}
              edited={edited}
              onChange={handleChange}
            />
          );
        })}
      </div>
      </FieldTypesContext.Provider>
      </DefaultsContext.Provider>
      </div>
    </div>
  );
}

function PerTabToolbar({
  currentTab,
  getEntries,
  isDifferentFromDefault,
  flatDefaults,
  diffOnly,
  onToggleDiff,
  onReset,
}: {
  currentTab: Tab;
  getEntries: (sectionKey: string) => [string, unknown][];
  isDifferentFromDefault: (key: string) => boolean;
  flatDefaults: Record<string, unknown>;
  diffOnly: boolean;
  onToggleDiff: () => void;
  onReset: (changedKeys: string[]) => void;
}) {
  // All keys on this tab, across virtual + real sections, deduped.
  const tabKeys = useMemo(() => {
    const seen = new Set<string>();
    for (const sectionKey of currentTab.sections) {
      for (const [k] of getEntries(sectionKey)) seen.add(k);
    }
    return Array.from(seen);
  }, [currentTab, getEntries]);

  const changedKeys = useMemo(
    () => tabKeys.filter((k) => isDifferentFromDefault(k)),
    [tabKeys, isDifferentFromDefault],
  );

  const defaultsLoaded = Object.keys(flatDefaults).length > 0;

  return (
    <div className="flex items-center justify-end gap-2 text-xs">
      <span className="text-gray-500">
        {changedKeys.length} of {tabKeys.length} differ from default
      </span>
      <button
        type="button"
        onClick={onToggleDiff}
        disabled={!defaultsLoaded}
        className={clsx(
          "px-2.5 py-1 rounded border transition-colors disabled:opacity-40",
          diffOnly
            ? "bg-blue-900/40 border-blue-700 text-blue-300"
            : "bg-gray-800 border-gray-700 text-gray-300 hover:bg-gray-700",
        )}
        title="Show only settings that differ from their default value"
      >
        {diffOnly ? "Showing diff" : "Diff from default"}
      </button>
      <button
        type="button"
        onClick={() => onReset(changedKeys)}
        disabled={!defaultsLoaded || changedKeys.length === 0}
        className="px-2.5 py-1 rounded border bg-gray-800 border-gray-700 text-gray-300 hover:bg-gray-700 transition-colors disabled:opacity-40 disabled:hover:bg-gray-800"
        title="Stage all settings on this tab to their default values (still requires Save)"
      >
        Reset to default
      </button>
    </div>
  );
}
