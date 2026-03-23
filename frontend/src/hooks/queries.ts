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
