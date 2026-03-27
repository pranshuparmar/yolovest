import { useState, useEffect } from "react";
import {
  useDryRunHistory,
  useDryRunDetail,
  useRunDryRun,
  useRunSkill,
  useScoreDryRun,
  useDeleteDryRun,
} from "../hooks/queries";
import clsx from "clsx";

function fmt(n: number | null | undefined, d = 2) {
  if (n == null) return "--";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function formatDate(iso: string) {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function DryRunPage() {
  const { data: history, isLoading: histLoading } = useDryRunHistory();
  const runDryRun = useRunDryRun();
  const runSkill = useRunSkill();
  const scoreDryRun = useScoreDryRun();
  const deleteDryRun = useDeleteDryRun();
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const { data: signals, isLoading: detailLoading } =
    useDryRunDetail(selectedRun);

  // Auto-select the most recent run on page load
  useEffect(() => {
    if (!selectedRun && history && history.length > 0) {
      setSelectedRun(history[0].run_id);
    }
  }, [history, selectedRun]);

  const handleRun = () => {
    runDryRun.mutate(undefined, {
      onSuccess: (result) => {
        if (result.run_id) setSelectedRun(result.run_id);
      },
    });
  };

  const scored = signals?.filter((s) => s.scored_at) ?? [];
  const unscored = signals?.filter((s) => !s.scored_at) ?? [];
  const correctCount = scored.filter((s) => s.direction_correct === 1).length;
  const targetHitCount = scored.filter((s) => s.target_hit === 1).length;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-gray-100">
            Signal Preview (Dry Run)
          </h2>
          <p className="text-sm text-gray-500 mt-1">
            Generate signals on current data without placing trades. Compare
            predictions against next-day actuals.
          </p>
        </div>
        <button
          onClick={handleRun}
          disabled={runDryRun.isPending}
          className="px-4 py-2 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors shrink-0"
        >
          {runDryRun.isPending ? "Scanning..." : "Generate Signals Now"}
        </button>
      </div>

      {/* Run result banner */}
      {runDryRun.isSuccess && runDryRun.data && (
        <div className="bg-emerald-900/20 border border-emerald-800 rounded-lg p-3 text-sm text-emerald-400">
          Dry run <span className="font-mono">{runDryRun.data.run_id}</span>{" "}
          complete: scanned {runDryRun.data.universe_size} stocks, shortlisted{" "}
          {runDryRun.data.shortlist_size}, generated{" "}
          <span className="font-semibold">
            {runDryRun.data.signals.length}
          </span>{" "}
          signals.
        </div>
      )}

      {runDryRun.isSuccess && runDryRun.data?.warning && (
        <div className="bg-amber-900/20 border border-amber-800 rounded-lg p-3 text-sm text-amber-400 flex items-center justify-between gap-3">
          <span>{runDryRun.data.warning}</span>
          <button
            onClick={() => runSkill.mutate("model-retrain")}
            disabled={runSkill.isPending}
            className="px-3 py-1 rounded text-xs font-medium bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-50 transition-colors shrink-0"
          >
            {runSkill.isPending ? "Training..." : "Train Model Now"}
          </button>
        </div>
      )}

      {runSkill.isSuccess && (
        <div className="bg-emerald-900/20 border border-emerald-800 rounded-lg p-3 text-sm text-emerald-400">
          Model retrain complete.{" "}
          {runSkill.data.data?.reason === "insufficient_data"
            ? `Insufficient training data (${runSkill.data.data?.bar_count ?? 0} bars). Ingest more OHLCV data first.`
            : "You can now re-run the dry run."}
        </div>
      )}

      {runSkill.isError && (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-sm text-red-400">
          Model retrain failed. Check server logs for details.
        </div>
      )}

      {runDryRun.isError && (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-sm text-red-400">
          Dry run failed. Make sure you have OHLCV data ingested and ML model
          loaded.
        </div>
      )}

      {/* Past runs */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800">
          <h3 className="text-sm font-semibold text-gray-300">Past Runs</h3>
        </div>
        {histLoading ? (
          <div className="h-20 animate-pulse bg-gray-800 m-4 rounded" />
        ) : !history || history.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-gray-500">
            No dry runs yet. Click "Generate Signals Now" to create one.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                  <th className="py-2 px-4 text-left">Run ID</th>
                  <th className="py-2 px-4 text-right">Signals</th>
                  <th className="py-2 px-4 text-right">Scored</th>
                  <th className="py-2 px-4 text-right">Correct</th>
                  <th className="py-2 px-4 text-right">Created</th>
                  <th className="py-2 px-4 text-center">Actions</th>
                </tr>
              </thead>
              <tbody>
                {history.map((run) => (
                  <tr
                    key={run.run_id}
                    className={clsx(
                      "border-b border-gray-800 hover:bg-gray-800/30 cursor-pointer",
                      selectedRun === run.run_id && "bg-gray-800/50"
                    )}
                    onClick={() => setSelectedRun(run.run_id)}
                  >
                    <td className="py-2 px-4 font-mono text-xs text-gray-300">
                      {run.run_id}
                    </td>
                    <td className="py-2 px-4 text-right text-gray-300">
                      {run.signal_count}
                    </td>
                    <td className="py-2 px-4 text-right text-gray-400">
                      {run.scored}/{run.signal_count}
                    </td>
                    <td className="py-2 px-4 text-right">
                      {run.scored > 0 ? (
                        <span
                          className={clsx(
                            "font-medium",
                            (run.correct ?? 0) / run.scored >= 0.5
                              ? "text-emerald-400"
                              : "text-red-400"
                          )}
                        >
                          {run.correct ?? 0}/{run.scored}
                        </span>
                      ) : (
                        <span className="text-gray-500">--</span>
                      )}
                    </td>
                    <td className="py-2 px-4 text-right text-gray-400 text-xs">
                      {formatDate(run.created_at)}
                    </td>
                    <td className="py-2 px-4 text-center space-x-1">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          scoreDryRun.mutate(run.run_id);
                        }}
                        disabled={
                          scoreDryRun.isPending ||
                          run.scored === run.signal_count
                        }
                        className="px-2 py-1 rounded text-xs bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-30 transition-colors"
                      >
                        {scoreDryRun.isPending ? "..." : "Score"}
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (selectedRun === run.run_id) setSelectedRun(null);
                          deleteDryRun.mutate(run.run_id);
                        }}
                        disabled={deleteDryRun.isPending}
                        className="px-2 py-1 rounded text-xs bg-red-600 hover:bg-red-700 text-white disabled:opacity-30 transition-colors"
                        title="Delete this dry run"
                      >
                        {deleteDryRun.isPending ? "..." : "Delete"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Signal detail */}
      {selectedRun && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-800 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-gray-300">
              Signals for run{" "}
              <span className="font-mono text-emerald-400">{selectedRun}</span>
            </h3>
            {scored.length > 0 && (
              <div className="flex items-center gap-4 text-xs">
                <span className="text-gray-400">
                  Direction accuracy:{" "}
                  <span
                    className={clsx(
                      "font-semibold",
                      correctCount / scored.length >= 0.5
                        ? "text-emerald-400"
                        : "text-red-400"
                    )}
                  >
                    {((correctCount / scored.length) * 100).toFixed(0)}%
                  </span>
                </span>
                <span className="text-gray-400">
                  Target hit:{" "}
                  <span className="font-semibold text-amber-400">
                    {((targetHitCount / scored.length) * 100).toFixed(0)}%
                  </span>
                </span>
              </div>
            )}
          </div>

          {detailLoading ? (
            <div className="h-32 animate-pulse bg-gray-800 m-4 rounded" />
          ) : !signals || signals.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-gray-500">
              No signals in this run.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                    <th className="py-2 px-3 text-left">Symbol</th>
                    <th className="py-2 px-3 text-center">Signal</th>
                    <th className="py-2 px-3 text-right">Entry</th>
                    <th className="py-2 px-3 text-right">Target</th>
                    <th className="py-2 px-3 text-right">SL</th>
                    <th className="py-2 px-3 text-right">Confidence</th>
                    <th className="py-2 px-3 text-right">Actual Close</th>
                    <th className="py-2 px-3 text-right">Move %</th>
                    <th className="py-2 px-3 text-center">Direction</th>
                    <th className="py-2 px-3 text-center">Target Hit</th>
                  </tr>
                </thead>
                <tbody>
                  {(signals || []).map((s) => (
                    <tr
                      key={s.id}
                      className="border-b border-gray-800/50 hover:bg-gray-800/30"
                    >
                      <td className="py-2 px-3 font-medium text-gray-200">
                        {s.symbol}
                      </td>
                      <td className="py-2 px-3 text-center">
                        <span
                          className={clsx(
                            "px-1.5 py-0.5 rounded text-xs font-medium",
                            s.signal_type === "BUY"
                              ? "bg-emerald-900/40 text-emerald-400"
                              : "bg-red-900/40 text-red-400"
                          )}
                        >
                          {s.signal_type}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-right font-mono text-gray-300">
                        {fmt(s.entry_price)}
                      </td>
                      <td className="py-2 px-3 text-right font-mono text-emerald-400">
                        {fmt(s.target_price)}
                      </td>
                      <td className="py-2 px-3 text-right font-mono text-red-400">
                        {fmt(s.stop_loss_price)}
                      </td>
                      <td className="py-2 px-3 text-right">
                        <span
                          className={clsx(
                            "font-medium",
                            s.confidence_score >= 0.7
                              ? "text-emerald-400"
                              : s.confidence_score >= 0.5
                                ? "text-amber-400"
                                : "text-gray-400"
                          )}
                        >
                          {(s.confidence_score * 100).toFixed(0)}%
                        </span>
                      </td>
                      <td className="py-2 px-3 text-right font-mono text-gray-300">
                        {s.actual_close != null ? fmt(s.actual_close) : (
                          <span className="text-gray-600">pending</span>
                        )}
                      </td>
                      <td className="py-2 px-3 text-right">
                        {s.actual_move_pct != null ? (
                          <span
                            className={clsx(
                              "font-medium",
                              s.actual_move_pct >= 0
                                ? "text-emerald-400"
                                : "text-red-400"
                            )}
                          >
                            {s.actual_move_pct >= 0 ? "+" : ""}
                            {fmt(s.actual_move_pct)}%
                          </span>
                        ) : (
                          <span className="text-gray-600">--</span>
                        )}
                      </td>
                      <td className="py-2 px-3 text-center">
                        {s.direction_correct != null ? (
                          s.direction_correct === 1 ? (
                            <span className="text-emerald-400">Y</span>
                          ) : (
                            <span className="text-red-400">N</span>
                          )
                        ) : (
                          <span className="text-gray-600">--</span>
                        )}
                      </td>
                      <td className="py-2 px-3 text-center">
                        {s.target_hit != null ? (
                          s.target_hit === 1 ? (
                            <span className="text-emerald-400">Y</span>
                          ) : (
                            <span className="text-red-400">N</span>
                          )
                        ) : (
                          <span className="text-gray-600">--</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Unscored / Scored summary */}
          {signals && signals.length > 0 && (
            <div className="px-4 py-3 border-t border-gray-800 flex flex-wrap gap-4 text-xs text-gray-500">
              {unscored.length > 0 && (
                <span>
                  {unscored.length} signal{unscored.length > 1 ? "s" : ""}{" "}
                  awaiting next-day data — click "Score" after market data is
                  ingested tomorrow.
                </span>
              )}
              {scored.length > 0 && (
                <span>
                  {scored.length} scored:{" "}
                  <span className="text-emerald-400">
                    {correctCount} correct
                  </span>
                  ,{" "}
                  <span className="text-red-400">
                    {scored.length - correctCount} wrong
                  </span>
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {/* How it works */}
      <div className="bg-gray-900/50 border border-gray-800 rounded-lg p-4 text-xs text-gray-500 space-y-2">
        <p className="font-medium text-gray-400">How it works:</p>
        <ol className="list-decimal list-inside space-y-1">
          <li>
            Click "Generate Signals Now" — runs market scan + ML model on your
            current OHLCV data (works anytime, even when market is closed).
          </li>
          <li>
            Review the predicted signals: symbol, direction, entry/target/SL,
            confidence.
          </li>
          <li>
            After the next trading day, click "Score" to compare predictions
            against actual market data.
          </li>
          <li>
            Check direction accuracy and target hit rate to evaluate model
            quality before going live.
          </li>
        </ol>
      </div>
    </div>
  );
}
