import { useState } from "react";
import { ScoreboardTable } from "../components/ScoreboardTable";
import { SlippageChart } from "../components/SlippageChart";
import { LLMAccuracyCard } from "../components/LLMAccuracyCard";
import { PnlCalendarHeatmap } from "../components/PnlCalendarHeatmap";
import { useScoreboard, useSlippage, useLLMAccuracy, usePnlCalendar } from "../hooks/queries";

export function AnalyticsPage() {
  const [groupType, setGroupType] = useState<string | undefined>(undefined);
  const [days, setDays] = useState(30);

  const { data: scoreboard, isLoading: sbLoading } = useScoreboard(groupType);
  const { data: slippage, isLoading: slLoading } = useSlippage({ days });
  const { data: llmAcc, isLoading: llmLoading } = useLLMAccuracy(days);
  const { data: pnlCalendar } = usePnlCalendar(days);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Analytics</h2>
        <div className="flex items-center gap-2">
          <label className="text-xs text-gray-500">Period:</label>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
          >
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
            <option value={365}>1 year</option>
          </select>
        </div>
      </div>

      {/* PnL Calendar Heatmap */}
      {pnlCalendar && pnlCalendar.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">PnL Calendar</h3>
          <PnlCalendarHeatmap data={pnlCalendar} months={days <= 30 ? 1 : days <= 90 ? 3 : days <= 180 ? 6 : 12} />
        </div>
      )}

      {/* LLM Accuracy */}
      {llmLoading ? (
        <div className="h-40 animate-pulse bg-gray-900 rounded-lg" />
      ) : llmAcc ? (
        <LLMAccuracyCard data={llmAcc} />
      ) : null}

      {/* Slippage */}
      {slLoading ? (
        <div className="h-40 animate-pulse bg-gray-900 rounded-lg" />
      ) : slippage ? (
        <SlippageChart data={slippage} />
      ) : null}

      {/* Prediction Scoreboard */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-gray-400">
            Prediction Scoreboard
          </h3>
          <select
            value={groupType || ""}
            onChange={(e) => setGroupType(e.target.value || undefined)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-100"
          >
            <option value="">All</option>
            <option value="symbol">By Symbol</option>
            <option value="model">By Model</option>
            <option value="timeframe">By Timeframe</option>
            <option value="overall">Overall</option>
          </select>
        </div>
        {sbLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : (
          <ScoreboardTable entries={scoreboard || []} />
        )}
      </div>
    </div>
  );
}
