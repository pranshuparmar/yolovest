import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/endpoints";

const STALE_30S = 30_000;

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function usePortfolio() {
  return useQuery({
    queryKey: ["portfolio"],
    queryFn: api.portfolio,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function usePositions() {
  return useQuery({
    queryKey: ["positions"],
    queryFn: api.positions,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function useTradesToday() {
  return useQuery({
    queryKey: ["trades", "today"],
    queryFn: api.tradesToday,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function useTrades(params?: {
  start?: string;
  end?: string;
  symbol?: string;
  limit?: number;
}) {
  return useQuery({
    queryKey: ["trades", params],
    queryFn: () => api.trades(params),
    staleTime: STALE_30S,
  });
}

export function useTradeDetail(tradeId: string) {
  return useQuery({
    queryKey: ["trade", tradeId],
    queryFn: () => api.tradeDetail(tradeId),
    enabled: !!tradeId,
  });
}

export function useEquityCurve(days = 30) {
  return useQuery({
    queryKey: ["equity-curve", days],
    queryFn: () => api.equityCurve(days),
    staleTime: 60_000,
  });
}

export function useWatchlist() {
  return useQuery({
    queryKey: ["watchlist"],
    queryFn: api.watchlist,
    staleTime: 60_000,
  });
}

export function useAddWatchlistSymbol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ symbol, sector }: { symbol: string; sector?: string }) =>
      api.addWatchlistSymbol(symbol, sector),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
  });
}

export function useRemoveWatchlistSymbol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (symbol: string) => api.removeWatchlistSymbol(symbol),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlist"] }),
  });
}

export function useSectors() {
  return useQuery({
    queryKey: ["sectors"],
    queryFn: api.sectors,
    staleTime: 60_000,
  });
}

export function useScoreboard(groupType?: string) {
  return useQuery({
    queryKey: ["scoreboard", groupType],
    queryFn: () => api.scoreboard(groupType),
    staleTime: 60_000,
  });
}

export function useReports(params?: {
  report_type?: string;
  start?: string;
  end?: string;
  limit?: number;
}) {
  return useQuery({
    queryKey: ["reports", params],
    queryFn: () => api.reports(params),
    staleTime: 60_000,
  });
}

export function useSlippage(params?: { symbol?: string; days?: number }) {
  return useQuery({
    queryKey: ["slippage", params],
    queryFn: () => api.slippage(params),
    staleTime: 60_000,
  });
}

export function useLLMAccuracy(days = 30) {
  return useQuery({
    queryKey: ["llm-accuracy", days],
    queryFn: () => api.llmAccuracy(days),
    staleTime: 60_000,
  });
}

export function useAudit(params?: { limit?: number; action_type?: string }) {
  return useQuery({
    queryKey: ["audit", params],
    queryFn: () => api.audit(params),
    staleTime: STALE_30S,
  });
}

export function useIntegrations() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: api.integrations,
    staleTime: STALE_30S,
  });
}

export function usePingGemini() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.pingGemini,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
}

export function useAuthenticateZerodha() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) => api.authenticateZerodha(token),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
}

export function useTestTelegram() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.testTelegram,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
}

export function useSendTelegram() {
  return useMutation({
    mutationFn: (message: string) => api.sendTelegram(message),
  });
}

// --- New hooks ---

export function useEconomicCalendar(params?: { days?: number; country?: string; event_type?: string }) {
  return useQuery({
    queryKey: ["economic-calendar", params],
    queryFn: () => api.economicCalendar(params),
    staleTime: 60_000,
  });
}

export function useEarnings(params?: { symbol?: string; days?: number }) {
  return useQuery({
    queryKey: ["earnings", params],
    queryFn: () => api.earnings(params),
    staleTime: 60_000,
  });
}

export function useNews(params?: { symbol?: string; limit?: number }) {
  return useQuery({
    queryKey: ["news", params],
    queryFn: () => api.news(params),
    staleTime: STALE_30S,
  });
}

export function useSentiment(symbol: string) {
  return useQuery({
    queryKey: ["sentiment", symbol],
    queryFn: () => api.sentiment(symbol),
    enabled: !!symbol,
    staleTime: 60_000,
  });
}

export function useMLModels() {
  return useQuery({
    queryKey: ["ml-models"],
    queryFn: api.mlModels,
    staleTime: 60_000,
  });
}

export function usePredictionsToday() {
  return useQuery({
    queryKey: ["predictions", "today"],
    queryFn: api.predictionsToday,
    staleTime: STALE_30S,
  });
}

export function usePredictionsUnscored() {
  return useQuery({
    queryKey: ["predictions", "unscored"],
    queryFn: api.predictionsUnscored,
    staleTime: STALE_30S,
  });
}

export function usePredictionOutcomes() {
  return useQuery({
    queryKey: ["predictions", "outcomes"],
    queryFn: api.predictionOutcomes,
    staleTime: 60_000,
  });
}

export function useWeeklyTrades() {
  return useQuery({
    queryKey: ["weekly", "trades"],
    queryFn: api.weeklyTrades,
    staleTime: 60_000,
  });
}

export function useWeeklyPredictions() {
  return useQuery({
    queryKey: ["weekly", "predictions"],
    queryFn: api.weeklyPredictions,
    staleTime: 60_000,
  });
}

export function useWeeklyLLMReviews() {
  return useQuery({
    queryKey: ["weekly", "llm-reviews"],
    queryFn: api.weeklyLLMReviews,
    staleTime: 60_000,
  });
}

export function useRiskExposure() {
  return useQuery({
    queryKey: ["risk-exposure"],
    queryFn: api.riskExposure,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function useNSEUniverse() {
  return useQuery({
    queryKey: ["nse-universe"],
    queryFn: api.nseUniverse,
    staleTime: 300_000,
  });
}

export function usePremarket() {
  return useQuery({
    queryKey: ["premarket"],
    queryFn: api.premarket,
    staleTime: 60_000,
  });
}

export function useSystemState() {
  return useQuery({
    queryKey: ["system-state"],
    queryFn: api.systemState,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}
