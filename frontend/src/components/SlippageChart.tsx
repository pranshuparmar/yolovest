import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import type { SlippageStats } from "../types/api";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";

export function SlippageChart({ data }: { data: SlippageStats }) {
  const ct = useChartTheme();
  const tooltipStyle = useTooltipStyle();

  const chartData = Object.entries(data.by_symbol).map(([symbol, stats]) => ({
    symbol,
    avg: stats.avg_slippage,
    max: stats.max_slippage,
    count: stats.count,
  }));

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-2">
        Slippage by Symbol
      </h3>
      <div className="grid grid-cols-3 gap-4 mb-4 text-center">
        <div>
          <p className="text-xs text-gray-500">Total Trades</p>
          <p className="text-lg font-semibold">{data.total_trades}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Avg Slippage</p>
          <p className="text-lg font-semibold">
            {data.avg_slippage.toFixed(2)}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Avg Slippage %</p>
          <p className="text-lg font-semibold">
            {data.avg_slippage_pct.toFixed(3)}%
          </p>
        </div>
      </div>
      {chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke={ct.grid} />
            <XAxis
              dataKey="symbol"
              tick={{ fill: ct.tick, fontSize: 10 }}
              angle={-45}
              textAnchor="end"
              height={60}
            />
            <YAxis tick={{ fill: ct.tick, fontSize: 11 }} />
            <Tooltip contentStyle={tooltipStyle} />
            <Bar dataKey="avg" fill="#d29922" name="Avg Slippage" />
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
