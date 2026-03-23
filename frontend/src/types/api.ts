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
