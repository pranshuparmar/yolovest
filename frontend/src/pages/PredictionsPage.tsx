import { useState } from "react";
import {
  usePredictionsToday,
  usePredictionsUnscored,
  usePredictionOutcomes,
  useScoreboard,
  useRunSkill,
} from "../hooks/queries";
import { ScoreboardTable } from "../components/ScoreboardTable";
import clsx from "clsx";
import type { PredictionDetail } from "../types/api";

function fmt(n: number | null | undefined, d = 2) {
  if (n == null) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function PredictionRow({ p }: { p: PredictionDetail }) {
  const [expanded, setExpanded] = useState(false);
  const dirOk = p.direction_correct;
  const tgtHit = p.target_hit;

  return (
    <div className="border-b border-gray-800/50 last:border-0">
      <div
        className="flex items-center gap-4 py-2.5 px-2 hover:bg-gray-800/30 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-sm font-medium text-emerald-400 w-24 shrink-0">
          {p.symbol || "—"}
        </span>
        <span className="text-xs text-gray-400 w-16">
          {p.signal_type || "—"}
        </span>
        <span className="text-xs text-gray-400 w-20">
          Conf: {fmt(p.confidence_score ? p.confidence_score * 100 : null, 1)}%
        </span>
        <span className="flex-1" />
        {dirOk !== null && (
          <span
            className={clsx(
              "px-1.5 py-0.5 rounded text-xs font-medium",
              dirOk
                ? "bg-emerald-900/40 text-emerald-400"
                : "bg-red-900/40 text-red-400"
            )}
          >
            {dirOk ? "Direction OK" : "Direction Wrong"}
          </span>
        )}
        {tgtHit !== null && (
          <span
            className={clsx(
              "px-1.5 py-0.5 rounded text-xs font-medium",
              tgtHit
                ? "bg-emerald-900/40 text-emerald-400"
                : "bg-amber-900/40 text-amber-400"
            )}
          >
            {tgtHit ? "Target Hit" : "Target Missed"}
          </span>
        )}
        {p.actual_pnl_pct != null && (
          <span
            className={clsx(
              "text-sm font-medium w-20 text-right",
              p.actual_pnl_pct >= 0 ? "text-emerald-400" : "text-red-400"
            )}
          >
            {p.actual_pnl_pct >= 0 ? "+" : ""}
            {fmt(p.actual_pnl_pct)}%
          </span>
        )}
        <span className="text-xs text-gray-500 w-6">
          {expanded ? "▲" : "▼"}
        </span>
      </div>
      {expanded && (
        <div className="px-4 pb-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <div>
            <span className="text-gray-500">Prediction ID</span>
            <p className="text-gray-300 font-mono text-xs mt-0.5">
              {p.prediction_id}
            </p>
          </div>
          <div>
            <span className="text-gray-500">Trade ID</span>
            <p className="text-gray-300 font-mono text-xs mt-0.5">
              {p.trade_id}
            </p>
          </div>
          <div>
            <span className="text-gray-500">Created</span>
            <p className="text-gray-300 mt-0.5">
              {new Date(p.created_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })}
            </p>
          </div>
          <div>
            <span className="text-gray-500">End Time</span>
            <p className="text-gray-300 mt-0.5">
              {p.prediction_end_time
                ? new Date(p.prediction_end_time).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })
                : "—"}
            </p>
          </div>
          {p.actual_price != null && (
            <div>
              <span className="text-gray-500">Actual Price</span>
              <p className="text-gray-300 mt-0.5">{fmt(p.actual_price)}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function PredictionsPage() {
  const [tab, setTab] = useState<"today" | "unscored" | "outcomes">("today");
  const [sbGroup, setSbGroup] = useState<string | undefined>();

  const { data: today, isLoading: todayLoading } = usePredictionsToday();
  const { data: unscored, isLoading: unscoredLoading } =
    usePredictionsUnscored();
  const { data: outcomes, isLoading: outcomesLoading } =
    usePredictionOutcomes();
  const { data: scoreboard, isLoading: sbLoading } = useScoreboard(sbGroup);
  const runSkill = useRunSkill();

  const tabData: Record<string, { items: PredictionDetail[] | undefined; loading: boolean }> = {
    today: { items: today, loading: todayLoading },
    unscored: { items: unscored, loading: unscoredLoading },
    outcomes: { items: outcomes, loading: outcomesLoading },
  };

  const current = tabData[tab]!;

  // Compute summary stats for outcomes
  const outcomeStats = (outcomes || []).reduce(
    (acc, p) => {
      acc.total++;
      if (p.direction_correct) acc.dirCorrect++;
      if (p.target_hit) acc.tgtHit++;
      if (p.actual_pnl_pct != null) {
        acc.totalPnl += p.actual_pnl_pct;
        acc.pnlCount++;
      }
      return acc;
    },
    { total: 0, dirCorrect: 0, tgtHit: 0, totalPnl: 0, pnlCount: 0 }
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Predictions</h2>
        <button
          onClick={() => runSkill.mutate("predict-track")}
          disabled={runSkill.isPending}
          className="px-4 py-2 rounded text-sm font-medium bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-50 transition-colors"
        >
          {runSkill.isPending ? "Scoring..." : "Score Predictions"}
        </button>
      </div>
      {runSkill.isSuccess && (
        <div className="bg-emerald-900/20 border border-emerald-800 rounded-lg p-3 text-sm text-emerald-400">
          Prediction scoring complete. Refresh the page to see updated results.
        </div>
      )}
      {runSkill.isError && (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-sm text-red-400">
          Scoring failed. Check server logs for details.
        </div>
      )}

      {/* Summary cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 md:grid-cols-5 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Today</p>
          <p className="text-xl font-semibold">{today?.length || 0}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Awaiting Score</p>
          <p className="text-xl font-semibold text-amber-400">
            {unscored?.length || 0}
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Direction Accuracy</p>
          <p
            className={clsx(
              "text-xl font-semibold",
              outcomeStats.total > 0 &&
                outcomeStats.dirCorrect / outcomeStats.total > 0.5
                ? "text-emerald-400"
                : "text-red-400"
            )}
          >
            {outcomeStats.total > 0
              ? fmt((outcomeStats.dirCorrect / outcomeStats.total) * 100, 1)
              : "—"}
            %
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Target Hit Rate</p>
          <p className="text-xl font-semibold text-blue-400">
            {outcomeStats.total > 0
              ? fmt((outcomeStats.tgtHit / outcomeStats.total) * 100, 1)
              : "—"}
            %
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Avg PnL %</p>
          <p
            className={clsx(
              "text-xl font-semibold",
              outcomeStats.totalPnl >= 0 ? "text-emerald-400" : "text-red-400"
            )}
          >
            {outcomeStats.pnlCount > 0
              ? `${outcomeStats.totalPnl >= 0 ? "+" : ""}${fmt(outcomeStats.totalPnl / outcomeStats.pnlCount)}`
              : "—"}
            %
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg">
        <div className="flex border-b border-gray-800">
          {(
            [
              ["today", "Today's Predictions"],
              ["unscored", "Awaiting Score"],
              ["outcomes", "Scored Outcomes"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={clsx(
                "px-4 py-2.5 text-sm font-medium border-b-2 transition-colors",
                tab === key
                  ? "text-emerald-400 border-emerald-400"
                  : "text-gray-500 border-transparent hover:text-gray-300"
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="p-4">
          {current.loading ? (
            <div className="h-40 animate-pulse bg-gray-800 rounded" />
          ) : !current.items || current.items.length === 0 ? (
            <p className="text-gray-500 text-sm py-4">No predictions</p>
          ) : (
            <div>
              {current.items.map((p) => (
                <PredictionRow key={p.prediction_id} p={p} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Scoreboard */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-gray-400">
            Prediction Scoreboard
          </h3>
          <select
            value={sbGroup || ""}
            onChange={(e) => setSbGroup(e.target.value || undefined)}
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
