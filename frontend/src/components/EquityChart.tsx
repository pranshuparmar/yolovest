import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { useEquityCurve } from "../hooks/queries";

export function EquityChart({ days = 30 }: { days?: number }) {
  const { data, isLoading } = useEquityCurve(days);

  if (isLoading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-64 animate-pulse" />
    );
  }

  if (!data || data.length === 0) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-64 flex items-center justify-center text-gray-500">
        No equity data available
      </div>
    );
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-4">
        Equity Curve ({days}d)
      </h3>
      <ResponsiveContainer width="100%" height={240}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#353847" />
          <XAxis
            dataKey="date"
            tick={{ fill: "#908e96", fontSize: 11 }}
            tickFormatter={(v: string) => v.slice(5)}
          />
          <YAxis tick={{ fill: "#908e96", fontSize: 11 }} />
          <Tooltip
            contentStyle={{
              backgroundColor: "#2a2c37",
              border: "1px solid #353847",
              borderRadius: 8,
              color: "#d5d3cd",
            }}
          />
          <Line
            type="monotone"
            dataKey="cumulative_pnl"
            stroke="#a4cc78"
            strokeWidth={2}
            dot={false}
            name="Cumulative PnL"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
