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
} from "../types/api";

export const api = {
  health: () => apiFetch<HealthResponse>("/api/health"),

  portfolio: () => apiFetch<PortfolioState>("/api/portfolio"),

  positions: () => apiFetch<Trade[]>("/api/positions"),

  tradesToday: () => apiFetch<Trade[]>("/api/trades/today"),

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

  news: (params?: { symbol?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.symbol) q.set("symbol", params.symbol);
    if (params?.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return apiFetch<NewsArticle[]>(`/api/news${qs ? "?" + qs : ""}`);
  },

  sentiment: (symbol: string) =>
    apiFetch<SentimentResult>(`/api/sentiment/${symbol}`),

  mlModels: () => apiFetch<MLModelsResponse>("/api/ml-models"),

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
};
