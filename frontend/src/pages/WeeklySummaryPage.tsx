import {
  useWeeklyTrades,
  useWeeklyPredictions,
  useWeeklyLLMReviews,
} from "../hooks/queries";
import { TradesTable } from "../components/TradesTable";
import { SymbolLink } from "../components/SymbolLink";
import clsx from "clsx";

function fmt(n: number | null | undefined, d = 2) {
  if (n == null) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function WeeklySummaryPage() {
  const { data: trades, isLoading: tradesLoading } = useWeeklyTrades();
  const { data: predictions, isLoading: predsLoading } =
    useWeeklyPredictions();
  const { data: reviews, isLoading: reviewsLoading } = useWeeklyLLMReviews();

  // Compute trade stats
  const totalPnl = (trades || []).reduce(
    (s, t) => s + (t.pnl || 0),
    0
  );
  const winners = (trades || []).filter(
    (t) => t.pnl != null && t.pnl > 0
  ).length;
  const losers = (trades || []).filter(
    (t) => t.pnl != null && t.pnl < 0
  ).length;
  const winRate =
    trades && trades.length > 0
      ? (winners / trades.length) * 100
      : 0;

  // Prediction stats
  const predCorrect = (predictions || []).filter(
    (p) => p.direction_correct
  ).length;
  const predTotal = predictions?.length || 0;
  const predAccuracy = predTotal > 0 ? (predCorrect / predTotal) * 100 : 0;

  // LLM review stats
  const approved = (reviews || []).filter(
    (r) => r.decision === "APPROVE"
  ).length;
  const rejected = (reviews || []).filter(
    (r) => r.decision === "REJECT"
  ).length;
  const resized = (reviews || []).filter(
    (r) => r.decision === "RESIZE"
  ).length;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Weekly Summary</h2>

      {/* KPI cards */}
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Trades</p>
          <p className="text-xl font-semibold">{trades?.length || 0}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Total PnL</p>
          <p
            className={clsx(
              "text-xl font-semibold",
              totalPnl >= 0 ? "text-emerald-400" : "text-red-400"
            )}
          >
            {totalPnl >= 0 ? "+" : ""}
            {fmt(totalPnl, 0)}
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Winners</p>
          <p className="text-xl font-semibold text-emerald-400">{winners}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Losers</p>
          <p className="text-xl font-semibold text-red-400">{losers}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Win Rate</p>
          <p
            className={clsx(
              "text-xl font-semibold",
              winRate >= 50 ? "text-emerald-400" : "text-red-400"
            )}
          >
            {fmt(winRate, 1)}%
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Predictions</p>
          <p className="text-xl font-semibold">{predTotal}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">Pred. Accuracy</p>
          <p
            className={clsx(
              "text-xl font-semibold",
              predAccuracy >= 50 ? "text-emerald-400" : "text-red-400"
            )}
          >
            {fmt(predAccuracy, 1)}%
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
          <p className="text-xs text-gray-500">LLM Reviews</p>
          <p className="text-xl font-semibold">{reviews?.length || 0}</p>
        </div>
      </div>

      {/* LLM Reviews breakdown */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          LLM Review Decisions This Week
        </h3>
        {reviewsLoading ? (
          <div className="h-20 animate-pulse bg-gray-800 rounded" />
        ) : !reviews || reviews.length === 0 ? (
          <p className="text-gray-500 text-sm">No reviews this week</p>
        ) : (
          <>
            <div className="flex gap-4 mb-4">
              <div className="flex items-center gap-2">
                <span className="w-3 h-3 rounded-full bg-emerald-400" />
                <span className="text-sm text-gray-300">
                  Approved: {approved}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span className="w-3 h-3 rounded-full bg-red-400" />
                <span className="text-sm text-gray-300">
                  Rejected: {rejected}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <span className="w-3 h-3 rounded-full bg-amber-400" />
                <span className="text-sm text-gray-300">
                  Resized: {resized}
                </span>
              </div>
            </div>
            {/* Bar visualization */}
            <div className="h-4 bg-gray-800 rounded-full overflow-hidden flex">
              {approved > 0 && (
                <div
                  className="bg-emerald-500 h-full"
                  style={{
                    width: `${(approved / reviews.length) * 100}%`,
                  }}
                />
              )}
              {resized > 0 && (
                <div
                  className="bg-amber-500 h-full"
                  style={{
                    width: `${(resized / reviews.length) * 100}%`,
                  }}
                />
              )}
              {rejected > 0 && (
                <div
                  className="bg-red-500 h-full"
                  style={{
                    width: `${(rejected / reviews.length) * 100}%`,
                  }}
                />
              )}
            </div>
            {/* Review list */}
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-gray-500 border-b border-gray-800">
                    <th className="pb-2 pr-4">Trade ID</th>
                    <th className="pb-2 pr-4">Decision</th>
                    <th className="pb-2 pr-4">PnL</th>
                    <th className="pb-2">Reasoning</th>
                  </tr>
                </thead>
                <tbody>
                  {reviews.map((r) => (
                    <tr
                      key={r.id}
                      className="border-b border-gray-800/50 hover:bg-gray-800/30"
                    >
                      <td className="py-2 pr-4 text-xs font-mono text-gray-400">
                        {r.trade_id.slice(0, 8)}...
                      </td>
                      <td className="py-2 pr-4">
                        <span
                          className={clsx(
                            "px-1.5 py-0.5 rounded text-xs font-medium",
                            r.decision === "APPROVE"
                              ? "bg-emerald-900/40 text-emerald-400"
                              : r.decision === "REJECT"
                                ? "bg-red-900/40 text-red-400"
                                : "bg-amber-900/40 text-amber-400"
                          )}
                        >
                          {r.decision}
                        </span>
                      </td>
                      <td
                        className={clsx(
                          "py-2 pr-4 text-sm",
                          r.pnl != null && r.pnl >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        )}
                      >
                        {r.pnl != null ? fmt(r.pnl) : "—"}
                      </td>
                      <td className="py-2 text-xs text-gray-400 max-w-md truncate">
                        {r.reasoning}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {/* Weekly predictions */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Predictions This Week
        </h3>
        {predsLoading ? (
          <div className="h-20 animate-pulse bg-gray-800 rounded" />
        ) : !predictions || predictions.length === 0 ? (
          <p className="text-gray-500 text-sm">No predictions this week</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-4">Symbol</th>
                  <th className="pb-2 pr-4">Type</th>
                  <th className="pb-2 pr-4">Confidence</th>
                  <th className="pb-2 pr-4">Direction</th>
                  <th className="pb-2 pr-4">Target</th>
                  <th className="pb-2">PnL %</th>
                </tr>
              </thead>
              <tbody>
                {predictions.map((p) => (
                  <tr
                    key={p.prediction_id}
                    className="border-b border-gray-800/50 hover:bg-gray-800/30"
                  >
                    <td className="py-2 pr-4 font-medium text-emerald-400">
                      {p.symbol
                        ? <SymbolLink symbol={p.symbol} className="text-emerald-400" />
                        : "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-400">
                      {p.signal_type || "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-300">
                      {p.confidence_score
                        ? fmt(p.confidence_score * 100, 1) + "%"
                        : "—"}
                    </td>
                    <td className="py-2 pr-4">
                      {p.direction_correct != null ? (
                        <span
                          className={
                            p.direction_correct
                              ? "text-emerald-400"
                              : "text-red-400"
                          }
                        >
                          {p.direction_correct ? "Correct" : "Wrong"}
                        </span>
                      ) : (
                        <span className="text-gray-500">Pending</span>
                      )}
                    </td>
                    <td className="py-2 pr-4">
                      {p.target_hit != null ? (
                        <span
                          className={
                            p.target_hit
                              ? "text-emerald-400"
                              : "text-amber-400"
                          }
                        >
                          {p.target_hit ? "Hit" : "Missed"}
                        </span>
                      ) : (
                        <span className="text-gray-500">Pending</span>
                      )}
                    </td>
                    <td
                      className={clsx(
                        "py-2",
                        p.actual_pnl_pct != null && p.actual_pnl_pct >= 0
                          ? "text-emerald-400"
                          : "text-red-400"
                      )}
                    >
                      {p.actual_pnl_pct != null
                        ? `${p.actual_pnl_pct >= 0 ? "+" : ""}${fmt(p.actual_pnl_pct)}%`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Weekly trades */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Trades This Week
        </h3>
        {tradesLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : (
          <TradesTable trades={trades || []} />
        )}
      </div>
    </div>
  );
}
