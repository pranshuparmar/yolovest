import type { ScoreboardEntry } from "../types/api";
import clsx from "clsx";

function pct(v: number | null) {
  if (v === null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

export function ScoreboardTable({ entries }: { entries: ScoreboardEntry[] }) {
  if (entries.length === 0) {
    return <p className="text-gray-500 text-sm py-4">No prediction data</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            <th className="pb-2 pr-4">Group</th>
            <th className="pb-2 pr-4">Total</th>
            <th className="pb-2 pr-4">Correct</th>
            <th className="pb-2 pr-4">Accuracy</th>
            <th className="pb-2 pr-4">Avg Confidence</th>
            <th className="pb-2 pr-4">Target Hit Rate</th>
            <th className="pb-2">Avg PnL %</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr
              key={e.group_key}
              className="border-b border-gray-800/50 hover:bg-gray-800/30"
            >
              <td className="py-2 pr-4 font-medium">{e.group_key}</td>
              <td className="py-2 pr-4">{e.total_predictions}</td>
              <td className="py-2 pr-4">{e.correct_predictions}</td>
              <td
                className={clsx(
                  "py-2 pr-4 font-medium",
                  e.accuracy !== null && e.accuracy >= 0.5
                    ? "text-emerald-400"
                    : "text-red-400"
                )}
              >
                {pct(e.accuracy)}
              </td>
              <td className="py-2 pr-4">{pct(e.avg_confidence)}</td>
              <td className="py-2 pr-4">{pct(e.target_hit_rate)}</td>
              <td
                className={clsx(
                  "py-2",
                  e.avg_pnl_pct !== null && e.avg_pnl_pct > 0
                    ? "text-emerald-400"
                    : "text-red-400"
                )}
              >
                {e.avg_pnl_pct !== null ? `${e.avg_pnl_pct.toFixed(2)}%` : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
