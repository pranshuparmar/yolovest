import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, lazy, Suspense } from "react";
import { AuthProvider, useAuth } from "./hooks/useAuth";
import { ThemeProvider } from "./hooks/useTheme";
import { setAuthHeader, setCsrfToken, setOnUnauthorized } from "./api/client";
import { Layout } from "./components/Layout";
import { LoginPage } from "./pages/LoginPage";

// Eager: landing + the few pages users hit on every session. Keeping
// them in the main chunk avoids a flash-of-spinner right after login.
import { DashboardPage } from "./pages/DashboardPage";
import { PositionsPage } from "./pages/PositionsPage";

// Everything else lazy-loaded so the initial bundle doesn't ship code
// for pages that may never be opened in a session (Reports, Audit,
// EconomicCalendar, DryRun, RiskSimulator, etc.). Each becomes its
// own JS chunk Vite emits at build time.
const TradesPage = lazy(() => import("./pages/TradesPage").then(m => ({ default: m.TradesPage })));
const TradeDetailPage = lazy(() => import("./pages/TradeDetailPage").then(m => ({ default: m.TradeDetailPage })));
const WatchlistPage = lazy(() => import("./pages/WatchlistPage").then(m => ({ default: m.WatchlistPage })));
const HoldingsPage = lazy(() => import("./pages/HoldingsPage").then(m => ({ default: m.HoldingsPage })));
const SymbolPage = lazy(() => import("./pages/SymbolPage").then(m => ({ default: m.SymbolPage })));
const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage").then(m => ({ default: m.AnalyticsPage })));
const ReportsPage = lazy(() => import("./pages/ReportsPage").then(m => ({ default: m.ReportsPage })));
const AuditPage = lazy(() => import("./pages/AuditPage").then(m => ({ default: m.AuditPage })));
const IntegrationsPage = lazy(() => import("./pages/IntegrationsPage").then(m => ({ default: m.IntegrationsPage })));
const EconomicCalendarPage = lazy(() => import("./pages/EconomicCalendarPage").then(m => ({ default: m.EconomicCalendarPage })));
const NewsFeedPage = lazy(() => import("./pages/NewsFeedPage").then(m => ({ default: m.NewsFeedPage })));
const MLModelsPage = lazy(() => import("./pages/MLModelsPage").then(m => ({ default: m.MLModelsPage })));
const PredictionsPage = lazy(() => import("./pages/PredictionsPage").then(m => ({ default: m.PredictionsPage })));
const WeeklySummaryPage = lazy(() => import("./pages/WeeklySummaryPage").then(m => ({ default: m.WeeklySummaryPage })));
const StrategyPerformancePage = lazy(() => import("./pages/StrategyPerformancePage").then(m => ({ default: m.StrategyPerformancePage })));
const AlertsPage = lazy(() => import("./pages/AlertsPage").then(m => ({ default: m.AlertsPage })));
const RiskSimulatorPage = lazy(() => import("./pages/RiskSimulatorPage").then(m => ({ default: m.RiskSimulatorPage })));
const CorrelationPage = lazy(() => import("./pages/CorrelationPage").then(m => ({ default: m.CorrelationPage })));
const ExecutionQualityPage = lazy(() => import("./pages/ExecutionQualityPage").then(m => ({ default: m.ExecutionQualityPage })));
const ModelDriftPage = lazy(() => import("./pages/ModelDriftPage").then(m => ({ default: m.ModelDriftPage })));
const InstitutionalFlowsPage = lazy(() => import("./pages/InstitutionalFlowsPage").then(m => ({ default: m.InstitutionalFlowsPage })));
const DataManagementPage = lazy(() => import("./pages/DataManagementPage").then(m => ({ default: m.DataManagementPage })));
const DryRunPage = lazy(() => import("./pages/DryRunPage").then(m => ({ default: m.DryRunPage })));
const SkillsPage = lazy(() => import("./pages/SkillsPage").then(m => ({ default: m.SkillsPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));

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
        <Route
          path="*"
          element={
            <Suspense fallback={<PageFallback />}>
              <LazyRoutes />
            </Suspense>
          }
        />
      </Route>
    </Routes>
  );
}

function PageFallback() {
  return <div className="h-40 animate-pulse bg-gray-900/40 rounded-lg" />;
}

function LazyRoutes() {
  return (
    <Routes>
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
