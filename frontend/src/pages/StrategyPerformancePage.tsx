import { useStrategyPerformance } from "../hooks/queries";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Cell,
} from "recharts";
import clsx from "clsx";
import type { PerformanceRow } from "../types/api";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function PerfTable({ rows, labelKey }: { rows: PerformanceRow[]; labelKey: string }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            <th className="pb-2 pr-4">{labelKey}</th>
            <th className="pb-2 pr-4">Trades</th>
            <th className="pb-2 pr-4">Wins</th>
            <th className="pb-2 pr-4">Losses</th>
            <th className="pb-2 pr-4">Win Rate</th>
            <th className="pb-2 pr-4">Total PnL</th>
            <th className="pb-2">Avg PnL</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const rAny = r as unknown as Record<string, unknown>;
            const label = rAny[labelKey.toLowerCase().replace(/ /g, "_")] ?? rAny["hour"] ?? "—";
            const winRate = r.cnt > 0 ? (r.wins / r.cnt) * 100 : 0;
            return (
              <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="py-2 pr-4 font-medium">{String(label)}</td>
                <td className="py-2 pr-4">{r.cnt}</td>
                <td className="py-2 pr-4 text-emerald-400">{r.wins}</td>
                <td className="py-2 pr-4 text-red-400">{r.losses}</td>
                <td className={clsx("py-2 pr-4", winRate >= 50 ? "text-emerald-400" : "text-red-400")}>
                  {fmt(winRate, 1)}%
                </td>
                <td className={clsx("py-2 pr-4", r.total_pnl >= 0 ? "text-emerald-400" : "text-red-400")}>
                  ₹{fmt(r.total_pnl, 0)}
                </td>
                <td className={clsx("py-2", r.avg_pnl >= 0 ? "text-emerald-400" : "text-red-400")}>
                  ₹{fmt(r.avg_pnl)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function StrategyPerformancePage() {
  const { data, isLoading } = useStrategyPerformance();
  const ct = useChartTheme();
  const tooltipStyle = useTooltipStyle();

  if (isLoading) return <div className="h-96 animate-pulse bg-gray-900 rounded-lg" />;
  if (!data) return <p className="text-gray-500">No performance data</p>;

  const hourData = (data.by_hour || []).map((h) => ({
    hour: `${h.hour}:00`,
    pnl: h.total_pnl,
    winRate: h.cnt > 0 ? (h.wins / h.cnt) * 100 : 0,
    trades: h.cnt,
  }));

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Strategy Performance</h2>

      {/* PnL by Hour chart */}
      {hourData.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">PnL by Entry Hour</h3>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={hourData}>
              <CartesianGrid strokeDasharray="3 3" stroke={ct.grid} />
              <XAxis dataKey="hour" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis tick={{ fontSize: 10, fill: ct.tick }} />
              <Tooltip contentStyle={tooltipStyle} />
              <Bar dataKey="pnl" name="PnL">
                {hourData.map((d, i) => (
                  <Cell key={i} fill={d.pnl >= 0 ? "#3fb950" : "#f85149"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">By Signal Type</h3>
          <PerfTable rows={data.by_signal_type || []} labelKey="signal_type" />
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">By Product</h3>
          <PerfTable rows={data.by_product || []} labelKey="product" />
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">By Sector</h3>
        <PerfTable rows={data.by_sector || []} labelKey="sector" />
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">By Holding Period</h3>
        <PerfTable rows={data.by_holding_period || []} labelKey="holding_period" />
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">By Entry Hour</h3>
        <PerfTable rows={data.by_hour || []} labelKey="hour" />
      </div>
    </div>
  );
}
