import { PortfolioCards } from "../components/PortfolioCards";
import { EquityChart } from "../components/EquityChart";
import { TradesTable } from "../components/TradesTable";
import { RiskExposureChart } from "../components/RiskExposureChart";
import { EconomicCalendarWidget } from "../components/EconomicCalendarWidget";
import { PremarketCard } from "../components/PremarketCard";
import { PendingTradesBanner } from "../components/PendingTradesBanner";
import { useTradesToday, useSystemState } from "../hooks/queries";

export function DashboardPage() {
  const { data: todaysTrades, isLoading } = useTradesToday();
  const { data: systemState } = useSystemState();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Dashboard</h2>
        {systemState?.kill_switch_active && (
          <span className="px-3 py-1 rounded text-xs font-bold bg-red-900/60 text-red-400 animate-pulse">
            KILL SWITCH ACTIVE
          </span>
        )}
      </div>

      <PendingTradesBanner />

      <PortfolioCards />

      {/* Pre-market + Calendar row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <PremarketCard />
        <EconomicCalendarWidget />
      </div>

      <EquityChart days={30} />

      {/* Risk exposure */}
      <RiskExposureChart />

      {/* Today's trades */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Today's Trades
        </h3>
        {isLoading ? (
          <div className="h-20 animate-pulse bg-gray-800 rounded" />
        ) : (
          <TradesTable trades={todaysTrades || []} compact />
        )}
      </div>
    </div>
  );
}
