import { useState } from "react";
import { PortfolioCards } from "../components/PortfolioCards";
import { EquityChart } from "../components/EquityChart";
import { TradesTable } from "../components/TradesTable";
import { RiskExposureChart } from "../components/RiskExposureChart";
import { RiskGatesPanel } from "../components/RiskGatesPanel";
import { EconomicCalendarWidget } from "../components/EconomicCalendarWidget";
import { PremarketCard } from "../components/PremarketCard";
import { PendingTradesBanner, ClearSignalsButton } from "../components/PendingTradesBanner";
import { RecommendationsPanel } from "../components/RecommendationsPanel";
import { KillSwitchControl } from "../components/KillSwitchControl";
import { useTradesToday, useSystemState } from "../hooks/queries";
import { CdslAuthBanner } from "../components/CdslAuthBanner";

export function DashboardPage() {
  const { data: todaysTrades, isLoading } = useTradesToday();
  const { data: systemState } = useSystemState();
  const [degradedDismissed, setDegradedDismissed] = useState(
    () => sessionStorage.getItem("yv_degraded_dismissed") === "1"
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold">Dashboard</h2>
          {systemState?.kill_switch_active && (
            <span
              className="px-3 py-1 rounded text-xs font-bold bg-red-900/60 text-red-400 animate-pulse"
              title={
                systemState.kill_switch_mode === "pause"
                  ? "Soft pause — new trades blocked, broker untouched."
                  : systemState.kill_switch_mode === "stop"
                    ? "Stop — pending orders cancelled, positions still open."
                    : systemState.kill_switch_mode === "kill"
                      ? "Kill — every position squared off, trading paused."
                      : "Kill switch is active."
              }
            >
              {systemState.kill_switch_mode === "pause"
                ? "PAUSED (NEW TRADES BLOCKED)"
                : systemState.kill_switch_mode === "stop"
                  ? "STOPPED (ORDERS CANCELLED)"
                  : systemState.kill_switch_mode === "kill"
                    ? "KILLED (ALL SQUARED OFF)"
                    : "KILL SWITCH ACTIVE"}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <KillSwitchControl />
          <ClearSignalsButton />
        </div>
      </div>

      <PendingTradesBanner />

      {/* Degraded mode banner */}
      {systemState?.is_degraded && systemState?.show_degraded_banner !== false && !degradedDismissed && (
        <div className="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 relative">
          <button
            onClick={() => {
              sessionStorage.setItem("yv_degraded_dismissed", "1");
              setDegradedDismissed(true);
            }}
            className="absolute top-2 right-2 text-yellow-600 hover:text-yellow-400 text-lg leading-none px-1"
            aria-label="Dismiss"
          >
            x
          </button>
          <div className="flex items-center gap-2 mb-2">
            <span className="text-yellow-400 font-semibold text-sm">Degraded Mode</span>
            {(systemState.auto_approved_today ?? 0) > 0 && (
              <span className="px-2 py-0.5 rounded text-xs bg-yellow-800/60 text-yellow-300">
                {systemState.auto_approved_today} auto-approved today
              </span>
            )}
          </div>
          <div className="space-y-1">
            {systemState.degraded_features?.map((f) => (
              <div key={f.feature} className="flex items-start gap-2 text-xs">
                <span className="text-yellow-500 shrink-0 mt-0.5">
                  {f.status === "disabled" ? "[OFF]" : "[!]"}
                </span>
                <span className="text-gray-300">
                  <span className="font-medium text-yellow-300">{f.feature}:</span>{" "}
                  {f.impact}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <CdslAuthBanner />

      <PortfolioCards />

      {/* Pre-market + Calendar row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <PremarketCard />
        <EconomicCalendarWidget />
      </div>

      <RecommendationsPanel />

      <EquityChart days={30} />

      {/* Opt-in risk-gate status (drift suspension / beta cap / earnings) */}
      <RiskGatesPanel />

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
