import { useState, useMemo } from "react";
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

const PAGE_SIZE = 25;

function fmt(n: number | null | undefined, d = 2) {
  if (n == null) return "\u2014";
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
          {p.symbol || "\u2014"}
        </span>
        <span className="text-xs text-gray-400 w-16">
          {p.signal_type || "\u2014"}
        </span>
        <span className="text-xs text-gray-400 w-20">
          Conf: {fmt(p.confidence_score ? p.confidence_score * 100 : null, 1)}%
        </span>
        {p.model_version && (
          <span className="text-xs text-gray-600 truncate max-w-[120px]" title={p.model_version}>
            {p.model_version}
          </span>
        )}
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
          {expanded ? "\u25B2" : "\u25BC"}
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
                : "\u2014"}
            </p>
          </div>
          {p.actual_price != null && (
            <div>
              <span className="text-gray-500">Actual Price</span>
              <p className="text-gray-300 mt-0.5">{fmt(p.actual_price)}</p>
            </div>
          )}
          {p.model_version && (
            <div>
              <span className="text-gray-500">Model</span>
              <p className="text-gray-300 font-mono text-xs mt-0.5">{p.model_version}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Pagination({
  total,
  page,
  pageSize,
  onPageChange,
}: {
  total: number;
  page: number;
  pageSize: number;
  onPageChange: (p: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (totalPages <= 1) return null;
  return (
    <div className="flex items-center justify-between pt-3 border-t border-gray-800">
      <span className="text-xs text-gray-500">
        {total} prediction{total !== 1 ? "s" : ""} | Page {page + 1} of {totalPages}
      </span>
      <div className="flex gap-1">
        <button
          onClick={() => onPageChange(page - 1)}
          disabled={page === 0}
          className="px-2 py-1 text-xs rounded bg-gray-800 text-gray-300 disabled:opacity-30 hover:bg-gray-700 transition-colors"
        >
          Prev
        </button>
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages - 1}
          className="px-2 py-1 text-xs rounded bg-gray-800 text-gray-300 disabled:opacity-30 hover:bg-gray-700 transition-colors"
        >
          Next
        </button>
      </div>
    </div>
  );
}

const selectCls =
  "bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-emerald-500";
const inputCls =
  "bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 w-24 focus:outline-none focus:border-emerald-500";

export function PredictionsPage() {
  const [tab, setTab] = useState<"today" | "unscored" | "outcomes">("today");
  const [sbGroup, setSbGroup] = useState<string | undefined>();

  // Filters
  const [filterSymbol, setFilterSymbol] = useState("");
  const [filterDirection, setFilterDirection] = useState("");
  const [filterDirCorrect, setFilterDirCorrect] = useState("");
  const [filterTgtHit, setFilterTgtHit] = useState("");
  const [filterModel, setFilterModel] = useState("");

  // Pagination per tab
  const [pageToday, setPageToday] = useState(0);
  const [pageUnscored, setPageUnscored] = useState(0);
  const [pageOutcomes, setPageOutcomes] = useState(0);

  // Reset page on filter change
  const resetPages = () => { setPageToday(0); setPageUnscored(0); setPageOutcomes(0); };

  // Build params
  const baseParams = useMemo(() => ({
    symbol: filterSymbol || undefined,
    direction: filterDirection || undefined,
    model: filterModel || undefined,
  }), [filterSymbol, filterDirection, filterModel]);

  const outcomeParams = useMemo(() => ({
    ...baseParams,
    direction_correct: filterDirCorrect !== "" ? Number(filterDirCorrect) : undefined,
    target_hit: filterTgtHit !== "" ? Number(filterTgtHit) : undefined,
  }), [baseParams, filterDirCorrect, filterTgtHit]);

  const { data: todayData, isLoading: todayLoading } = usePredictionsToday({
    ...baseParams, limit: PAGE_SIZE, offset: pageToday * PAGE_SIZE,
  });
  const { data: unscoredData, isLoading: unscoredLoading } = usePredictionsUnscored({
    ...baseParams, limit: PAGE_SIZE, offset: pageUnscored * PAGE_SIZE,
  });
  const { data: outcomesData, isLoading: outcomesLoading } = usePredictionOutcomes({
    ...outcomeParams, limit: PAGE_SIZE, offset: pageOutcomes * PAGE_SIZE,
  });
  const { data: scoreboard, isLoading: sbLoading } = useScoreboard(sbGroup);
  const runSkill = useRunSkill();

  // Unwrap paginated responses
  const today = todayData?.items;
  const unscored = unscoredData?.items;
  const outcomes = outcomesData?.items;

  const tabMeta: Record<string, {
    items: PredictionDetail[] | undefined;
    loading: boolean;
    total: number;
    page: number;
    setPage: (p: number) => void;
  }> = {
    today: { items: today, loading: todayLoading, total: todayData?.total ?? 0, page: pageToday, setPage: setPageToday },
    unscored: { items: unscored, loading: unscoredLoading, total: unscoredData?.total ?? 0, page: pageUnscored, setPage: setPageUnscored },
    outcomes: { items: outcomes, loading: outcomesLoading, total: outcomesData?.total ?? 0, page: pageOutcomes, setPage: setPageOutcomes },
  };

  const current = tabMeta[tab]!;

  // Summary from outcomes total (use backend total, not just current page)
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
          Prediction scoring complete. Results will update automatically.
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
          <p className="text-xl font-semibold">{todayData?.total ?? 0}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Awaiting Score</p>
          <p className="text-xl font-semibold text-amber-400">
            {unscoredData?.total ?? 0}
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
              : "\u2014"}
            %
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Target Hit Rate</p>
          <p className="text-xl font-semibold text-blue-400">
            {outcomeStats.total > 0
              ? fmt((outcomeStats.tgtHit / outcomeStats.total) * 100, 1)
              : "\u2014"}
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
              : "\u2014"}
            %
          </p>
        </div>
      </div>

      {/* Filters */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-xs text-gray-500 font-medium">Filters:</span>
          <input
            type="text"
            placeholder="Symbol"
            value={filterSymbol}
            onChange={(e) => { setFilterSymbol(e.target.value.toUpperCase()); resetPages(); }}
            className={inputCls}
          />
          <select
            value={filterDirection}
            onChange={(e) => { setFilterDirection(e.target.value); resetPages(); }}
            className={selectCls}
          >
            <option value="">All Directions</option>
            <option value="BUY">BUY</option>
            <option value="SELL">SELL</option>
          </select>
          <input
            type="text"
            placeholder="Model version"
            value={filterModel}
            onChange={(e) => { setFilterModel(e.target.value); resetPages(); }}
            className={clsx(inputCls, "w-40")}
          />
          {tab === "outcomes" && (
            <>
              <select
                value={filterDirCorrect}
                onChange={(e) => { setFilterDirCorrect(e.target.value); resetPages(); }}
                className={selectCls}
              >
                <option value="">Direction: Any</option>
                <option value="1">Correct</option>
                <option value="0">Wrong</option>
              </select>
              <select
                value={filterTgtHit}
                onChange={(e) => { setFilterTgtHit(e.target.value); resetPages(); }}
                className={selectCls}
              >
                <option value="">Target: Any</option>
                <option value="1">Hit</option>
                <option value="0">Missed</option>
              </select>
            </>
          )}
          {(filterSymbol || filterDirection || filterModel || filterDirCorrect || filterTgtHit) && (
            <button
              onClick={() => {
                setFilterSymbol(""); setFilterDirection(""); setFilterModel("");
                setFilterDirCorrect(""); setFilterTgtHit(""); resetPages();
              }}
              className="text-xs text-gray-400 hover:text-gray-200 underline"
            >
              Clear all
            </button>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg">
        <div className="flex border-b border-gray-800">
          {(
            [
              ["today", `Today (${todayData?.total ?? 0})`],
              ["unscored", `Awaiting Score (${unscoredData?.total ?? 0})`],
              ["outcomes", `Scored (${outcomesData?.total ?? 0})`],
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
              <Pagination
                total={current.total}
                page={current.page}
                pageSize={PAGE_SIZE}
                onPageChange={current.setPage}
              />
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
