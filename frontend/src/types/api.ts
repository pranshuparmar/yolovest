export interface HealthResponse {
  status: "ok" | "degraded";
  database: boolean;
  mode: "paper" | "live";
}

export interface PortfolioState {
  total_capital: number;
  available_cash: number;
  exposure_pct: number;
  open_positions: number;
  stock_exposures: Record<string, number>;
  sector_counts: Record<string, number>;
  daily_pnl_pct: number;
  weekly_pnl_pct: number;
  trades_today: number;
  minutes_since_last_loss: number;
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
  product: "MIS" | "CNC";
  mode: "paper" | "live";
  status: string;
  slippage: number;
  pnl: number | null;
  exit_price: number | null;
  created_at: string;
  closed_at: string | null;
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
  created_at: string;
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
}

export interface EquityCurvePoint {
  date: string;
  daily_pnl: number | null;
  cumulative_pnl: number;
  trade_count: number;
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

export interface GeminiStatus {
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
  created_at: string;
  prediction_end_time: string | null;
  actual_price: number | null;
  direction_correct: boolean | null;
  target_hit: boolean | null;
  actual_pnl_pct: number | null;
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
  orchestrator: string | null;
  mode: "paper" | "live";
  degraded_features?: DegradedFeature[];
  is_degraded?: boolean;
  show_degraded_banner?: boolean;
  auto_approved_today?: number;
  llm_reviewed_today?: number;
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
}

export interface RiskSimResult {
  params: RiskSimParams;
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
}

export interface DryRunSummary {
  run_id: string;
  signal_count: number;
  created_at: string;
  correct: number | null;
  scored: number;
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

export interface DryRunResult {
  success: boolean;
  run_id: string;
  universe_size: number;
  shortlist_size: number;
  signals: DryRunSignal[];
  warning?: string;
}
