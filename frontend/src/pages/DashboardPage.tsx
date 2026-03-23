import { PortfolioCards } from "../components/PortfolioCards";
import { EquityChart } from "../components/EquityChart";
import { TradesTable } from "../components/TradesTable";
import { useTradesToday } from "../hooks/queries";

export function DashboardPage() {
  const { data: todaysTrades, isLoading } = useTradesToday();

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Dashboard</h2>
      <PortfolioCards />
      <EquityChart days={30} />
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
