import { useRiskExposure } from "../hooks/queries";
import {
  PieChart,
  Pie,
  Cell,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import { useTooltipStyle } from "../hooks/useChartTheme";

const COLORS = [
  "#58a6ff", // blue
  "#3fb950", // green
  "#d29922", // amber
  "#f85149", // coral
  "#bc8cff", // purple
  "#f0883e", // orange
  "#39d2c0", // teal
  "#f778ba", // pink
  "#79c0ff", // light blue
  "#56d364", // light green
];

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function RiskExposureChart() {
  const { data, isLoading } = useRiskExposure();
  const tooltipStyle = useTooltipStyle();

  if (isLoading || !data) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 h-80 animate-pulse" />
    );
  }

  const sectorData = Object.entries(data.sector_exposure_pct)
    .filter(([, v]) => v > 0)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value);

  const stockData = Object.entries(data.stock_exposures)
    .filter(([, v]) => v > 0)
    .map(([name, value]) => ({ name, value: Number((value * 100).toFixed(2)) }))
    .sort((a, b) => b.value - a.value);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-medium text-gray-400 mb-4">
        Risk Exposure Breakdown
      </h3>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
        <div className="bg-gray-800/50 rounded p-3">
          <p className="text-xs text-gray-500">Total Exposure</p>
          <p className="text-lg font-semibold text-gray-100">
            {fmt(data.exposure_pct * 100, 1)}%
          </p>
        </div>
        <div className="bg-gray-800/50 rounded p-3">
          <p className="text-xs text-gray-500">Open Positions</p>
          <p className="text-lg font-semibold text-gray-100">
            {data.positions_count}
          </p>
        </div>
        <div className="bg-gray-800/50 rounded p-3">
          <p className="text-xs text-gray-500">Sectors Exposed</p>
          <p className="text-lg font-semibold text-gray-100">
            {sectorData.length}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Sector pie chart */}
        <div>
          <p className="text-xs text-gray-500 mb-2">By Sector</p>
          {sectorData.length === 0 ? (
            <p className="text-gray-500 text-sm py-8 text-center">
              No sector exposure
            </p>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie
                  data={sectorData}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  outerRadius={80}
                  label={({ name, value }) => `${name}: ${value}%`}
                  labelLine={false}
                >
                  {sectorData.map((_, i) => (
                    <Cell
                      key={i}
                      fill={COLORS[i % COLORS.length]}
                      stroke="transparent"
                    />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={tooltipStyle}
                  formatter={(value: number) => `${value}%`}
                />
              </PieChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Stock exposure bar list */}
        <div>
          <p className="text-xs text-gray-500 mb-2">By Stock</p>
          {stockData.length === 0 ? (
            <p className="text-gray-500 text-sm py-8 text-center">
              No stock exposure
            </p>
          ) : (
            <div className="space-y-2 max-h-56 overflow-y-auto">
              {stockData.map((s, i) => (
                <div key={s.name} className="flex items-center gap-2">
                  <span className="text-xs text-gray-400 w-20 shrink-0 truncate">
                    {s.name}
                  </span>
                  <div className="flex-1 h-4 bg-gray-800 rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${Math.min(s.value * 2, 100)}%`,
                        backgroundColor: COLORS[i % COLORS.length],
                      }}
                    />
                  </div>
                  <span className="text-xs text-gray-400 w-12 text-right">
                    {s.value}%
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
