export interface HealthResponse {
  status: "ok" | "degraded";
  database: boolean;
  mode: "paper" | "live";
  timezone?: string;
}

export interface PortfolioState {
  total_capital: number;
  available_cash: number;
  exposure_pct: number;
  open_positions: number;
  system_positions: number;
  adopted_positions: number;
  system_position_value: number;
  adopted_position_value: number;
  stock_exposures: Record<string, number>;
  sector_counts: Record<string, number>;
  daily_pnl_pct: number;
  weekly_pnl_pct: number;
  daily_pnl: number;
  daily_charges: number;
  weekly_pnl: number;
  weekly_charges: number;
  trades_today: number;
  minutes_since_last_loss: number;
  // Broker-synced capital breakdown
  available_funds: number;
  utilised_margin: number;
  pending_trade_value: number;
  locked_total: number;
  holdings_invested: number;
  holdings_current: number;
  holdings_unrealized_pnl: number;
  holdings_unrealized_pnl_pct: number;
  total_portfolio_value: number;
  total_pnl: number;
  all_time_realized_pnl: number;
  all_time_charges: number;
}

export interface Trade {
  trade_id: string;
  symbol: string;
  signal_type: "BUY" | "SELL";
  entry_price: number;
  fill_price: number;
  quantity: number;
  stop_loss_price: number;
  target_price: number;
  order_id: string | null;
  sl_order_id: string | null;
  target_order_id: string | null;
  gtt_id: number | null;
  gtt_status: string | null;
  origin: "system" | "adopted" | null;
  product: "MIS" | "CNC";
  mode: "paper" | "live";
  status: string;
  slippage: number;
  estimated_costs: number | null;
  pnl: number | null;
  exit_price: number | null;
  // Accumulated realised PnL from partial-close bookings, if any.
  // Total PnL surfaced to the user is realized_partial_pnl + (pnl ?? 0).
  // Null/undefined on rows from before migration 043.
  realized_partial_pnl?: number | null;
  // Producing model's version, stamped at execution (migration 050);
  // read paths COALESCE with the signal's version for legacy rows.
  // Null for adopted/manual trades — no model produced them.
  model_version?: string | null;
  created_at: string;
  closed_at: string | null;
}

export interface CostBreakdown {
  brokerage: number;
  stt: number;
  other_charges: number;
  total: number;
  source?: "broker" | "estimate" | "contract_note";
}

export interface BrokerOrderHistoryRow {
  order_id: string;
  status: string;
  status_message?: string | null;
  order_timestamp?: string;
  exchange_timestamp?: string;
  average_price?: number;
  filled_quantity?: number;
  pending_quantity?: number;
  quantity?: number;
  price?: number;
  trigger_price?: number;
  order_type?: string;
  transaction_type?: string;
  product?: string;
  tag?: string;
}

export interface BrokerOrderTradeRow {
  trade_id?: string;
  order_id?: string;
  fill_timestamp?: string;
  exchange_timestamp?: string;
  quantity: number;
  average_price: number;
  transaction_type?: string;
}

export interface TradeOrderDetailLeg {
  order_id: string;
  history: BrokerOrderHistoryRow[];
  fills: BrokerOrderTradeRow[];
}

export interface TradeOrderDetail {
  trade_id: string;
  legs: {
    entry?: TradeOrderDetailLeg;
    sl?: TradeOrderDetailLeg;
    target?: TradeOrderDetailLeg;
  };
}

export interface GttEvent {
  id: number;
  timestamp_utc: string;
  trade_id: string | null;
  gtt_id: number | null;
  symbol: string | null;
  event_type: string;
  status: string | null;
  details_json: string | null;
}

export interface LLMReview {
  id: number;
  trade_id: string;
  decision: "APPROVE" | "REJECT" | "RESIZE";
  reasoning: string;
  adjusted_size: number | null;
  created_at: string;
}

export interface Signal {
  id: number;
  symbol: string;
  signal_type: string;
  entry_price: number;
  target_price: number;
  stop_loss_price: number;
  position_size: number;
  confidence_score: number;
  model_version: string;
  features_snapshot: string | null;
  attribution_json: string | null;
  created_at: string;
}

export interface FeatureAttribution {
  feature: string;
  value: number;
  contribution: number;
}

export interface Prediction {
  prediction_id: string;
  signal_id: number;
  trade_id: string;
  created_at: string;
  prediction_end_time: string | null;
  actual_price: number | null;
  direction_correct: boolean | null;
  target_hit: boolean | null;
  actual_pnl_pct: number | null;
}

export interface AuditEntry {
  id: number;
  timestamp_ist: string;
  action_type: string;
  skill_name: string | null;
  input_summary: string | null;
  output_summary: string | null;
  duration_ms: number | null;
  created_at: string;
}

export interface TradeDetail extends Trade {
  llm_review: LLMReview | null;
  prediction: Prediction | null;
  signal: Signal | null;
  audit_trail: AuditEntry[];
  cost_breakdown?: CostBreakdown;
  gtt_events?: GttEvent[];
}

export interface EquityCurvePoint {
  date: string;
  daily_pnl: number | null;
  cumulative_pnl: number;
  trade_count: number;
}

export interface PnlCalendarDay {
  date: string;
  pnl: number;
  trade_count: number;
  wins: number;
  losses: number;
}

export interface WatchlistItem {
  symbol: string;
  composite_score: number | null;
  technical_score: number | null;
  volume_momentum_score: number | null;
  news_sentiment_score: number | null;
  fundamental_score: number | null;
  sector: string | null;
  updated_at: string;
  source?: "algo" | "user" | "both";
}

export interface UserWatchlistItem {
  symbol: string;
  sector: string | null;
  notes: string | null;
  created_at: string;
  composite_score: number | null;
  technical_score: number | null;
  volume_momentum_score: number | null;
  news_sentiment_score: number | null;
  fundamental_score: number | null;
}

export interface SectorRotation {
  strong: string[];
  weak: string[];
  sectors: Record<string, { avg_score: number; count: number }>;
}

export interface ScoreboardEntry {
  id: number;
  group_key: string;
  group_type: string;
  total_predictions: number;
  correct_predictions: number;
  accuracy: number | null;
  avg_confidence: number | null;
  target_hit_rate: number | null;
  avg_pnl_pct: number | null;
  updated_at: string;
}

export interface SlippageStats {
  total_trades: number;
  avg_slippage: number;
  max_slippage: number;
  avg_slippage_pct: number;
  by_symbol: Record<
    string,
    { count: number; avg_slippage: number; max_slippage: number }
  >;
}

export interface LLMAccuracy {
  total_reviews: number;
  approved_count: number;
  rejected_count: number;
  approved_with_outcomes: number;
  profitable_approvals: number;
  losing_approvals: number;
  approval_accuracy: number | null;
  approved_total_pnl: number;
  approved_avg_pnl: number;
}

export interface Report {
  id: number;
  report_type: "daily" | "weekly";
  report_date: string;
  content: Record<string, unknown>;
  created_at: string;
}

export type SignalDisposition =
  | "pending"
  | "risk_rejected"
  | "llm_rejected"
  | "awaiting_approval"
  | "executed"
  | "expired"
  | "rejected"
  | "recently_rejected_dedup";

export interface Recommendation {
  id: number;
  symbol: string;
  signal_type: "BUY" | "SELL";
  entry_price: number;
  target_price: number;
  stop_loss_price: number;
  position_size: number;
  confidence_score: number;
  model_version: string;
  disposition: SignalDisposition;
  disposition_reason: string | null;
  created_at: string;
  // Holding-period decision + derived economics (backend-enriched).
  product?: string | null; // "MIS" (intraday) / "CNC" (delivery)
  holding_period?: string | null;
  expected_holding_days?: number | null;
  target_date?: string | null; // predicted exit date (YYYY-MM-DD)
  estimated_costs?: number | null;
  est_net_gain?: number | null; // net P&L if target hits, after costs
  est_net_loss?: number | null; // net P&L if SL hits, after costs (<= 0)
}

export interface GeminiStatus {
  enabled: boolean;
  configured: boolean;
  connected: boolean;
  model: string;
}

export interface ZerodhaStatus {
  configured: boolean;
  connected: boolean;
  mode: "paper" | "live";
  login_url: string | null;
  margins: Record<string, unknown> | null;
}

export interface TelegramStatus {
  configured: boolean;
  enabled: boolean;
  chat_id: string;
  hint?: string;
}

export interface IntegrationsStatus {
  gemini: GeminiStatus;
  zerodha: ZerodhaStatus;
  telegram: TelegramStatus;
}

export interface ActionResult {
  success: boolean;
  error?: string;
  margins?: Record<string, unknown> | null;
}

// --- New types for enhanced UI ---

export interface EconomicEvent {
  event_date: string;
  event_type: string;
  title: string;
  country: string;
  impact: "high" | "medium" | "low";
  source: string;
  symbol?: string;
}

export interface EarningsEvent {
  event_date: string;
  title: string;
  symbol: string;
  impact: string;
  source: string;
}

export interface NewsArticle {
  content_hash: string;
  headline: string;
  source: string;
  url: string;
  symbols: string[];
  published_at: string;
}

export interface SentimentResult {
  symbol: string;
  sentiment: "bullish" | "bearish" | "neutral";
  confidence: number;
  key_drivers: string[];
}

export interface MLModelInfo {
  model_type: string;
  version: string;
  file_path?: string;
  sharpe_ratio?: number;
  max_drawdown_pct?: number;
  win_rate?: number;
  profit_factor?: number;
  status?: string;
}

export interface MLModelsResponse {
  production: Record<string, MLModelInfo>;
  shadow: MLModelInfo[];
  retired: MLModelInfo[];
}

export interface PredictionDetail {
  prediction_id: string;
  signal_id: number;
  trade_id: string;
  symbol?: string;
  signal_type?: string;
  confidence_score?: number;
  model_version?: string;
  created_at: string;
  prediction_end_time: string | null;
  actual_price: number | null;
  direction_correct: boolean | null;
  target_hit: boolean | null;
  actual_pnl_pct: number | null;
  // Signal setup (joined from signals/trades) — mirrors the dry-run / signal detail.
  entry_price?: number | null;
  target_price?: number | null;
  stop_loss_price?: number | null;
  product?: string | null;
  holding_period?: string | null;
  expected_holding_days?: number | null;
}

export interface PaginatedPredictions {
  items: PredictionDetail[];
  total: number;
}

export interface PredictionFilters {
  symbol?: string;
  direction?: "BUY" | "SELL";
  direction_correct?: 0 | 1;
  target_hit?: 0 | 1;
  model?: string;
  min_confidence?: number;
}

export interface RiskExposure {
  total_capital: number;
  exposure_pct: number;
  stock_exposures: Record<string, number>;
  sector_counts: Record<string, number>;
  sector_exposure_value: Record<string, number>;
  sector_exposure_pct: Record<string, number>;
  positions_count: number;
}

export interface RiskGates {
  drift: {
    enabled: boolean;
    suspended: boolean;
    reason: string | null;
  };
  beta: {
    enabled: boolean;
    cap_multiple: number;
    cap_value: number;
    current_beta_weighted: number;
    utilization_pct: number;
    positions: {
      symbol: string;
      beta: number;
      notional: number;
      beta_weighted: number;
      estimated: boolean;
    }[];
  };
  earnings: {
    enabled: boolean;
    window_days: number;
    blocked_symbols: {
      symbol: string;
      event_date: string | null;
      title: string | null;
      held: boolean;
    }[];
  };
}

export interface PremarketData {
  date: string | null;
  gift_nifty_change_pct: number | null;
  us_sp500_change_pct?: number | null;
  market_bias: string | null;
  llm_summary?: string | null;
}

export interface DegradedFeature {
  feature: string;
  status: string;
  impact: string;
}

export interface SystemState {
  kill_switch_active: boolean;
  /** Which command activated the pause: pause / stop / kill. "" when inactive. */
  kill_switch_mode?: "pause" | "stop" | "kill" | "";
  orchestrator: string | null;
  mode: "paper" | "live";
  degraded_features?: DegradedFeature[];
  is_degraded?: boolean;
  show_degraded_banner?: boolean;
  auto_approved_today?: number;
  llm_reviewed_today?: number;
  // Set by the cdsl-auth-check CRON skill (and live refreshes from
  // the banner's button). Null when never checked. Drives the
  // CdslAuthBanner — see components/CdslAuthBanner.tsx.
  cdsl_auth?: {
    authenticated: boolean;
    needs_auth?: boolean;
    // True iff needs_auth AND has_active_cnc_exits. This is the
    // gate the UI banner / Telegram alert keys off — having
    // unauthorised holdings alone doesn't trigger the alert if
    // nothing the system manages might try to sell today.
    alert_needed?: boolean;
    has_active_cnc_exits?: boolean;
    active_cnc_positions?: number;
    active_gtts?: number;
    pending_cnc_sells?: number;
    pending_qty?: number;
    pending_count?: number;
    pending_symbols?: Array<{
      symbol: string;
      isin?: string;
      deliverable_qty: number;
      authorised_qty: number;
      pending_qty: number;
    }>;
    checked_at?: string | null;
    ddpi_likely_enabled?: boolean;
  } | null;
}

export interface NSESymbol {
  symbol: string;
  sector?: string;
  industry?: string;
  [key: string]: unknown;
}

export interface OHLCVBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  delivery_pct?: number | null;
}

export interface StrategyPerformance {
  by_signal_type: PerformanceRow[];
  by_product: PerformanceRow[];
  by_hour: PerformanceRow[];
  by_sector: PerformanceRow[];
  by_holding_period: PerformanceRow[];
}

export interface PerformanceRow {
  signal_type?: string;
  product?: string;
  hour?: number;
  sector?: string;
  holding_period?: string;
  cnt: number;
  wins: number;
  losses: number;
  total_pnl: number;
  avg_pnl: number;
}

export interface ModelDriftDayPoint {
  date: string;
  predicted_win_rate: number | null;
  realised_win_rate: number;
  sample_size: number;
}

export interface ModelDriftCalibrationBucket {
  bucket: string;
  predicted_mean: number | null;
  realised_rate: number | null;
  samples: number;
}

export interface ModelDriftVersion {
  model_type: string;
  version: string;
  is_production: boolean;
  by_day: ModelDriftDayPoint[];
  calibration_buckets: ModelDriftCalibrationBucket[];
}

export interface ModelDrift {
  model_versions: ModelDriftVersion[];
  warning: string | null;
}

export interface FiiDiiDayPoint {
  date: string;
  fii_buy: number;
  fii_sell: number;
  fii_net: number;
  dii_buy: number;
  dii_sell: number;
  dii_net: number;
}

export interface FiiDiiSummary {
  days_covered: number;
  fii_net_total: number;
  dii_net_total: number;
  fii_net_today: number | null;
  dii_net_today: number | null;
}

export interface BulkDealRow {
  deal_date: string;
  symbol: string;
  deal_type: string;
  client_name: string | null;
  buy_sell: string | null;
  quantity: number | null;
  trade_price: number | null;
}

export interface InstitutionalFlows {
  fii_dii_timeline: FiiDiiDayPoint[];
  fii_dii_summary: FiiDiiSummary;
  bulk_deals: BulkDealRow[];
}

export interface QuarantineEntry {
  symbol: string;
  consecutive_failures: number;
  last_error: string | null;
  quarantined_at: string | null;
  replacement_symbol: string | null;
}

export interface SymbolContext {
  quarantine: QuarantineEntry | null;
  recent_bulk_deals: BulkDealRow[];
  delivery_pct_avg_5d: number | null;
  latest_signal: SymbolLatestSignal | null;
}

export interface SymbolLatestSignal {
  signal_type: string;
  confidence_score: number | null;
  disposition: string | null;
  created_at: string;
  attribution: { feature: string; contribution: number }[];
}

export interface SymbolQuickContext {
  symbol: string;
  sector: string | null;
  ltp: number | null;
  bars: {
    timestamp: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
  }[];
  avg_volume_20d: number | null;
  quarantine: { is_quarantined: boolean; reason: string | null };
  is_locked: boolean;
  open_position: {
    signal_type: string;
    quantity: number;
    fill_price: number | null;
    entry_price: number;
    target_price: number;
    stop_loss_price: number;
    product: string;
  } | null;
  todays_signal: {
    signal_type: string;
    confidence_score: number | null;
    disposition: string | null;
    disposition_reason: string | null;
    created_at: string;
  } | null;
}

export interface RotationCooldown {
  enabled: boolean;
  no_signal_threshold: number;
  cooldown_hours: number;
  symbols: string[];
  count: number;
}

export interface ExecutionQuality {
  total_orders: number;
  filled_orders: number;
  fill_rate_pct: number;
  avg_abs_slippage: number;
  max_abs_slippage: number;
  avg_signed_slippage: number;
  zero_slippage_pct: number;
  slippage_by_hour: { hour: number; cnt: number; avg_slippage: number; max_slippage: number }[];
  slippage_by_size: { size_bucket: string; cnt: number; avg_slippage: number; max_slippage: number }[];
}

export interface CorrelationData {
  symbols: string[];
  matrix: number[][];
}

export interface PriceAlert {
  id: number;
  symbol: string;
  target_price: number;
  direction: "above" | "below";
  note: string | null;
  active: number;
  triggered_at: string | null;
  created_at: string;
}

export interface RiskSimParams {
  max_exposure_pct: number;
  max_single_stock_pct: number;
  max_positions: number;
  initial_capital: number;
  date_from?: string;
  date_to?: string;
  /** Replay set: `signals` (default) replays generated signals, `trades`
   * replays actually-executed trades from the trades table. */
  source?: "signals" | "trades";
}

export interface RiskSimResult {
  params: RiskSimParams;
  signals_available: number;
  signals_without_pnl: number;
  results: {
    trades_taken: number;
    trades_skipped: number;
    total_pnl: number;
    final_capital: number;
    win_rate: number;
    wins: number;
    losses: number;
    max_drawdown_pct: number;
    return_pct: number;
  };
}

export interface WeeklyLLMReview {
  id: number;
  trade_id: string;
  decision: "APPROVE" | "REJECT" | "RESIZE";
  reasoning: string;
  pnl?: number | null;
  created_at: string;
}

export interface TableStats {
  row_count: number;
  oldest: string | null;
  newest: string | null;
}

export interface DbFileStats {
  db_bytes: number;
  wal_bytes: number;
  total_bytes: number;
}

export interface StorageStats {
  ohlcv: TableStats;
  news_articles: TableStats;
  economic_events: TableStats;
  audit_log: TableStats;
  predictions: TableStats;
  trades: TableStats;
  agent_memory: TableStats;
  _db_file: DbFileStats;
  [key: string]: TableStats | DbFileStats;
}

export interface CleanupResult {
  success: boolean;
  table: string;
  rows_deleted: number;
}

export interface BackupResult {
  success: boolean;
  backup_path: string;
}

export interface BackupEntry {
  filename: string;
  size_bytes: number;
  created_at: string;
  /** When true, the backup is pinned via a sibling .lock sentinel —
   * the daily prune and manual delete will both skip it until the
   * lock is cleared. */
  locked: boolean;
}

export interface ResetResult {
  success: boolean;
  total_rows_deleted: number;
  by_table: Record<string, number>;
}

export interface DryRunSignal {
  id: number;
  run_id: string;
  symbol: string;
  signal_type: string;
  entry_price: number;
  target_price: number;
  stop_loss_price: number;
  confidence_score: number;
  position_size: number | null;
  model_version: string | null;
  holding_period: string | null;
  expected_holding_days: number | null;
  product: string | null;
  estimated_costs: number | null;
  volatility_score: number | null;
  composite_score: number | null;
  technical_score: number | null;
  volume_momentum_score: number | null;
  news_sentiment_score: number | null;
  fundamental_score: number | null;
  actual_open: number | null;
  actual_close: number | null;
  actual_high: number | null;
  actual_low: number | null;
  direction_correct: number | null;
  target_hit: number | null;
  actual_move_pct: number | null;
  created_at: string;
  scored_at: string | null;
  // Derived economics (backend-enriched, same as recommendations).
  target_date?: string | null; // predicted exit date (YYYY-MM-DD)
  est_net_gain?: number | null;
  est_net_loss?: number | null;
}

export interface DryRunSummary {
  run_id: string;
  signal_count: number;
  created_at: string;
  correct: number | null;
  scored: number;
  strategy_mode: string | null;
  as_of?: string | null;
  model_version?: string | null;
}

export interface HoldingsResponse {
  holdings: HoldingEntry[];
  broker_authenticated: boolean;
  login_url?: string;
}

export interface HoldingEntry {
  tradingsymbol: string;
  exchange: string;
  quantity: number;
  average_price: number;
  last_price: number;
  close_price: number;
  pnl: number;
  day_change: number;
  day_change_percentage: number;
  isin?: string;
  t1_quantity?: number;
  locked?: boolean;
}

export interface ManualOrder {
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  order_type: "MARKET" | "LIMIT" | "SL" | "SL-M";
  product: "CNC" | "MIS";
  price?: number;
  trigger_price?: number;
}

export interface DryRunSelectedModel {
  version: string;
  model_type: string;
  status: string | null;
}

export interface DryRunResult {
  success: boolean;
  run_id: string;
  mode?: string;
  as_of?: string | null;
  selected_model?: DryRunSelectedModel | null;
  universe_size: number;
  shortlist_size: number;
  signals: DryRunSignal[];
  // Present when a past-date run was auto-scored in the same request.
  scoring?: { scored: number; pending?: number; not_found?: number; already_scored?: number; message?: string } | null;
  warning?: string;
}

export interface HolidaysResponse {
  holidays: string[];
  early_close_days: Record<string, string>;
}

export interface ConfigSections {
  sections: Record<string, Record<string, unknown>>;
}

export interface ConfigUpdateResult {
  status: string;
  updated: string[];
  sections: Record<string, Record<string, unknown>>;
}

export interface SignalClassDay {
  date: string;
  BUY: number;
  HOLD: number;
  SELL: number;
}

export interface SignalClassDistribution {
  BUY: number;
  HOLD: number;
  SELL: number;
  total: number;
  by_day: SignalClassDay[];
}
