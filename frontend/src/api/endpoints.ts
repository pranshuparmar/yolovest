import { apiFetch } from "./client";
import type {
  HealthResponse,
  PortfolioState,
  Trade,
  TradeDetail,
  EquityCurvePoint,
  WatchlistItem,
  SectorRotation,
  ScoreboardEntry,
  SlippageStats,
  LLMAccuracy,
  Report,
  AuditEntry,
  IntegrationsStatus,
  ActionResult,
  UserWatchlistItem,
  EconomicEvent,
  EarningsEvent,
  NewsArticle,
  SentimentResult,
  MLModelsResponse,
  PredictionDetail,
  RiskExposure,
  PremarketData,
  SystemState,
  NSESymbol,
  WeeklyLLMReview,
  OHLCVBar,
  StrategyPerformance,
  ExecutionQuality,
  CorrelationData,
  PriceAlert,
  RiskSimParams,
  RiskSimResult,
  StorageStats,
  CleanupResult,
  BackupResult,
  BackupEntry,
  ResetResult,
  DryRunResult,
  DryRunSummary,
  DryRunSignal,
  HoldingsResponse,
  ManualOrder,
} from "../types/api";

export const api = {
  health: () => apiFetch<HealthResponse>("/api/health"),

  portfolio: () => apiFetch<PortfolioState>("/api/portfolio"),

  positions: () => apiFetch<Trade[]>("/api/positions"),

  tradesToday: () => apiFetch<Trade[]>("/api/trades/today"),

  holdings: () => apiFetch<HoldingsResponse>("/api/holdings"),

  placeOrder: (order: ManualOrder) =>
    apiFetch<{ success: boolean; order_id?: string; error?: string }>("/api/orders", {
      method: "POST",
      body: JSON.stringify(order),
    }),

  trades: (params?: {
    start?: string;
    end?: string;
    symbol?: string;
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    if (params?.start) q.set("start", params.start);
    if (params?.end) q.set("end", params.end);
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return apiFetch<Trade[]>(`/api/trades${qs ? "?" + qs : ""}`);
  },

  tradeDetail: (tradeId: string) =>
    apiFetch<TradeDetail>(`/api/trades/${tradeId}`),

  equityCurve: (days = 30) =>
    apiFetch<EquityCurvePoint[]>(`/api/equity-curve?days=${days}`),

  watchlist: () => apiFetch<WatchlistItem[]>("/api/watchlist"),

  userWatchlist: () => apiFetch<UserWatchlistItem[]>("/api/user-watchlist"),

  addUserWatchlistSymbol: (data: { symbol: string; sector?: string; notes?: string }) =>
    apiFetch<ActionResult>("/api/user-watchlist", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  removeUserWatchlistSymbol: (symbol: string) =>
    apiFetch<ActionResult>(`/api/user-watchlist/${symbol}`, { method: "DELETE" }),

  sectors: () => apiFetch<SectorRotation>("/api/sectors"),

  scoreboard: (groupType?: string) => {
    const qs = groupType ? `?group_type=${groupType}` : "";
    return apiFetch<ScoreboardEntry[]>(`/api/predictions/scoreboard${qs}`);
  },

  reports: (params?: {
    report_type?: string;
    start?: string;
    end?: string;
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    if (params?.report_type) q.set("report_type", params.report_type);
    if (params?.start) q.set("start", params.start);
    if (params?.end) q.set("end", params.end);
    if (params?.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return apiFetch<Report[]>(`/api/reports${qs ? "?" + qs : ""}`);
  },

  slippage: (params?: { symbol?: string; days?: number }) => {
    const q = new URLSearchParams();
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.days) q.set("days", String(params.days));
    const qs = q.toString();
    return apiFetch<SlippageStats>(`/api/slippage${qs ? "?" + qs : ""}`);
  },

  llmAccuracy: (days = 30) =>
    apiFetch<LLMAccuracy>(`/api/llm-accuracy?days=${days}`),

  audit: (params?: { limit?: number; action_type?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.action_type) q.set("action_type", params.action_type);
    const qs = q.toString();
    return apiFetch<AuditEntry[]>(`/api/audit${qs ? "?" + qs : ""}`);
  },

  serverLogs: (lines = 200) =>
    apiFetch<{ lines: string[]; total: number }>(`/api/logs?lines=${lines}`),

  integrations: () => apiFetch<IntegrationsStatus>("/api/integrations"),

  pingGemini: () =>
    apiFetch<ActionResult>("/api/integrations/gemini/ping", { method: "POST" }),

  authenticateZerodha: (requestToken: string) =>
    apiFetch<ActionResult>("/api/integrations/zerodha/authenticate", {
      method: "POST",
      body: JSON.stringify({ request_token: requestToken }),
    }),

  testTelegram: () =>
    apiFetch<ActionResult>("/api/integrations/telegram/test", { method: "POST" }),

  sendTelegram: (message: string) =>
    apiFetch<ActionResult>("/api/integrations/telegram/send", {
      method: "POST",
      body: JSON.stringify({ message }),
    }),

  // --- New endpoints ---

  economicCalendar: (params?: { days?: number; country?: string; event_type?: string }) => {
    const q = new URLSearchParams();
    if (params?.days) q.set("days", String(params.days));
    if (params?.country) q.set("country", params.country);
    if (params?.event_type) q.set("event_type", params.event_type);
    const qs = q.toString();
    return apiFetch<EconomicEvent[]>(`/api/economic-calendar${qs ? "?" + qs : ""}`);
  },

  earnings: (params?: { symbol?: string; days?: number }) => {
    const q = new URLSearchParams();
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.days) q.set("days", String(params.days));
    const qs = q.toString();
    return apiFetch<EarningsEvent[]>(`/api/earnings${qs ? "?" + qs : ""}`);
  },

  news: (params?: { symbol?: string; source?: string; date_from?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.source) q.set("source", params.source);
    if (params?.date_from) q.set("date_from", params.date_from);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return apiFetch<NewsArticle[]>(`/api/news${qs ? "?" + qs : ""}`);
  },

  sentiment: (symbol: string) =>
    apiFetch<SentimentResult>(`/api/sentiment/${symbol}`),

  mlModels: () => apiFetch<MLModelsResponse>("/api/ml-models"),

  promoteModel: (modelType: string, version: string) =>
    apiFetch<{ promoted: boolean }>(`/api/ml-models/${modelType}/${version}/promote`, {
      method: "POST",
    }),

  deleteModel: (modelType: string, version: string) =>
    apiFetch<{ db_deleted: boolean; file_deleted: boolean }>(`/api/ml-models/${modelType}/${version}`, {
      method: "DELETE",
    }),

  reshadowModel: (modelType: string, version: string) =>
    apiFetch<{ reshadowed: boolean }>(`/api/ml-models/${modelType}/${version}/reshadow`, {
      method: "POST",
    }),

  retireModel: (modelType: string, version: string) =>
    apiFetch<{ retired: boolean }>(`/api/ml-models/${modelType}/${version}/retire`, {
      method: "POST",
    }),

  shadowComparison: (modelType: string) =>
    apiFetch<{ shadow: Record<string, number>; production: Record<string, number> }>(`/api/ml-models/${modelType}/shadow-comparison`),

  predictionsToday: () =>
    apiFetch<PredictionDetail[]>("/api/predictions/today"),

  predictionsUnscored: () =>
    apiFetch<PredictionDetail[]>("/api/predictions/unscored"),

  predictionOutcomes: () =>
    apiFetch<PredictionDetail[]>("/api/predictions/outcomes"),

  weeklyTrades: () => apiFetch<Trade[]>("/api/weekly/trades"),

  weeklyPredictions: () =>
    apiFetch<PredictionDetail[]>("/api/weekly/predictions"),

  weeklyLLMReviews: () =>
    apiFetch<WeeklyLLMReview[]>("/api/weekly/llm-reviews"),

  riskExposure: () => apiFetch<RiskExposure>("/api/risk-exposure"),

  nseUniverse: () => apiFetch<NSESymbol[]>("/api/nse-universe"),

  premarket: () => apiFetch<PremarketData>("/api/premarket"),

  systemState: () => apiFetch<SystemState>("/api/system-state"),

  // Feature #3: Symbol deep-dive
  symbolOHLCV: (symbol: string, params?: { days?: number; interval?: string }) => {
    const q = new URLSearchParams();
    if (params?.days) q.set("days", String(params.days));
    if (params?.interval) q.set("interval", params.interval);
    const qs = q.toString();
    return apiFetch<OHLCVBar[]>(`/api/symbol/${symbol}/ohlcv${qs ? "?" + qs : ""}`);
  },

  symbolTrades: (symbol: string, limit = 50) =>
    apiFetch<Trade[]>(`/api/symbol/${symbol}/trades?limit=${limit}`),

  symbolPredictions: (symbol: string) =>
    apiFetch<PredictionDetail[]>(`/api/symbol/${symbol}/predictions`),

  // Feature #5
  strategyPerformance: () =>
    apiFetch<StrategyPerformance>("/api/strategy-performance"),

  // Feature #8
  executionQuality: (days = 30) =>
    apiFetch<ExecutionQuality>(`/api/execution-quality?days=${days}`),

  // Feature #7
  correlations: (days = 60) =>
    apiFetch<CorrelationData>(`/api/correlations?days=${days}`),

  // Feature #4
  alerts: (activeOnly = true) =>
    apiFetch<PriceAlert[]>(`/api/alerts?active_only=${activeOnly}`),

  createAlert: (data: { symbol: string; target_price: number; direction: string; note?: string }) =>
    apiFetch<ActionResult>("/api/alerts", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  deleteAlert: (id: number) =>
    apiFetch<ActionResult>(`/api/alerts/${id}`, { method: "DELETE" }),

  // Feature #6
  riskSimulator: (params: Partial<RiskSimParams>) =>
    apiFetch<RiskSimResult>("/api/risk-simulator", {
      method: "POST",
      body: JSON.stringify(params),
    }),

  // Data Management
  storageStats: () => apiFetch<StorageStats>("/api/storage-stats"),

  cleanupTable: (data: { table: string; older_than_days: number }) =>
    apiFetch<CleanupResult>(`/api/cleanup?table=${data.table}&older_than_days=${data.older_than_days}`, {
      method: "POST",
    }),

  createBackup: () => apiFetch<BackupResult>("/api/backup", { method: "POST" }),

  listBackups: () => apiFetch<BackupEntry[]>("/api/backups"),

  universeSymbols: () => apiFetch<string[]>("/api/universe-symbols"),

  restoreBackup: (filename: string) =>
    apiFetch<{ success: boolean; db_restored: boolean; models_restored?: number }>(
      `/api/restore/${filename}`,
      { method: "POST" },
    ),

  changePassword: (newPassword: string) =>
    apiFetch<{ success: boolean }>("/api/change-password", {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    }),

  updateCapital: (amount: number) =>
    apiFetch<{ success: boolean; initial_capital: number }>("/api/capital", {
      method: "POST",
      body: JSON.stringify({ amount }),
    }),

  syncCapital: () =>
    apiFetch<{ success: boolean; initial_capital?: number; error?: string }>("/api/capital/sync", {
      method: "POST",
    }),

  resetAllData: () => apiFetch<ResetResult>("/api/reset", { method: "POST" }),

  // Pending Trades (manual approval)
  pendingTrades: () =>
    apiFetch<{ id: number; symbol: string; signal_type: string; entry_price: number; target_price: number; stop_loss_price: number; position_size: number; confidence_score: number; product: string; created_at: string }[]>("/api/pending-trades"),

  approvePendingTrade: (tradeId: number) =>
    apiFetch<{ success: boolean; trade?: Record<string, unknown> }>(`/api/pending-trades/${tradeId}/approve`, { method: "POST" }),

  rejectPendingTrade: (tradeId: number) =>
    apiFetch<{ success: boolean }>(`/api/pending-trades/${tradeId}/reject`, { method: "POST" }),

  // Dry-Run Signal Preview
  runDryRun: () => apiFetch<DryRunResult>("/api/dry-run", { method: "POST" }),

  dryRunHistory: (limit = 10) =>
    apiFetch<DryRunSummary[]>(`/api/dry-run/history?limit=${limit}`),

  dryRunDetail: (runId: string) =>
    apiFetch<DryRunSignal[]>(`/api/dry-run/${runId}`),

  scoreDryRun: (runId: string) =>
    apiFetch<{ scored: number; not_found: number }>(`/api/dry-run/${runId}/score`, { method: "POST" }),

  deleteDryRun: (runId: string) =>
    apiFetch<{ success: boolean; deleted: number }>(`/api/dry-run/${runId}`, { method: "DELETE" }),

  quarantinedSymbols: () =>
    apiFetch<{ symbol: string; consecutive_failures: number; last_error: string; quarantined_at: string; updated_at: string }[]>("/api/quarantined-symbols"),

  unquarantineSymbol: (symbol: string) =>
    apiFetch<{ success: boolean; symbol: string }>(`/api/quarantined-symbols/${symbol}`, { method: "DELETE" }),

  listSkills: () =>
    apiFetch<{ name: string; description: string; trigger: string; schedule: string | null }[]>(
      "/api/skills",
    ),

  runSkill: (skillName: string) =>
    apiFetch<{ success: boolean; skill: string; data: Record<string, unknown>; error: string | null }>(
      `/api/skills/${skillName}/run`,
      { method: "POST" },
    ),
};
