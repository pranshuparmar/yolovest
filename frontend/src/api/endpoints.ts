import { apiFetch, apiDownload, apiUpload } from "./client";
import { expectObject, expectArray } from "./validate";
import type {
  HealthResponse,
  PortfolioState,
  Trade,
  TradeDetail,
  TradeOrderDetail,
  EquityCurvePoint,
  PnlCalendarDay,
  WatchlistItem,
  SectorRotation,
  ScoreboardEntry,
  SlippageStats,
  LLMAccuracy,
  Recommendation,
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
  PaginatedPredictions,
  RiskExposure,
  RiskGates,
  PremarketData,
  SystemState,
  NSESymbol,
  WeeklyLLMReview,
  OHLCVBar,
  StrategyPerformance,
  ExecutionQuality,
  ModelDrift,
  SignalClassDistribution,
  InstitutionalFlows,
  SymbolContext,
  SymbolQuickContext,
  RotationCooldown,
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
  ConfigSections,
  ConfigUpdateResult,
  HolidaysResponse,
} from "../types/api";

export const api = {
  health: () => apiFetch<HealthResponse>("/api/health"),

  portfolio: () => apiFetch<PortfolioState>("/api/portfolio", undefined, expectObject),

  fundsHistory: (days = 90) =>
    apiFetch<{
      count: number;
      snapshots: Array<{
        snapshot_date: string;
        captured_at: string;
        mode: string;
        available_cash: number;
        utilised_margin: number;
        net: number;
        holdings_invested: number;
        holdings_current: number;
        m2m_realised: number;
        m2m_unrealised: number;
        live_balance: number;
      }>;
    }>(`/api/funds/history?days=${days}`),

  funds: () =>
    apiFetch<{
      authenticated: boolean;
      enabled?: boolean;
      raw: Record<string, unknown> | null;
      summary: {
        available_cash: number;
        live_balance: number;
        opening_balance: number;
        adhoc_margin?: number;
        intraday_payin?: number;
        collateral: number;
        utilised_margin: number;
        m2m_unrealised: number;
        m2m_realised: number;
        payout: number;
        exposure: number;
        span: number;
        delivery: number;
        option_premium?: number;
        turnover?: number;
        net: number;
      };
    }>("/api/funds"),

  positions: () => apiFetch<Trade[]>("/api/positions", undefined, expectArray),

  closePosition: (tradeId: string, qty?: number) =>
    apiFetch<{
      status: string;
      trade_id: string;
      exit_order_id: string;
      // Full close shape
      exit_price?: number;
      pnl?: number;
      // Partial close shape
      exit_qty?: number;
      remaining_qty?: number;
      partial_pnl?: number;
    }>(
      qty
        ? `/api/positions/${encodeURIComponent(tradeId)}/close?qty=${qty}`
        : `/api/positions/${encodeURIComponent(tradeId)}/close`,
      { method: "POST" },
    ),

  tightenSl: (tradeId: string, newSl: number) =>
    apiFetch<{ ok: boolean; trade_id: string; symbol: string; previous_sl: number; new_sl: number; path: string }>(
      `/api/positions/${encodeURIComponent(tradeId)}/tighten-sl`,
      { method: "POST", body: JSON.stringify({ new_sl: newSl }) },
    ),

  modifyTarget: (tradeId: string, newTarget: number) =>
    apiFetch<{ ok: boolean; trade_id: string; symbol: string; previous_target: number; new_target: number; path: string }>(
      `/api/positions/${encodeURIComponent(tradeId)}/modify-target`,
      { method: "POST", body: JSON.stringify({ new_target: newTarget }) },
    ),

  brokerOrders: () =>
    apiFetch<{
      authenticated: boolean;
      orders: Record<string, unknown>[];
      gtts: Record<string, unknown>[];
      error?: string;
    }>("/api/broker/orders"),

  cancelBrokerOrder: (orderId: string) =>
    apiFetch<{ ok: boolean; order_id: string }>(
      `/api/broker/orders/${encodeURIComponent(orderId)}/cancel`,
      { method: "POST" },
    ),

  modifyBrokerOrder: (
    orderId: string,
    body: { price?: number; quantity?: number; trigger_price?: number; order_type?: string },
  ) =>
    apiFetch<{ ok: boolean; order_id: string }>(
      `/api/broker/orders/${encodeURIComponent(orderId)}/modify`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  cancelBrokerGtt: (gttId: number) =>
    apiFetch<{ ok: boolean; gtt_id: number }>(
      `/api/broker/gtts/${gttId}/cancel`,
      { method: "POST" },
    ),

  tradesToday: () => apiFetch<Trade[]>("/api/trades/today"),

  holdings: () => apiFetch<HoldingsResponse>("/api/holdings", undefined, expectObject),

  placeOrder: (order: ManualOrder) =>
    apiFetch<{
      success: boolean;
      order_id?: string;
      error?: string;
      // Populated when the broker rejected the order because of a
      // missing CDSL TPIN authorisation. The UI renders an
      // "Authorize at CDSL" action button that opens auth_url.
      error_type?: string;
      auth_url?: string;
      auth_url_static?: boolean;
      ddpi_help_url?: string;
      hint?: string;
    }>("/api/orders", {
      method: "POST",
      body: JSON.stringify(order),
    }),

  initiateHoldingsAuth: () =>
    apiFetch<{
      success: boolean;
      error_type?: string;
      auth_url?: string;
      auth_url_static?: boolean;
      ddpi_help_url?: string;
      hint?: string;
    }>("/api/broker/holdings-auth", { method: "POST", body: "{}" }),

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

  tradeOrderDetail: (tradeId: string) =>
    apiFetch<TradeOrderDetail>(`/api/trades/${tradeId}/order-detail`),

  deleteTrade: (tradeId: string) =>
    apiFetch<{ success: boolean; trade_id: string }>(`/api/trades/${tradeId}`, { method: "DELETE" }),

  equityCurve: (days = 30) =>
    apiFetch<EquityCurvePoint[]>(`/api/equity-curve?days=${days}`),

  pnlCalendar: (days = 90) =>
    apiFetch<PnlCalendarDay[]>(`/api/pnl-calendar?days=${days}`),

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

  recommendations: () => apiFetch<Recommendation[]>("/api/recommendations"),

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

  logoutZerodha: () =>
    apiFetch<{ success: boolean }>("/api/integrations/zerodha/logout", {
      method: "POST",
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

  news: (params?: { symbol?: string; source?: string; date_from?: string; date_to?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.source) q.set("source", params.source);
    if (params?.date_from) q.set("date_from", params.date_from);
    if (params?.date_to) q.set("date_to", params.date_to);
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

  // Cross-machine model transfer (train on a big box, import here)
  downloadModel: (version: string) =>
    apiDownload(`/api/ml-models/${encodeURIComponent(version)}/download`, `${version}.pkl`),

  uploadModel: (file: File) =>
    apiUpload<{ success: boolean; version: string; filename: string; size_bytes: number; metrics: Record<string, unknown> }>(
      "/api/ml-models/upload",
      file,
    ),

  importModel: (data: { model_type: string; version: string; promote: boolean; force?: boolean }) =>
    apiFetch<{ imported: boolean; model_type: string; version: string; promoted: boolean; hot_reloaded: boolean; metrics: Record<string, unknown>; warnings: string[] }>(
      "/api/ml-models/import",
      { method: "POST", body: JSON.stringify(data) },
    ),

  shadowComparison: (modelType: string) =>
    apiFetch<{ shadow: Record<string, number>; production: Record<string, number> }>(`/api/ml-models/${modelType}/shadow-comparison`),

  predictionsToday: (params?: { limit?: number; offset?: number; symbol?: string; direction?: string; model?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.direction) q.set("direction", params.direction);
    if (params?.model) q.set("model", params.model);
    const qs = q.toString();
    return apiFetch<PaginatedPredictions>(`/api/predictions/today${qs ? `?${qs}` : ""}`);
  },

  predictionsUnscored: (params?: { limit?: number; offset?: number; symbol?: string; direction?: string; model?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.direction) q.set("direction", params.direction);
    if (params?.model) q.set("model", params.model);
    const qs = q.toString();
    return apiFetch<PaginatedPredictions>(`/api/predictions/unscored${qs ? `?${qs}` : ""}`);
  },

  predictionOutcomes: (params?: { limit?: number; offset?: number; symbol?: string; direction?: string; direction_correct?: number; target_hit?: number; model?: string; min_confidence?: number }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.direction) q.set("direction", params.direction);
    if (params?.direction_correct != null) q.set("direction_correct", String(params.direction_correct));
    if (params?.target_hit != null) q.set("target_hit", String(params.target_hit));
    if (params?.model) q.set("model", params.model);
    if (params?.min_confidence != null) q.set("min_confidence", String(params.min_confidence));
    const qs = q.toString();
    return apiFetch<PaginatedPredictions>(`/api/predictions/outcomes${qs ? `?${qs}` : ""}`);
  },

  weeklyTrades: () => apiFetch<Trade[]>("/api/weekly/trades"),

  weeklyPredictions: () =>
    apiFetch<PredictionDetail[]>("/api/weekly/predictions"),

  weeklyLLMReviews: () =>
    apiFetch<WeeklyLLMReview[]>("/api/weekly/llm-reviews"),

  riskExposure: () => apiFetch<RiskExposure>("/api/risk-exposure"),

  riskGates: () => apiFetch<RiskGates>("/api/risk-gates"),

  clearDriftSuspension: () =>
    apiFetch<{ success: boolean; suspended: boolean }>("/api/drift-suspension", {
      method: "DELETE",
    }),

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

  ltpBatch: (symbols: string[]) => {
    const qs = encodeURIComponent(symbols.join(","));
    return apiFetch<Record<string, number>>(`/api/ltp?symbols=${qs}`);
  },

  symbolPredictions: (symbol: string) =>
    apiFetch<PredictionDetail[]>(`/api/symbol/${symbol}/predictions`),

  symbolContext: (symbol: string) =>
    apiFetch<SymbolContext>(`/api/symbol/${symbol}/context`),

  symbolQuickContext: (symbol: string) =>
    apiFetch<SymbolQuickContext>(`/api/symbol/${symbol}/quick-context`),

  recentTradedSymbols: (limit = 10) =>
    apiFetch<string[]>(`/api/trades/recent-symbols?limit=${limit}`),

  rotationCooldown: () =>
    apiFetch<RotationCooldown>("/api/rotation-cooldown"),

  clearRotationCooldown: (symbol?: string) => {
    const qs = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
    return apiFetch<{ success: boolean; cleared: number; symbol: string | null }>(
      `/api/rotation-cooldown/clear${qs}`,
      { method: "POST" },
    );
  },

  // Feature #5
  strategyPerformance: () =>
    apiFetch<StrategyPerformance>("/api/strategy-performance"),

  // Feature #8
  executionQuality: (days = 30) =>
    apiFetch<ExecutionQuality>(`/api/execution-quality?days=${days}`),

  modelDrift: (days = 30) =>
    apiFetch<ModelDrift>(`/api/model-drift?days=${days}`),

  signalClassDistribution: (days = 7) =>
    apiFetch<SignalClassDistribution>(
      `/api/signal-class-distribution?days=${days}`,
    ),

  institutionalFlows: (params?: {
    days?: number; bulk_limit?: number; symbol?: string;
  }) => {
    const q = new URLSearchParams();
    if (params?.days) q.set("days", String(params.days));
    if (params?.bulk_limit) q.set("bulk_limit", String(params.bulk_limit));
    if (params?.symbol) q.set("symbol", params.symbol);
    const qs = q.toString();
    return apiFetch<InstitutionalFlows>(
      `/api/institutional-flows${qs ? "?" + qs : ""}`,
    );
  },

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

  deleteBackup: (filename: string) =>
    apiFetch<{ success: boolean; filename: string; size_bytes: number }>(
      `/api/backups/${filename}`,
      { method: "DELETE" },
    ),

  setBackupLock: (filename: string, locked: boolean) =>
    apiFetch<{ success: boolean; filename: string; locked: boolean }>(
      `/api/backups/${filename}/${locked ? "lock" : "unlock"}`,
      { method: "POST" },
    ),

  downloadBackup: (filename: string) =>
    apiDownload(`/api/backups/${encodeURIComponent(filename)}/download`, filename),

  uploadBackup: (file: File) =>
    apiUpload<{ success: boolean; filename: string; size_bytes: number }>(
      "/api/backups/upload",
      file,
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
    apiFetch<{ id: number; symbol: string; signal_type: string; entry_price: number; target_price: number; stop_loss_price: number; position_size: number; confidence_score: number; product: string; created_at: string; expected_holding_days?: number | null; expected_holding_period?: string | null; is_override?: boolean; is_manual?: boolean }[]>("/api/pending-trades"),

  approvePendingTrade: (tradeId: number, overrides?: Record<string, unknown>) =>
    apiFetch<{ success: boolean; trade?: Record<string, unknown> }>(`/api/pending-trades/${tradeId}/approve`, {
      method: "POST",
      body: JSON.stringify(overrides ? { overrides } : {}),
    }),

  rejectPendingTrade: (tradeId: number) =>
    apiFetch<{ success: boolean }>(`/api/pending-trades/${tradeId}/reject`, { method: "POST" }),

  clearTodaysSignals: () =>
    apiFetch<{ success: boolean; signals_deleted: number; pending_deleted: number }>("/api/clear-signals", { method: "POST" }),

  killSwitch: (command: "pause" | "stop" | "kill" | "resume") =>
    apiFetch<{ success: boolean; command: string; data: Record<string, unknown>; error: string | null }>(
      `/api/kill-switch/${command}`,
      { method: "POST" },
    ),

  bulkDelete: (group: string) =>
    apiFetch<{ success: boolean; group: string; deleted: Record<string, number>; total: number }>(
      `/api/bulk-delete/${group}`, { method: "POST" },
    ),

  manualTrade: (trade: { symbol: string; signal_type: string; entry_price: number; target_price: number; stop_loss_price: number; product?: string; position_size?: number }) =>
    apiFetch<{ success: boolean; trade?: Record<string, unknown>; error?: string | null }>("/api/manual-trade", {
      method: "POST",
      body: JSON.stringify(trade),
    }),

  reloadConfig: () =>
    apiFetch<{ status: string; reloaded: string[] }>("/api/config/reload", { method: "POST" }),

  // Dry-Run Signal Preview
  runDryRun: (mode?: string, asOf?: string, modelVersion?: string) => {
    const params = new URLSearchParams();
    if (mode) params.set("mode", mode);
    if (asOf) params.set("as_of", asOf);
    if (modelVersion) params.set("model_version", modelVersion);
    const qs = params.toString();
    return apiFetch<DryRunResult>(
      `/api/dry-run${qs ? `?${qs}` : ""}`,
      { method: "POST" },
    );
  },

  dryRunHistory: (limit = 10) =>
    apiFetch<DryRunSummary[]>(`/api/dry-run/history?limit=${limit}`),

  dryRunDetail: (runId: string) =>
    apiFetch<DryRunSignal[]>(`/api/dry-run/${runId}`),

  scoreDryRun: (runId: string) =>
    apiFetch<{ scored: number; not_found: number; pending?: number; already_scored?: number; message?: string }>(`/api/dry-run/${runId}/score`, { method: "POST" }),

  deleteDryRun: (runId: string) =>
    apiFetch<{ success: boolean; deleted: number }>(`/api/dry-run/${runId}`, { method: "DELETE" }),

  quarantinedSymbols: () =>
    apiFetch<{ symbol: string; consecutive_failures: number; last_error: string; quarantined_at: string; updated_at: string; replacement_symbol: string | null }[]>("/api/quarantined-symbols"),

  unquarantineSymbol: (symbol: string) =>
    apiFetch<{ success: boolean; symbol: string }>(`/api/quarantined-symbols/${symbol}`, { method: "DELETE" }),

  bulkUnquarantineSymbols: (symbols: string[]) =>
    apiFetch<{ success: boolean; removed: number; results: Record<string, boolean> }>(
      "/api/quarantined-symbols/bulk-unblock",
      { method: "POST", body: JSON.stringify({ symbols }) },
    ),

  setReplacementSymbol: (symbol: string, replacement: string | null) =>
    apiFetch<{ success: boolean; symbol: string; replacement: string | null }>(
      `/api/quarantined-symbols/${symbol}/replacement`,
      { method: "PUT", body: JSON.stringify({ replacement }) },
    ),

  lockHolding: (symbol: string) =>
    apiFetch<{ success: boolean; symbol: string; locked: boolean }>(`/api/locked-holdings/${symbol}`, { method: "POST" }),

  unlockHolding: (symbol: string) =>
    apiFetch<{ success: boolean; symbol: string; locked: boolean }>(`/api/locked-holdings/${symbol}`, { method: "DELETE" }),

  bulkLockHoldings: (symbols: string[], action: "lock" | "unlock", notes?: string) =>
    apiFetch<{ success: boolean; action: string; results: Record<string, string> }>(
      "/api/locked-holdings/bulk",
      { method: "POST", body: JSON.stringify({ symbols, action, notes }) },
    ),

  reviewHoldings: (symbols?: string[]) =>
    apiFetch<{ recommendations: { symbol: string; held: boolean; quantity: number; average_price: number; last_price: number; pnl_pct: number; action: string; confidence: number; signal_type: string; reasoning: string; target_price?: number; stop_loss_price?: number; trade_id?: string | null; current_sl?: number; trade_signal_type?: string | null; entry_price?: number; day_change_pct?: number | null; week_change_pct?: number | null; vol_ratio?: number | null; target_pct?: number | null; sl_pct?: number | null; rsi?: number | null }[] }>(
      "/api/review",
      { method: "POST", body: JSON.stringify(symbols ? { symbols } : {}) },
    ),

  listSkills: () =>
    apiFetch<{ name: string; description: string; trigger: string; schedule: string | null; enabled: boolean | null; next_run: string | null }[]>(
      "/api/skills",
    ),

  runSkill: (skillName: string) =>
    apiFetch<{ success: boolean; skill: string; data: Record<string, unknown>; error: string | null }>(
      `/api/skills/${skillName}/run`,
      { method: "POST" },
    ),

  setScheduleEnabled: (skillName: string, enabled: boolean) =>
    apiFetch<{ success: boolean; skill: string; enabled: boolean }>(
      `/api/skills/${skillName}/schedule`,
      { method: "POST", body: JSON.stringify({ enabled }) },
    ),

  // Holidays
  holidays: () => apiFetch<HolidaysResponse>("/api/holidays"),

  addHoliday: (data: { date: string; early_close?: string }) =>
    apiFetch<{ success: boolean; date: string; early_close?: string }>("/api/holidays", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  removeHoliday: (date: string) =>
    apiFetch<{ success: boolean; date: string }>(`/api/holidays/${date}`, { method: "DELETE" }),

  // Config (UI-editable settings)
  getConfig: () => apiFetch<ConfigSections>("/api/config"),

  getConfigDefaults: () => apiFetch<ConfigSections>("/api/config/defaults"),

  updateConfig: (updates: Record<string, unknown>) =>
    apiFetch<ConfigUpdateResult>("/api/config", {
      method: "PUT",
      body: JSON.stringify({ updates }),
    }),

  exportConfig: () =>
    apiDownload("/api/config/export", "yolovest_config.json"),

  importConfig: (file: File) =>
    apiUpload<{ success: boolean; imported: number; sections: ConfigSections }>(
      "/api/config/import",
      file,
    ),
};
