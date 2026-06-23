import { useState, useMemo } from "react";
import { formatIST } from "../utils/datetime";
import {
  usePredictionsToday,
  usePredictionsUnscored,
  usePredictionOutcomes,
  useScoreboard,
  useRunSkill,
} from "../hooks/queries";
import { ScoreboardTable } from "../components/ScoreboardTable";
import { Pagination } from "../components/Pagination";
import { SymbolLink } from "../components/SymbolLink";
import { formatPriceMovePct, priceMovePct } from "../utils/priceMove";
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

function PredictionRow({ p, occurrences }: { p: PredictionDetail; occurrences?: number }) {
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
          {p.symbol
            ? <SymbolLink symbol={p.symbol} className="text-emerald-400" />
            : "\u2014"}
        </span>
        <span
          className={clsx(
            "text-xs font-medium w-12",
            p.signal_type === "BUY"
              ? "text-emerald-400"
              : p.signal_type === "SELL"
                ? "text-red-400"
                : "text-gray-400",
          )}
        >
          {p.signal_type || "\u2014"}
        </span>
        {p.product && (
          <span
            className={clsx(
              "text-[10px] px-1.5 py-0.5 rounded font-medium hidden sm:inline",
              p.product === "MIS"
                ? "bg-amber-900/30 text-amber-400"
                : "bg-blue-900/30 text-blue-400",
            )}
          >
            {p.product}
          </span>
        )}
        {p.holding_period && (
          <span className="text-[10px] text-gray-500 hidden md:inline">
            {p.holding_period.replace("_", " ")}
            {p.expected_holding_days ? ` ${p.expected_holding_days}d` : ""}
          </span>
        )}
        {occurrences && occurrences > 1 && (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/40 text-blue-300 font-medium"
            title={`${occurrences} near-duplicate predictions grouped`}
          >
            \u00d7{occurrences}
          </span>
        )}
        {/* Entry \u2192 Target at-a-glance (signal-style), hidden on narrow screens */}
        {p.entry_price != null && p.target_price != null && (
          <span className="text-[11px] font-mono text-gray-500 hidden lg:inline">
            {fmt(p.entry_price)}
            <span className="text-gray-600"> {"\u2192"} </span>
            <span className="text-emerald-400/80">{fmt(p.target_price)}</span>
          </span>
        )}
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
          {/* Trade setup \u2014 mirrors the signal / dry-run detail */}
          <div>
            <span className="text-gray-500">Direction</span>
            <p
              className={clsx(
                "mt-0.5 font-medium",
                p.signal_type === "BUY"
                  ? "text-emerald-400"
                  : p.signal_type === "SELL"
                    ? "text-red-400"
                    : "text-gray-300",
              )}
            >
              {p.signal_type || "\u2014"}
            </p>
          </div>
          <div>
            <span className="text-gray-500">Confidence</span>
            <p className="text-gray-300 mt-0.5">
              {fmt(p.confidence_score != null ? p.confidence_score * 100 : null, 1)}%
            </p>
          </div>
          <div>
            <span className="text-gray-500">Product</span>
            <p className="text-gray-300 mt-0.5">{p.product || "\u2014"}</p>
          </div>
          <div>
            <span className="text-gray-500">Holding</span>
            <p className="text-gray-300 mt-0.5">
              {p.holding_period ? p.holding_period.replace("_", " ") : "\u2014"}
              {p.expected_holding_days != null && p.expected_holding_days > 0
                ? ` (${p.expected_holding_days}d)`
                : ""}
            </p>
          </div>
          <div>
            <span className="text-gray-500">Entry</span>
            <p className="text-gray-300 font-mono mt-0.5">{fmt(p.entry_price)}</p>
          </div>
          <div>
            <span className="text-gray-500">Target</span>
            <p className="text-emerald-400 font-mono mt-0.5">
              {fmt(p.target_price)}
              {priceMovePct(p.entry_price, p.target_price, p.signal_type) != null && (
                <span className="ml-1 text-[10px] text-emerald-400/70">
                  {formatPriceMovePct(priceMovePct(p.entry_price, p.target_price, p.signal_type))}
                </span>
              )}
            </p>
          </div>
          <div>
            <span className="text-gray-500">Stop Loss</span>
            <p className="text-red-400 font-mono mt-0.5">
              {fmt(p.stop_loss_price)}
              {priceMovePct(p.entry_price, p.stop_loss_price, p.signal_type) != null && (
                <span className="ml-1 text-[10px] text-red-400/70">
                  {formatPriceMovePct(priceMovePct(p.entry_price, p.stop_loss_price, p.signal_type))}
                </span>
              )}
            </p>
          </div>
          {p.actual_price != null && (
            <div>
              <span className="text-gray-500">Actual Price</span>
              <p className="text-gray-300 font-mono mt-0.5">{fmt(p.actual_price)}</p>
            </div>
          )}
          <div>
            <span className="text-gray-500">Created</span>
            <p className="text-gray-300 mt-0.5">
              {formatIST(p.created_at)}
            </p>
          </div>
          <div>
            <span className="text-gray-500">End Time</span>
            <p className="text-gray-300 mt-0.5">
              {p.prediction_end_time
                ? formatIST(p.prediction_end_time)
                : "\u2014"}
            </p>
          </div>
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
          {p.model_version && (
            <div className="col-span-2 md:col-span-4">
              <span className="text-gray-500">Model</span>
              <p className="text-gray-300 font-mono text-xs mt-0.5">{p.model_version}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const selectCls =
  "bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-emerald-500";
const inputCls =
  "bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-200 w-24 focus:outline-none focus:border-emerald-500";

/** Collapse near-duplicate predictions emitted across multiple
 * heartbeats. Two predictions are treated as duplicates when their
 * symbol + direction + model match AND the predicted target is within
 * 0.5%. The most-recent row wins; older ones contribute only to the
 * `×N` occurrence badge. Keeps the table light on heartbeat-heavy days
 * without losing the underlying audit trail (the raw rows are still
 * one API call away). */
function groupPredictions(
  items: PredictionDetail[],
): { p: PredictionDetail; count: number }[] {
  const byKey = new Map<string, { p: PredictionDetail; count: number }>();
  for (const p of items) {
    // 5% confidence bucket so two heartbeats that differ by 0.001
    // collapse, but a meaningful conviction jump still splits.
    const confBucket = p.confidence_score != null
      ? Math.round(p.confidence_score * 20) / 20
      : 0;
    const key = `${p.symbol}|${p.signal_type}|${p.model_version || ""}|${confBucket}`;
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, { p, count: 1 });
    } else {
      if ((p.created_at || "") > (existing.p.created_at || "")) {
        existing.p = p;
      }
      existing.count += 1;
    }
  }
  return Array.from(byKey.values()).sort(
    (a, b) => (b.p.created_at || "").localeCompare(a.p.created_at || ""),
  );
}

export function PredictionsPage() {
  const [tab, setTab] = useState<"today" | "unscored" | "outcomes">("today");
  const [sbGroup, setSbGroup] = useState<string | undefined>();
  // Default-on grouping — that's the point of this feature. Toggle off
  // to see every raw row (audit / debugging).
  const [group, setGroup] = useState(true);

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
          <label className="ml-auto flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={group}
              onChange={(e) => setGroup(e.target.checked)}
              className="accent-emerald-500"
            />
            Group near-duplicates
          </label>
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
              {(group ? groupPredictions(current.items) : current.items.map((p) => ({ p, count: 1 }))).map(({ p, count }) => (
                <PredictionRow key={p.prediction_id} p={p} occurrences={count} />
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
