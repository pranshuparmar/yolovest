import { useState } from "react";
import { useExecutionQuality } from "../hooks/queries";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import clsx from "clsx";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";

function fmt(n: number | null | undefined, d = 2) {
  if (n == null) return "0";
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function ExecutionQualityPage() {
  const [days, setDays] = useState(30);
  const { data, isLoading } = useExecutionQuality(days);
  const ct = useChartTheme();
  const tooltipStyle = useTooltipStyle();

  if (isLoading) return <div className="h-96 animate-pulse bg-gray-900 rounded-lg" />;
  if (!data) return <p className="text-gray-500">No execution data</p>;

  const hourData = (data.slippage_by_hour || []).map((h) => ({
    hour: `${h.hour}:00`,
    avg: h.avg_slippage,
    max: h.max_slippage,
    count: h.cnt,
  }));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Execution Quality</h2>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}
          className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100">
          <option value={7}>7 days</option>
          <option value={30}>30 days</option>
          <option value={90}>90 days</option>
          <option value={365}>1 year</option>
        </select>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Fill Rate</p>
          <p className="text-xl font-semibold text-emerald-400">{fmt(data.fill_rate_pct, 1)}%</p>
          <p className="text-xs text-gray-500 mt-1">{data.filled_orders}/{data.total_orders} orders</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Avg Slippage</p>
          <p className="text-xl font-semibold text-amber-400">{fmt(data.avg_abs_slippage, 3)}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Max Slippage</p>
          <p className="text-xl font-semibold text-red-400">{fmt(data.max_abs_slippage, 3)}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Zero Slippage</p>
          <p className="text-xl font-semibold">{fmt(data.zero_slippage_pct, 1)}%</p>
        </div>
      </div>

      {/* Slippage by Hour */}
      {hourData.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Avg Slippage by Entry Hour</h3>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={hourData}>
              <CartesianGrid strokeDasharray="3 3" stroke={ct.grid} />
              <XAxis dataKey="hour" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis tick={{ fontSize: 10, fill: ct.tick }} />
              <Tooltip contentStyle={tooltipStyle} />
              <Bar dataKey="avg" name="Avg Slippage" fill="#d29922" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Slippage by Size */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Slippage by Order Size</h3>
        {!data.slippage_by_size || data.slippage_by_size.length === 0 ? (
          <p className="text-gray-500 text-sm">No data</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-4">Size Bucket</th>
                  <th className="pb-2 pr-4">Orders</th>
                  <th className="pb-2 pr-4">Avg Slippage</th>
                  <th className="pb-2">Max Slippage</th>
                </tr>
              </thead>
              <tbody>
                {data.slippage_by_size.map((row, i) => (
                  <tr key={i} className="border-b border-gray-800/50">
                    <td className="py-2 pr-4 font-medium">{row.size_bucket}</td>
                    <td className="py-2 pr-4">{row.cnt}</td>
                    <td className="py-2 pr-4 text-amber-400">{fmt(row.avg_slippage, 3)}</td>
                    <td className="py-2 text-red-400">{fmt(row.max_slippage, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Signed slippage */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-2">Slippage Direction</h3>
        <p className="text-sm text-gray-300">
          Avg signed slippage: <span className={clsx(data.avg_signed_slippage >= 0 ? "text-red-400" : "text-emerald-400")}>
            {fmt(data.avg_signed_slippage, 4)}
          </span>
          {" "}— {data.avg_signed_slippage >= 0
            ? "You're paying more than expected on average (unfavorable)"
            : "You're getting better fills than expected on average (favorable)"}
        </p>
      </div>
    </div>
  );
}
