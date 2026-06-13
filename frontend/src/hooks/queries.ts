import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/endpoints";
import { initTimezone } from "../utils/datetime";

const STALE_30S = 30_000;

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const data = await api.health();
      // Initialize display timezone from server config on first fetch
      if (data.timezone) initTimezone(data.timezone);
      return data;
    },
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

export function useFunds() {
  return useQuery({
    queryKey: ["funds"],
    queryFn: api.funds,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function useFundsHistory(days = 90) {
  return useQuery({
    queryKey: ["funds-history", days],
    queryFn: () => api.fundsHistory(days),
    // Daily snapshot — once an hour is plenty.
    staleTime: 60 * 60 * 1000,
    refetchInterval: 60 * 60 * 1000,
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

export function useClosePosition() {
  const qc = useQueryClient();
  return useMutation({
    // `qty` omitted = full close (legacy behaviour); pass a smaller
    // number to book a partial close. Caller is responsible for
    // validating qty <= current position quantity.
    mutationFn: ({ tradeId, qty }: { tradeId: string; qty?: number }) =>
      api.closePosition(tradeId, qty),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["trades", "today"] });
      qc.invalidateQueries({ queryKey: ["portfolio"] });
      qc.invalidateQueries({ queryKey: ["system-state"] });
      qc.invalidateQueries({ queryKey: ["recommendations"] });
    },
  });
}

export function useTightenSl() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ tradeId, newSl }: { tradeId: string; newSl: number }) =>
      api.tightenSl(tradeId, newSl),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["recommendations"] });
    },
  });
}

export function useModifyTarget() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ tradeId, newTarget }: { tradeId: string; newTarget: number }) =>
      api.modifyTarget(tradeId, newTarget),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["recommendations"] });
      qc.invalidateQueries({ queryKey: ["broker-orders"] });
    },
  });
}

export function useBrokerOrders() {
  return useQuery({
    queryKey: ["broker-orders"],
    queryFn: api.brokerOrders,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function useCancelBrokerOrder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (orderId: string) => api.cancelBrokerOrder(orderId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broker-orders"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
    },
  });
}

export function useModifyBrokerOrder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      orderId, ...body
    }: {
      orderId: string;
      price?: number;
      quantity?: number;
      trigger_price?: number;
      order_type?: string;
    }) => api.modifyBrokerOrder(orderId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broker-orders"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
    },
  });
}

export function useCancelBrokerGtt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (gttId: number) => api.cancelBrokerGtt(gttId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broker-orders"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
    },
  });
}

export function usePnlCalendar(days = 90) {
  return useQuery({
    queryKey: ["pnl-calendar", days],
    queryFn: () => api.pnlCalendar(days),
    staleTime: 60_000,
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

export function useDeleteTrade() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.deleteTrade,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["portfolio"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
    },
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

export function useHoldings() {
  return useQuery({
    queryKey: ["holdings"],
    queryFn: api.holdings,
    staleTime: STALE_30S,
  });
}

export function usePlaceOrder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.placeOrder,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["holdings"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["portfolio"] });
    },
  });
}

export function useLockHolding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.lockHolding,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["holdings"] }),
  });
}

export function useUnlockHolding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.unlockHolding,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["holdings"] }),
  });
}

export function useReviewHoldings() {
  return useMutation({
    mutationFn: (symbols?: string[]) => api.reviewHoldings(symbols),
  });
}

export function useBulkLockHoldings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ symbols, action, notes }: { symbols: string[]; action: "lock" | "unlock"; notes?: string }) =>
      api.bulkLockHoldings(symbols, action, notes),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["holdings"] }),
  });
}

export function useTradeDetail(tradeId: string) {
  return useQuery({
    queryKey: ["trade", tradeId],
    queryFn: () => api.tradeDetail(tradeId),
    enabled: !!tradeId,
  });
}

export function useTradeOrderDetail(tradeId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["trade", tradeId, "order-detail"],
    queryFn: () => api.tradeOrderDetail(tradeId),
    enabled: !!tradeId && enabled,
    // Live broker call — don't aggressively refetch.
    staleTime: 30_000,
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

export function useUserWatchlist() {
  return useQuery({
    queryKey: ["user-watchlist"],
    queryFn: api.userWatchlist,
    staleTime: 60_000,
  });
}

export function useAddUserWatchlistSymbol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.addUserWatchlistSymbol,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["user-watchlist"] }),
  });
}

export function useRemoveUserWatchlistSymbol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (symbol: string) => api.removeUserWatchlistSymbol(symbol),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["user-watchlist"] }),
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

export function useRecommendations() {
  return useQuery({
    queryKey: ["recommendations"],
    queryFn: () => api.recommendations(),
    refetchInterval: 60_000,
    staleTime: 30_000,
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

export function useServerLogs(lines = 200) {
  return useQuery({
    queryKey: ["server-logs", lines],
    queryFn: () => api.serverLogs(lines),
    refetchInterval: 5000,
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

export function useLogoutZerodha() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.logoutZerodha,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }),
  });
}

export function useReloadConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.reloadConfig,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["system-state"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
    },
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

const NEWS_PAGE_SIZE = 50;

export function useNewsInfinite(params?: { symbol?: string; source?: string; date_from?: string; date_to?: string }) {
  return useInfiniteQuery({
    queryKey: ["news-infinite", params],
    queryFn: ({ pageParam = 0 }) =>
      api.news({ symbol: params?.symbol, source: params?.source, date_from: params?.date_from, date_to: params?.date_to, limit: NEWS_PAGE_SIZE, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, _allPages, lastPageParam) =>
      lastPage.length < NEWS_PAGE_SIZE ? undefined : lastPageParam + NEWS_PAGE_SIZE,
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

export function usePromoteModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ modelType, version }: { modelType: string; version: string }) =>
      api.promoteModel(modelType, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ml-models"] });
    },
  });
}

export function useDeleteModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ modelType, version }: { modelType: string; version: string }) =>
      api.deleteModel(modelType, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ml-models"] });
    },
  });
}

export function useReshadowModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ modelType, version }: { modelType: string; version: string }) =>
      api.reshadowModel(modelType, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ml-models"] });
    },
  });
}

export function useRetireModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ modelType, version }: { modelType: string; version: string }) =>
      api.retireModel(modelType, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ml-models"] });
    },
  });
}

export function useShadowComparison(modelType: string | null) {
  return useQuery({
    queryKey: ["shadow-comparison", modelType],
    queryFn: () => api.shadowComparison(modelType!),
    enabled: !!modelType,
    staleTime: 60_000,
  });
}

export function usePredictionsToday(params?: { limit?: number; offset?: number; symbol?: string; direction?: string; model?: string }) {
  return useQuery({
    queryKey: ["predictions", "today", params],
    queryFn: () => api.predictionsToday(params),
    staleTime: STALE_30S,
  });
}

export function usePredictionsUnscored(params?: { limit?: number; offset?: number; symbol?: string; direction?: string; model?: string }) {
  return useQuery({
    queryKey: ["predictions", "unscored", params],
    queryFn: () => api.predictionsUnscored(params),
    staleTime: STALE_30S,
  });
}

export function usePredictionOutcomes(params?: { limit?: number; offset?: number; symbol?: string; direction?: string; direction_correct?: number; target_hit?: number; model?: string; min_confidence?: number }) {
  return useQuery({
    queryKey: ["predictions", "outcomes", params],
    queryFn: () => api.predictionOutcomes(params),
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

export function useRiskGates() {
  return useQuery({
    queryKey: ["risk-gates"],
    queryFn: api.riskGates,
    staleTime: STALE_30S,
    refetchInterval: STALE_30S,
  });
}

export function useClearDriftSuspension() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.clearDriftSuspension,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["risk-gates"] }),
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

// Feature #3: Symbol deep-dive
export function useSymbolOHLCV(symbol: string, params?: { days?: number; interval?: string }) {
  return useQuery({
    queryKey: ["symbol-ohlcv", symbol, params],
    queryFn: () => api.symbolOHLCV(symbol, params),
    enabled: !!symbol,
    staleTime: 60_000,
  });
}

export function useSymbolTrades(symbol: string) {
  return useQuery({
    queryKey: ["symbol-trades", symbol],
    queryFn: () => api.symbolTrades(symbol),
    enabled: !!symbol,
    staleTime: STALE_30S,
  });
}

export function useSymbolPredictions(symbol: string) {
  return useQuery({
    queryKey: ["symbol-predictions", symbol],
    queryFn: () => api.symbolPredictions(symbol),
    enabled: !!symbol,
    staleTime: 60_000,
  });
}

export function useSymbolContext(symbol: string) {
  return useQuery({
    queryKey: ["symbol-context", symbol],
    queryFn: () => api.symbolContext(symbol),
    enabled: !!symbol,
    staleTime: 60_000,
  });
}

export function useSymbolQuickContext(symbol: string) {
  return useQuery({
    queryKey: ["symbol-quick-context", symbol],
    queryFn: () => api.symbolQuickContext(symbol),
    enabled: !!symbol,
    staleTime: 30_000,
  });
}

export function useRecentTradedSymbols(limit = 10) {
  return useQuery({
    queryKey: ["recent-traded-symbols", limit],
    queryFn: () => api.recentTradedSymbols(limit),
    staleTime: 120_000,
  });
}

export function useRotationCooldown() {
  return useQuery({
    queryKey: ["rotation-cooldown"],
    queryFn: api.rotationCooldown,
    staleTime: 60_000,
  });
}

export function useClearRotationCooldown() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (symbol?: string) => api.clearRotationCooldown(symbol),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rotation-cooldown"] });
      qc.invalidateQueries({ queryKey: ["watchlist"] });
    },
  });
}

// Feature #5
export function useStrategyPerformance() {
  return useQuery({
    queryKey: ["strategy-performance"],
    queryFn: api.strategyPerformance,
    staleTime: 60_000,
  });
}

// Feature #8
export function useExecutionQuality(days = 30) {
  return useQuery({
    queryKey: ["execution-quality", days],
    queryFn: () => api.executionQuality(days),
    staleTime: 60_000,
  });
}

export function useModelDrift(days = 30) {
  return useQuery({
    queryKey: ["model-drift", days],
    queryFn: () => api.modelDrift(days),
    staleTime: 60_000,
  });
}

export function useSignalClassDistribution(days = 7) {
  return useQuery({
    queryKey: ["signal-class-distribution", days],
    queryFn: () => api.signalClassDistribution(days),
    staleTime: 60_000,
  });
}

export function useInstitutionalFlows(params?: {
  days?: number; bulk_limit?: number; symbol?: string;
}) {
  return useQuery({
    queryKey: ["institutional-flows", params],
    queryFn: () => api.institutionalFlows(params),
    staleTime: 60_000,
  });
}

// Feature #7
export function useCorrelations(days = 60) {
  return useQuery({
    queryKey: ["correlations", days],
    queryFn: () => api.correlations(days),
    staleTime: 60_000,
  });
}

// Feature #4
export function useAlerts(activeOnly = true) {
  return useQuery({
    queryKey: ["alerts", activeOnly],
    queryFn: () => api.alerts(activeOnly),
    staleTime: STALE_30S,
  });
}

export function useCreateAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.createAlert,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });
}

export function useDeleteAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.deleteAlert,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });
}

// Feature #6
export function useRiskSimulator() {
  return useMutation({ mutationFn: api.riskSimulator });
}

export function useUniverseSymbols() {
  return useQuery({
    queryKey: ["universe-symbols"],
    queryFn: api.universeSymbols,
    staleTime: 300_000, // 5 min — symbol list rarely changes
  });
}

// Data Management
export function useStorageStats() {
  return useQuery({
    queryKey: ["storage-stats"],
    queryFn: api.storageStats,
    staleTime: 60_000,
  });
}

export function useCleanupTable() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.cleanupTable,
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["storage-stats"] });
      // Cleanup is per-table — bust the caches that read from that
      // table so the page reflects the deletion without a hard refresh.
      const tableInvalidations: Record<string, string[][]> = {
        predictions: [
          ["predictions"], ["weekly", "predictions"], ["recommendations"],
          ["model-drift"], ["signal-class-distribution"],
        ],
        ohlcv: [["ohlcv"]],
        news_articles: [["news"], ["news-articles"]],
        economic_events: [["economic-events"], ["economic-calendar"]],
        audit_log: [["audit"], ["audit-log"]],
      };
      const keys = tableInvalidations[vars.table] || [];
      for (const key of keys) {
        qc.invalidateQueries({ queryKey: key });
      }
    },
  });
}

export function useBackups() {
  return useQuery({
    queryKey: ["backups"],
    queryFn: api.listBackups,
    staleTime: 60_000,
  });
}

export function useCreateBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.createBackup,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backups"] }),
  });
}

export function useRestoreBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (filename: string) => api.restoreBackup(filename),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["backups"] });
      qc.invalidateQueries({ queryKey: ["storage-stats"] });
      qc.invalidateQueries({ queryKey: ["ml-models"] });
    },
  });
}

export function useDeleteBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (filename: string) => api.deleteBackup(filename),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backups"] }),
  });
}

export function useSetBackupLock() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ filename, locked }: { filename: string; locked: boolean }) =>
      api.setBackupLock(filename, locked),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backups"] }),
  });
}

export function useUploadBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.uploadBackup(file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["backups"] });
      qc.invalidateQueries({ queryKey: ["storage-stats"] });
    },
  });
}

export function useUploadModel() {
  return useMutation({
    mutationFn: (file: File) => api.uploadModel(file),
  });
}

export function useImportModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { model_type: string; version: string; promote: boolean; force?: boolean }) =>
      api.importModel(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ml-models"] }),
  });
}

export function useImportConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.importConfig(file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["config-defaults"] });
    },
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (newPassword: string) => api.changePassword(newPassword),
  });
}

export function useUpdateCapital() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (amount: number) => api.updateCapital(amount),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["portfolio"] }),
  });
}

export function useSyncCapital() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.syncCapital,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["portfolio"] }),
  });
}

export function usePendingTrades() {
  return useQuery({
    queryKey: ["pending-trades"],
    queryFn: api.pendingTrades,
    staleTime: 10_000,
    refetchInterval: 10_000,
  });
}

export function useApprovePendingTrade() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ tradeId, overrides }: { tradeId: number; overrides?: Record<string, unknown> }) =>
      api.approvePendingTrade(tradeId, overrides),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pending-trades"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
    },
  });
}

export function useManualTrade() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (trade: { symbol: string; signal_type: string; entry_price: number; target_price: number; stop_loss_price: number; product?: string; position_size?: number }) =>
      api.manualTrade(trade),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
    },
  });
}

export function useRejectPendingTrade() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.rejectPendingTrade,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pending-trades"] });
    },
  });
}

export function useBulkDelete() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.bulkDelete,
    onSuccess: () => {
      qc.invalidateQueries();
    },
  });
}

export function useClearTodaysSignals() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.clearTodaysSignals,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pending-trades"] });
      qc.invalidateQueries({ queryKey: ["signals"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["system-status"] });
      qc.invalidateQueries({ queryKey: ["recommendations"] });
      qc.invalidateQueries({ queryKey: ["signal-class-distribution"] });
    },
  });
}

export function useKillSwitch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (command: "pause" | "stop" | "kill" | "resume") => api.killSwitch(command),
    onSuccess: () => {
      // Kill-switch flips system_state.kill_switch and (for "kill") closes
      // every open position + cancels pending orders, so flush everything
      // that could be downstream of those.
      qc.invalidateQueries({ queryKey: ["system-state"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["pending-trades"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
      qc.invalidateQueries({ queryKey: ["health"] });
    },
  });
}

export function useResetAllData() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.resetAllData,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["storage-stats"] });
      qc.invalidateQueries({ queryKey: ["backups"] });
    },
  });
}

// Dry-Run Signal Preview
export function useDryRunHistory() {
  return useQuery({
    queryKey: ["dry-run-history"],
    queryFn: () => api.dryRunHistory(),
    staleTime: 60_000,
  });
}

export function useDryRunDetail(runId: string | null) {
  return useQuery({
    queryKey: ["dry-run-detail", runId],
    queryFn: () => api.dryRunDetail(runId!),
    enabled: !!runId,
    staleTime: 60_000,
  });
}

export function useRunDryRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args?: { mode?: string; asOf?: string; modelVersion?: string }) =>
      api.runDryRun(args?.mode, args?.asOf, args?.modelVersion),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["dry-run-history"] });
      // A past-date run may have auto-scored; refresh its detail so the
      // actual-close / move% / net P&L columns show immediately.
      if (result?.run_id) {
        qc.invalidateQueries({ queryKey: ["dry-run-detail", result.run_id] });
      }
    },
  });
}

export function useListSkills() {
  return useQuery({
    queryKey: ["skills"],
    queryFn: api.listSkills,
    staleTime: 60_000,
  });
}

export function useRunSkill() {
  return useMutation({
    mutationFn: (skillName: string) => api.runSkill(skillName),
  });
}

export function useSetScheduleEnabled() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ skillName, enabled }: { skillName: string; enabled: boolean }) =>
      api.setScheduleEnabled(skillName, enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useScoreDryRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.scoreDryRun,
    onSuccess: (_data, runId) => {
      qc.invalidateQueries({ queryKey: ["dry-run-history"] });
      qc.invalidateQueries({ queryKey: ["dry-run-detail", runId] });
    },
  });
}

export function useDeleteDryRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.deleteDryRun,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dry-run-history"] });
    },
  });
}

export function useQuarantinedSymbols() {
  return useQuery({
    queryKey: ["quarantined-symbols"],
    queryFn: api.quarantinedSymbols,
    staleTime: 60_000,
  });
}

export function useUnquarantineSymbol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.unquarantineSymbol,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["quarantined-symbols"] });
    },
  });
}

export function useBulkUnquarantineSymbols() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (symbols: string[]) => api.bulkUnquarantineSymbols(symbols),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["quarantined-symbols"] });
    },
  });
}

export function useSetReplacementSymbol() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ symbol, replacement }: { symbol: string; replacement: string | null }) =>
      api.setReplacementSymbol(symbol, replacement),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["quarantined-symbols"] });
    },
  });
}

// Holidays
export function useHolidays() {
  return useQuery({
    queryKey: ["holidays"],
    queryFn: api.holidays,
    staleTime: 60_000,
  });
}

export function useAddHoliday() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { date: string; early_close?: string }) => api.addHoliday(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["holidays"] });
    },
  });
}

export function useRemoveHoliday() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (date: string) => api.removeHoliday(date),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["holidays"] });
    },
  });
}

// Config (UI-editable settings)
export function useConfig() {
  return useQuery({
    queryKey: ["config"],
    queryFn: api.getConfig,
    staleTime: 60_000,
  });
}

export function useConfigDefaults() {
  return useQuery({
    queryKey: ["config-defaults"],
    queryFn: api.getConfigDefaults,
    staleTime: Infinity, // defaults never change at runtime
  });
}

export function useUpdateConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (updates: Record<string, unknown>) => api.updateConfig(updates),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["system-state"] });
    },
  });
}
