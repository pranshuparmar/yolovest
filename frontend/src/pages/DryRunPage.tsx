import { useState, useEffect, useMemo } from "react";
import {
  useDryRunHistory,
  useDryRunDetail,
  useRunDryRun,
  useRunSkill,
  useScoreDryRun,
  useDeleteDryRun,
  useMLModels,
} from "../hooks/queries";
import clsx from "clsx";
import { formatPriceMovePct, priceMovePct } from "../utils/priceMove";

function fmt(n: number | null | undefined, d = 2) {
  if (n == null) return "--";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

// Signed rupee for net P&L (e.g. "+₹1,234.00" / "−₹987.00").
function netRupee(n: number) {
  return `${n >= 0 ? "+" : "−"}₹${fmt(Math.abs(n))}`;
}

import { formatIST } from "../utils/datetime";
import { SymbolLink } from "../components/SymbolLink";

function formatDate(iso: string) {
  return formatIST(iso);
}

export function DryRunPage() {
  const { data: history, isLoading: histLoading } = useDryRunHistory();
  const runDryRun = useRunDryRun();
  const runSkill = useRunSkill();
  const scoreDryRun = useScoreDryRun();
  const deleteDryRun = useDeleteDryRun();
  const [scoreMsg, setScoreMsg] = useState<{ text: string; type: "info" | "warn" } | null>(null);
  const [selectedMode, setSelectedMode] = useState<string>("balanced");
  const [asOf, setAsOf] = useState<string>("");
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const { data: signals, isLoading: detailLoading } =
    useDryRunDetail(selectedRun);
  const { data: mlModels } = useMLModels();

  // Flatten production / shadow / retired into one labelled list. Value is
  // the version string (passed as model_version); empty value = production.
  const modelOptions = useMemo(() => {
    const opts: { version: string; label: string }[] = [];
    if (!mlModels) return opts;
    for (const [mt, m] of Object.entries(mlModels.production)) {
      if (m?.version) opts.push({ version: m.version, label: `${mt} · ${m.version} · production` });
    }
    for (const m of mlModels.shadow ?? []) {
      if (m?.version) opts.push({ version: m.version, label: `${m.model_type} · ${m.version} · shadow` });
    }
    for (const m of mlModels.retired ?? []) {
      if (m?.version) opts.push({ version: m.version, label: `${m.model_type} · ${m.version} · retired` });
    }
    return opts;
  }, [mlModels]);

  // Auto-select the most recent run on page load
  useEffect(() => {
    if (!selectedRun && history && history.length > 0) {
      setSelectedRun(history[0].run_id);
    }
  }, [history, selectedRun]);

  const handleRun = () => {
    runDryRun.mutate(
      {
        mode: selectedMode,
        asOf: asOf || undefined,
        modelVersion: selectedModel || undefined,
      },
      {
        onSuccess: (result) => {
          if (result.run_id) setSelectedRun(result.run_id);
        },
      },
    );
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
        <div className="flex items-center gap-2 shrink-0">
          <select
            value={selectedMode}
            onChange={(e) => setSelectedMode(e.target.value)}
            className="px-3 py-2 rounded text-sm bg-gray-800 border border-gray-700 text-gray-200 focus:outline-none focus:border-emerald-500"
          >
            <option value="intraday">Intraday</option>
            <option value="short_term">Short Term</option>
            <option value="balanced">Balanced</option>
            <option value="long_term">Long Term</option>
            <option value="swing">Swing (Short + Long)</option>
          </select>
          <input
            type="date"
            value={asOf}
            max={new Date().toISOString().slice(0, 10)}
            onChange={(e) => setAsOf(e.target.value)}
            title="Evaluate as of a past date (leave blank for latest)"
            className="px-3 py-2 rounded text-sm bg-gray-800 border border-gray-700 text-gray-200 focus:outline-none focus:border-emerald-500"
          />
          {/* Always mounted so the toolbar width is identical with/without a
              date — toggling visibility (not mount) stops the controls from
              reflowing onto a new line when a date is picked or cleared. */}
          <button
            onClick={() => setAsOf("")}
            title="Clear date — use latest data"
            aria-hidden={!asOf}
            tabIndex={asOf ? 0 : -1}
            className={clsx(
              "px-2 py-2 rounded text-sm text-gray-400 hover:text-gray-200",
              !asOf && "invisible pointer-events-none",
            )}
          >
            ×
          </button>
          <select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            title="Evaluate against a specific model version (shadow / retired). Defaults to the production model."
            className="px-3 py-2 rounded text-sm bg-gray-800 border border-gray-700 text-gray-200 focus:outline-none focus:border-emerald-500 max-w-[16rem]"
          >
            <option value="">Production model (default)</option>
            {modelOptions.map((m) => (
              <option key={m.version} value={m.version}>
                {m.label}
              </option>
            ))}
          </select>
          <button
            onClick={handleRun}
            disabled={runDryRun.isPending}
            className="px-4 py-2 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
          >
            {runDryRun.isPending ? "Scanning..." : "Generate Signals Now"}
          </button>
        </div>
      </div>

      {/* Run result banner */}
      {runDryRun.isSuccess && runDryRun.data && (
        <div className="bg-emerald-900/20 border border-emerald-800 rounded-lg p-3 text-sm text-emerald-400">
          Dry run <span className="font-mono">{runDryRun.data.run_id}</span>{" "}
          ({runDryRun.data.mode ?? "balanced"} mode
          {runDryRun.data.as_of ? `, as of ${runDryRun.data.as_of}` : ""}
          {runDryRun.data.selected_model
            ? `, model ${runDryRun.data.selected_model.version} (${runDryRun.data.selected_model.status ?? "?"})`
            : ""}) complete: scanned{" "}
          {runDryRun.data.universe_size} stocks, shortlisted{" "}
          {runDryRun.data.shortlist_size}, generated{" "}
          <span className="font-semibold">
            {runDryRun.data.signals.length}
          </span>{" "}
          signals.
          {runDryRun.data.scoring && (runDryRun.data.scoring.scored > 0 || (runDryRun.data.scoring.pending ?? 0) > 0) && (
            <>
              {" "}Auto-scored{" "}
              <span className="font-semibold">{runDryRun.data.scoring.scored}</span>{" "}
              against actuals
              {(runDryRun.data.scoring.pending ?? 0) > 0
                ? `, ${runDryRun.data.scoring.pending} still pending (window not elapsed).`
                : "."}
            </>
          )}
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

      {scoreMsg && (
        <div
          className={clsx(
            "rounded-lg p-3 text-sm flex items-center justify-between",
            scoreMsg.type === "warn"
              ? "bg-amber-900/20 border border-amber-800 text-amber-400"
              : "bg-emerald-900/20 border border-emerald-800 text-emerald-400",
          )}
        >
          <span>{scoreMsg.text}</span>
          <button
            onClick={() => setScoreMsg(null)}
            className="text-xs opacity-60 hover:opacity-100 ml-2"
          >
            Dismiss
          </button>
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
                  <th className="py-2 px-4 text-left">Strategy</th>
                  <th className="py-2 px-4 text-left">As Of</th>
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
                    <td className="py-2 px-4 text-xs text-gray-400 capitalize">
                      {(run.strategy_mode ?? "balanced").replace("_", " ")}
                    </td>
                    <td className="py-2 px-4 text-xs">
                      {run.as_of ? (
                        <span className="text-gray-300 font-mono">{run.as_of}</span>
                      ) : (
                        <span className="text-gray-600">latest</span>
                      )}
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
                          setScoreMsg(null);
                          scoreDryRun.mutate(run.run_id, {
                            onSuccess: (data) => {
                              if (data.message) {
                                setScoreMsg({ text: data.message, type: "warn" });
                              } else if (data.scored > 0) {
                                setScoreMsg({ text: `Scored ${data.scored} signal(s).`, type: "info" });
                              }
                            },
                          });
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
                          if (!window.confirm(`Delete dry run ${run.run_id}? This cannot be undone.`)) return;
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
              {(() => {
                const run = history?.find((r) => r.run_id === selectedRun);
                if (!run) return null;
                return (
                  <>
                    {run.strategy_mode && (
                      <span className="ml-2 px-2 py-0.5 rounded text-xs font-medium bg-gray-800 text-gray-400 capitalize">
                        {run.strategy_mode.replace("_", " ")}
                      </span>
                    )}
                    <span className="ml-2 px-2 py-0.5 rounded text-xs font-medium bg-gray-800 text-gray-400">
                      as of {run.as_of ?? "latest"}
                    </span>
                    {run.model_version && (
                      <span
                        className="ml-2 px-2 py-0.5 rounded text-xs font-medium bg-gray-800 text-gray-400 font-mono"
                        title="Model version that produced these signals"
                      >
                        {run.model_version}
                      </span>
                    )}
                  </>
                );
              })()}
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
                    <th className="py-2 px-3 text-left">Hold / Target</th>
                    <th className="py-2 px-3 text-right">Entry / Target / SL</th>
                    <th className="py-2 px-3 text-right">Conf.</th>
                    <th className="py-2 px-3 text-right">Net G/L · Costs</th>
                    <th className="py-2 px-3 text-right">Actual</th>
                    <th className="py-2 px-3 text-center">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {(signals || []).map((s) => (
                    <tr
                      key={s.id}
                      className="border-b border-gray-800/50 hover:bg-gray-800/30 align-top"
                    >
                      {/* Symbol + direction + product */}
                      <td className="py-2 px-3">
                        <SymbolLink symbol={s.symbol} className="font-medium text-gray-200" />
                        <div className="flex items-center gap-1 mt-1">
                          <span
                            className={clsx(
                              "px-1.5 py-0.5 rounded text-[10px] font-medium",
                              s.signal_type === "BUY"
                                ? "bg-emerald-900/40 text-emerald-400"
                                : "bg-red-900/40 text-red-400"
                            )}
                          >
                            {s.signal_type}
                          </span>
                          {s.product && (
                            <span
                              className={clsx(
                                "px-1.5 py-0.5 rounded text-[10px] font-medium",
                                s.product === "MIS"
                                  ? "bg-amber-900/30 text-amber-400"
                                  : "bg-blue-900/30 text-blue-400"
                              )}
                            >
                              {s.product}
                            </span>
                          )}
                        </div>
                      </td>
                      {/* Holding period + target date */}
                      <td className="py-2 px-3 text-xs">
                        <div className="text-gray-300">
                          {s.holding_period ?? "--"}
                          {s.expected_holding_days != null && s.expected_holding_days > 0 && (
                            <span className="text-gray-600 ml-1">({s.expected_holding_days}d)</span>
                          )}
                        </div>
                        <div className="text-gray-500 font-mono mt-0.5">
                          {s.target_date ?? "--"}
                        </div>
                      </td>
                      {/* Entry / Target (+%) / SL (+%) */}
                      <td className="py-2 px-3 text-right font-mono text-xs whitespace-nowrap">
                        <div className="text-gray-300">
                          <span className="text-gray-600 mr-1">E</span>
                          {fmt(s.entry_price)}
                        </div>
                        <div className="text-emerald-400 mt-0.5">
                          <span className="text-gray-600 mr-1">T</span>
                          {fmt(s.target_price)}
                          <span className="ml-1 text-[10px] text-emerald-400/70">
                            {formatPriceMovePct(priceMovePct(s.entry_price, s.target_price, s.signal_type))}
                          </span>
                        </div>
                        <div className="text-red-400 mt-0.5">
                          <span className="text-gray-600 mr-1">SL</span>
                          {fmt(s.stop_loss_price)}
                          <span className="ml-1 text-[10px] text-red-400/70">
                            {formatPriceMovePct(priceMovePct(s.entry_price, s.stop_loss_price, s.signal_type))}
                          </span>
                        </div>
                      </td>
                      {/* Confidence */}
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
                      {/* Net G/L + estimated costs */}
                      <td className="py-2 px-3 text-right font-mono text-xs">
                        {s.est_net_gain != null && s.est_net_loss != null ? (
                          <div title="Net P&L after all deductions if target hits / if SL hits">
                            <span className="text-emerald-400">{netRupee(s.est_net_gain)}</span>
                            <span className="text-gray-600"> / </span>
                            <span className="text-red-400">{netRupee(s.est_net_loss)}</span>
                          </div>
                        ) : (
                          <div className="text-gray-600">--</div>
                        )}
                        {s.estimated_costs != null && (
                          <div className="text-gray-500 mt-0.5">
                            costs ₹{fmt(s.estimated_costs)}
                          </div>
                        )}
                      </td>
                      {/* Actual close + realised move % */}
                      <td className="py-2 px-3 text-right font-mono text-xs">
                        <div className="text-gray-300">
                          {s.actual_close != null ? (
                            fmt(s.actual_close)
                          ) : (
                            <span className="text-gray-600">pending</span>
                          )}
                        </div>
                        {s.actual_move_pct != null && (
                          <div
                            className={clsx(
                              "mt-0.5 font-medium",
                              s.actual_move_pct >= 0
                                ? "text-emerald-400"
                                : "text-red-400"
                            )}
                          >
                            {s.actual_move_pct >= 0 ? "+" : ""}
                            {fmt(s.actual_move_pct)}%
                          </div>
                        )}
                      </td>
                      {/* Result: direction + target hit */}
                      <td className="py-2 px-3 text-center whitespace-nowrap">
                        {s.direction_correct != null || s.target_hit != null ? (
                          <div className="flex flex-col items-center gap-1">
                            {s.direction_correct != null && (
                              <span
                                className={clsx(
                                  "px-1.5 py-0.5 rounded text-[10px] font-medium",
                                  s.direction_correct === 1
                                    ? "bg-emerald-900/40 text-emerald-400"
                                    : "bg-red-900/40 text-red-400"
                                )}
                              >
                                {s.direction_correct === 1 ? "Dir ✓" : "Dir ✗"}
                              </span>
                            )}
                            {s.target_hit != null && (
                              <span
                                className={clsx(
                                  "px-1.5 py-0.5 rounded text-[10px] font-medium",
                                  s.target_hit === 1
                                    ? "bg-emerald-900/40 text-emerald-400"
                                    : "bg-amber-900/40 text-amber-400"
                                )}
                              >
                                {s.target_hit === 1 ? "Tgt ✓" : "Tgt ✗"}
                              </span>
                            )}
                          </div>
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
            Pick a past "as of" date and the run scores itself instantly —
            each signal is compared against the actual close on its target
            date (% change shown). For latest-data runs, click "Score" after
            the next trading day once market data is ingested.
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
