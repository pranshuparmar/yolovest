import { useState } from "react";
import type { ScoreboardEntry } from "../types/api";
import clsx from "clsx";
import { SymbolLink } from "./SymbolLink";

function pct(v: number | null) {
  if (v === null) return "\u2014";
  return `${(v * 100).toFixed(1)}%`;
}

type SortKey = "group_key" | "total_predictions" | "accuracy" | "avg_confidence" | "target_hit_rate" | "avg_pnl_pct";

export function ScoreboardTable({ entries }: { entries: ScoreboardEntry[] }) {
  const [sortKey, setSortKey] = useState<SortKey>("total_predictions");
  const [sortAsc, setSortAsc] = useState(false);

  if (entries.length === 0) {
    return <p className="text-gray-500 text-sm py-4">No prediction data</p>;
  }

  const sorted = [...entries].sort((a, b) => {
    const av = (a as unknown as Record<string, unknown>)[sortKey] ?? 0;
    const bv = (b as unknown as Record<string, unknown>)[sortKey] ?? 0;
    if (typeof av === "string" && typeof bv === "string") {
      return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    }
    return sortAsc ? (av as number) - (bv as number) : (bv as number) - (av as number);
  });

  const handleSort = (key: SortKey) => {
    if (sortKey === key) setSortAsc(!sortAsc);
    else { setSortKey(key); setSortAsc(false); }
  };

  const thCls = "pb-2 pr-4 cursor-pointer hover:text-gray-300 select-none";
  const arrow = (key: SortKey) => sortKey === key ? (sortAsc ? " \u25B2" : " \u25BC") : "";

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800 text-xs uppercase tracking-wide">
            <th className={thCls} onClick={() => handleSort("group_key")}>
              Group{arrow("group_key")}
            </th>
            <th className={clsx(thCls, "text-right")} onClick={() => handleSort("total_predictions")}>
              Total{arrow("total_predictions")}
            </th>
            <th className="pb-2 pr-4 text-right">Correct</th>
            <th className={clsx(thCls, "text-right")} onClick={() => handleSort("accuracy")}>
              Accuracy{arrow("accuracy")}
            </th>
            <th className={clsx(thCls, "text-right")} onClick={() => handleSort("avg_confidence")}>
              Avg Conf{arrow("avg_confidence")}
            </th>
            <th className={clsx(thCls, "text-right")} onClick={() => handleSort("target_hit_rate")}>
              Target Hit{arrow("target_hit_rate")}
            </th>
            <th className={clsx(thCls, "text-right")} onClick={() => handleSort("avg_pnl_pct")}>
              Avg PnL{arrow("avg_pnl_pct")}
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((e) => {
            const isOverall = e.group_key === "overall";
            return (
              <tr
                key={e.group_key}
                className={clsx(
                  "border-b border-gray-800/50 hover:bg-gray-800/30",
                  isOverall && "bg-gray-800/20 font-semibold"
                )}
              >
                <td className="py-2 pr-4">
                  {e.group_key.startsWith("symbol:") ? (
                    <SymbolLink
                      symbol={e.group_key.replace("symbol:", "")}
                      className="text-emerald-400"
                    />
                  ) : e.group_key.startsWith("model:") ? (
                    <span className="text-blue-400 font-mono text-xs">{e.group_key.replace("model:", "")}</span>
                  ) : (
                    <span className={isOverall ? "text-gray-200" : ""}>{e.group_key}</span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right">{e.total_predictions}</td>
                <td className="py-2 pr-4 text-right">{e.correct_predictions}</td>
                <td
                  className={clsx(
                    "py-2 pr-4 text-right font-medium",
                    e.accuracy !== null && e.accuracy >= 0.5
                      ? "text-emerald-400"
                      : "text-red-400"
                  )}
                >
                  {pct(e.accuracy)}
                </td>
                <td className="py-2 pr-4 text-right text-gray-400">{pct(e.avg_confidence)}</td>
                <td
                  className={clsx(
                    "py-2 pr-4 text-right",
                    e.target_hit_rate !== null && e.target_hit_rate > 0
                      ? "text-blue-400"
                      : "text-gray-500"
                  )}
                >
                  {pct(e.target_hit_rate)}
                </td>
                <td
                  className={clsx(
                    "py-2 text-right font-medium",
                    e.avg_pnl_pct !== null && e.avg_pnl_pct > 0
                      ? "text-emerald-400"
                      : e.avg_pnl_pct !== null && e.avg_pnl_pct < 0
                      ? "text-red-400"
                      : "text-gray-500"
                  )}
                >
                  {e.avg_pnl_pct !== null
                    ? `${e.avg_pnl_pct > 0 ? "+" : ""}${e.avg_pnl_pct.toFixed(2)}%`
                    : "\u2014"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
