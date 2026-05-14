import { useState, useEffect, useCallback } from "react";
import { useConfig, useUpdateConfig } from "../hooks/queries";
import clsx from "clsx";

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
    sections: ["_strategy_top", "strategy", "scanning", "retraining"],
  },
  {
    id: "risk",
    label: "Risk & Execution",
    sections: ["risk", "execution", "transaction_costs"],
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
];

// Keys to hide from their original sections (shown in virtual sections instead)
const RELOCATED_KEYS = new Set([
  ...GENERAL_TOP_KEYS,
  ...CRON_KEYS,
  ...STRATEGY_TOP_KEYS,
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

// ---------------------------------------------------------------------------
// Section labels
// ---------------------------------------------------------------------------

const SECTION_LABELS: Record<string, string> = {
  _general_top: "General",
  _strategy_top: "Strategy — Core",
  _cron_schedules: "Cron Schedules",
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
  "strategy.feedback.enabled": "ML Feedback Loop",
  "strategy.feedback.lookback_days": "Feedback Lookback (days)",
  "strategy.feedback.sample_weight_boost": "Sample Weight Boost",
  "strategy.feedback.sources.predictions": "Source: Predictions",
  "strategy.feedback.sources.dry_runs": "Source: Dry Runs",
  "strategy.feedback.sources.trades": "Source: Trades",
  "risk.max_risk_per_trade_pct": "Max Risk / Trade",
  "risk.max_portfolio_exposure_pct": "Max Portfolio Exposure",
  "risk.max_open_positions": "Max Open Positions",
  "risk.max_single_stock_pct": "Max Single Stock Exposure",
  "risk.daily_loss_limit_pct": "Daily Loss Limit",
  "risk.weekly_loss_limit_pct": "Weekly Loss Limit",
  "risk.weekly_loss_sizing_reduction": "Weekly Loss Size Reduction",
  "risk.mandatory_stop_loss": "Mandatory Stop Loss",
  "risk.trailing_sl_enabled": "Trailing Stop Loss",
  "risk.trailing_sl_trigger_multiple": "Trailing SL Trigger (× risk)",
  "risk.trailing_sl_step_pct": "Trailing SL Step",
  "risk.target_early_exit_pct": "Target Early-Exit Buffer",
  "risk.min_confidence_buy": "Min Confidence (BUY)",
  "risk.min_confidence_sell": "Min Confidence (SELL)",
  "risk.skip_sell_on_holdings": "Skip SELL on Holdings",
  "risk.max_trades_per_day": "Max Trades / Day",
  "risk.kill_switch_enabled": "Kill Switch",
  "risk.llm_review_enabled": "LLM Trade Review",
  "risk.llm_fallback_to_rules": "Fallback to Rules if LLM Down",
  "risk.max_same_sector_positions": "Max Same Sector Positions",
  "risk.margin_usage_enabled": "Margin / Leverage",
  "risk.weekly_reset_day": "Weekly PnL Reset Day",
  "risk.loss_cooldown_minutes": "Loss Cooldown (min)",
  "risk.symbol_cooldown_days": "Symbol Cooldown (days)",
  "risk.symbol_repeat_lookback_days": "Repeat Symbol Lookback (days)",
  "risk.symbol_repeat_min_confidence": "Repeat Symbol Min Confidence",
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
  "database.backup_enabled": "Backups Enabled",
  "database.retention.ohlcv_days": "OHLCV Retention (days)",
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
  "strategy.min_training_samples": "Minimum data points required to train an ML model.",
  "strategy.ema_periods": "Exponential moving average periods used in technical analysis.",
  "strategy.allowed_holding_periods": "Which holding periods the strategy is allowed to use.",
  "strategy.holding_periods.intraday.target": "ATR multiplier for intraday profit target. target = entry ± N × ATR.",
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
  "strategy.feedback.enabled": "Enable ML feedback loop — model learns from its own performance.",
  "strategy.feedback.lookback_days": "How far back to aggregate feedback data for retraining.",
  "strategy.feedback.sample_weight_boost": "Weight multiplier for symbols where model performed poorly.",
  "strategy.feedback.sources.predictions": "Include scored prediction outcomes in feedback.",
  "strategy.feedback.sources.dry_runs": "Include scored dry run results in feedback.",
  "strategy.feedback.sources.trades": "Include closed trade PnL and slippage in feedback.",
  "risk.max_risk_per_trade_pct": "Maximum capital risked per trade (e.g. 0.02 = 2%).",
  "risk.max_portfolio_exposure_pct": "Maximum total portfolio exposure. Remainder stays as cash.",
  "risk.max_open_positions": "Maximum simultaneous open positions.",
  "risk.max_single_stock_pct": "Maximum capital allocated to any single stock.",
  "risk.daily_loss_limit_pct": "Stop trading for the day if portfolio drops this much.",
  "risk.weekly_loss_limit_pct": "Weekly circuit breaker — reduces sizing when hit.",
  "risk.weekly_loss_sizing_reduction": "Reduce position sizes by this factor when weekly breaker triggers.",
  "risk.mandatory_stop_loss": "Every trade must have a stop loss. Cannot be disabled in production.",
  "risk.trailing_sl_enabled": "Automatically trail stop loss upward as price moves in your favor.",
  "risk.trailing_sl_trigger_multiple": "Activate trailing SL when profit reaches this multiple of risk.",
  "risk.trailing_sl_step_pct": "Trail the stop loss in steps of this percentage.",
  "risk.target_early_exit_pct": "Exit when price is within this percentage of target. Heartbeats run every 15 min; without a buffer a price that gets within a paisa of target but never touches it waits a full cycle and may reverse. Default 0.15% catches ~₹0.15 on a ₹100 stock.",
  "risk.min_confidence_buy": "Minimum ML confidence for a BUY signal (0–1).",
  "risk.min_confidence_sell": "Minimum ML confidence for a SELL signal (0–1). Set higher than BUY to avoid exit noise.",
  "risk.skip_sell_on_holdings": "Don't generate SELL signals for symbols you already hold — position-monitor handles exits.",
  "risk.max_trades_per_day": "Maximum trades per day including re-entries.",
  "risk.kill_switch_enabled": "Allow /stop and /kill commands to halt all trading.",
  "risk.llm_review_enabled": "Gemini reviews each trade before execution (APPROVE/REJECT/RESIZE).",
  "risk.llm_fallback_to_rules": "Use rules-only risk check if LLM is unavailable.",
  "risk.max_same_sector_positions": "Maximum open positions in the same sector (correlation limit).",
  "risk.margin_usage_enabled": "When disabled, position value is capped by available cash (no leverage).",
  "risk.weekly_reset_day": "Day when weekly circuit breaker PnL counter resets.",
  "risk.loss_cooldown_minutes": "Wait this long after a losing trade before entering the next one.",
  "risk.symbol_cooldown_days": "Hard block on re-trading a symbol for this many days after last trade.",
  "risk.symbol_repeat_lookback_days": "Window during which repeat symbols need elevated confidence.",
  "risk.symbol_repeat_min_confidence": "Confidence required to re-trade a symbol within the lookback window — set higher than the per-direction BUY/SELL thresholds.",
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
  "database.backup_enabled": "Enable daily automatic database backups.",
  "database.retention.ohlcv_days": "Keep OHLCV price data for this many days.",
  "database.retention.audit_log_days": "Keep audit log entries for this many days.",
  "database.retention.predictions_days": "Keep ML prediction records for this many days.",
  "database.retention.news_days": "Keep news articles for this many days.",
  "database.retention.economic_events_days": "Keep economic calendar events for this many days.",
  "retraining.shadow_mode_days": "Run new model in shadow alongside production for this many days.",
  "retraining.shadow_min_predictions": "Minimum scored predictions before promotion decision.",
  "retraining.retired_model_cleanup_days": "Auto-delete retired model files after this many days.",
  "retraining.max_training_days": "Cap how far back daily bars are loaded for training. Default 730 (2 years) — fits a 2 GB host with ~500 symbols. Raise on larger hosts; the full ohlcv table (5 years × 500 symbols) can OOM the feature-matrix builder.",
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
  const [open, setOpen] = useState(false);
  const hasDescription = !!description;
  // Always render the icon — every setting should have one so the user
  // can at least see the canonical dotted key (useful for /run, docs,
  // /symbol contexts) even when we haven't written a description yet.
  const tooltip = hasDescription
    ? description
    : `Config key: ${fullKey}\nDescription not yet written — file an issue if unclear.`;
  return (
    <span className="relative inline-flex shrink-0">
      <button
        type="button"
        className={clsx(
          "inline-flex items-center justify-center w-4 h-4 rounded-full text-[9px] font-bold cursor-help shrink-0 transition-colors",
          hasDescription
            ? "bg-gray-800 border border-gray-600 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
            : "bg-gray-900 border border-dashed border-gray-700 text-gray-600 hover:text-gray-400 hover:border-gray-500",
        )}
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        onBlur={() => setOpen(false)}
      >
        i
      </button>
      {open && (
        <span className="absolute left-1/2 -translate-x-1/2 bottom-full mb-1.5 z-50 w-56 px-2.5 py-1.5 rounded bg-gray-700 border border-gray-600 text-[11px] text-gray-200 leading-snug shadow-lg whitespace-pre-line">
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
  return (
    <div className="flex items-center justify-between py-2.5 gap-4">
      <FieldLabel label={label} description={description} fullKey={fullKey} hint={hint} />
      <input
        type="number"
        step={value % 1 !== 0 ? 0.001 : 1}
        value={value}
        onChange={(e) => {
          const v = e.target.value;
          onChange(v.includes(".") ? parseFloat(v) : parseInt(v, 10));
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
    <div className="bg-gray-900 border border-gray-800 rounded-lg">
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

export default function SettingsPage() {
  const { data, isLoading, error } = useConfig();
  const updateMutation = useUpdateConfig();

  const [activeTab, setActiveTab] = useState("general");
  const [edited, setEdited] = useState<Record<string, unknown>>({});
  const [localConfig, setLocalConfig] = useState<Record<string, Record<string, unknown>>>({});
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  // Flatten all config into a single lookup for virtual sections
  const flatConfig: Record<string, unknown> = {};
  for (const section of Object.values(localConfig)) {
    for (const [k, v] of Object.entries(section)) {
      flatConfig[k] = v;
    }
  }

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
    // Normal section — filter out relocated keys
    return Object.entries(localConfig[sectionKey] ?? {}).filter(([k]) => !RELOCATED_KEYS.has(k));
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
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-100">Settings</h1>
          <p className="text-xs text-gray-500 mt-0.5">Changes take effect immediately after saving.</p>
        </div>
        <div className="flex items-center gap-2">
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

      {/* Tab content */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {currentTab.sections.map((sectionKey) => {
          const entries = getEntries(sectionKey);
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
    </div>
  );
}
