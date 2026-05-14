import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { AuthProvider, useAuth } from "./hooks/useAuth";
import { ThemeProvider } from "./hooks/useTheme";
import { setAuthHeader, setCsrfToken, setOnUnauthorized } from "./api/client";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";
import { DashboardPage } from "./pages/DashboardPage";
import { PositionsPage } from "./pages/PositionsPage";
import { TradesPage } from "./pages/TradesPage";
import { TradeDetailPage } from "./pages/TradeDetailPage";
import { WatchlistPage } from "./pages/WatchlistPage";
import { HoldingsPage } from "./pages/HoldingsPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { ReportsPage } from "./pages/ReportsPage";
import { AuditPage } from "./pages/AuditPage";
import { IntegrationsPage } from "./pages/IntegrationsPage";
import { EconomicCalendarPage } from "./pages/EconomicCalendarPage";
import { NewsFeedPage } from "./pages/NewsFeedPage";
import { MLModelsPage } from "./pages/MLModelsPage";
import { PredictionsPage } from "./pages/PredictionsPage";
import { WeeklySummaryPage } from "./pages/WeeklySummaryPage";
import { SymbolPage } from "./pages/SymbolPage";
import { StrategyPerformancePage } from "./pages/StrategyPerformancePage";
import { AlertsPage } from "./pages/AlertsPage";
import { RiskSimulatorPage } from "./pages/RiskSimulatorPage";
import { CorrelationPage } from "./pages/CorrelationPage";
import { ExecutionQualityPage } from "./pages/ExecutionQualityPage";
import { ModelDriftPage } from "./pages/ModelDriftPage";
import { InstitutionalFlowsPage } from "./pages/InstitutionalFlowsPage";
import { DataManagementPage } from "./pages/DataManagementPage";
import { DryRunPage } from "./pages/DryRunPage";
import { SkillsPage } from "./pages/SkillsPage";
import SettingsPage from "./pages/SettingsPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function AuthSync() {
  const { authHeader, csrfToken, logout, isAuthenticated } = useAuth();

  useEffect(() => {
    setAuthHeader(authHeader);
    setCsrfToken(csrfToken);
    setOnUnauthorized(logout);
  }, [authHeader, csrfToken, logout]);

  // WebSocket is now handled by NotificationCenter in Layout
  if (!isAuthenticated) {
    return null;
  }

  return null;
}

function AppRoutes() {
  const { isAuthenticated } = useAuth();

  if (!isAuthenticated) {
    return <LoginPage />;
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/positions" element={<PositionsPage />} />
        <Route path="/trades" element={<TradesPage />} />
        <Route path="/trades/:tradeId" element={<TradeDetailPage />} />
        <Route path="/watchlist" element={<WatchlistPage />} />
        <Route path="/holdings" element={<HoldingsPage />} />
        <Route path="/news" element={<NewsFeedPage />} />
        <Route path="/calendar" element={<EconomicCalendarPage />} />
        <Route path="/predictions" element={<PredictionsPage />} />
        <Route path="/ml-models" element={<MLModelsPage />} />
        <Route path="/symbol/:symbol" element={<SymbolPage />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/strategy" element={<StrategyPerformancePage />} />
        <Route path="/execution" element={<ExecutionQualityPage />} />
        <Route path="/model-drift" element={<ModelDriftPage />} />
        <Route path="/institutional-flows" element={<InstitutionalFlowsPage />} />
        <Route path="/correlations" element={<CorrelationPage />} />
        <Route path="/risk-sim" element={<RiskSimulatorPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/weekly" element={<WeeklySummaryPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/audit" element={<AuditPage />} />
        <Route path="/dry-run" element={<DryRunPage />} />
        <Route path="/data" element={<DataManagementPage />} />
        <Route path="/skills" element={<SkillsPage />} />
        <Route path="/integrations" element={<IntegrationsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ThemeProvider>
          <BrowserRouter>
            <AuthSync />
            <AppRoutes />
          </BrowserRouter>
        </ThemeProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
