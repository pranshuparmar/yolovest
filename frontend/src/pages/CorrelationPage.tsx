import { useState } from "react";
import { useCorrelations } from "../hooks/queries";
import { useTheme } from "../hooks/useTheme";
import clsx from "clsx";

function corrColor(v: number, light: boolean): string {
  if (light) {
    if (v >= 0.7) return "bg-emerald-600 text-white font-semibold";
    if (v >= 0.4) return "bg-emerald-200 text-emerald-900";
    if (v >= 0.1) return "bg-emerald-50 text-emerald-800";
    if (v > -0.1) return "bg-gray-100 text-gray-500";
    if (v > -0.4) return "bg-red-50 text-red-800";
    if (v > -0.7) return "bg-red-200 text-red-900";
    return "bg-red-600 text-white font-semibold";
  }
  // dark
  if (v >= 0.7) return "bg-emerald-500 text-white font-semibold";
  if (v >= 0.4) return "bg-emerald-700/80 text-emerald-100";
  if (v >= 0.1) return "bg-emerald-900/40 text-emerald-300";
  if (v > -0.1) return "bg-gray-700/50 text-gray-400";
  if (v > -0.4) return "bg-red-900/40 text-red-300";
  if (v > -0.7) return "bg-red-700/80 text-red-100";
  return "bg-red-500 text-white font-semibold";
}

export function CorrelationPage() {
  const [days, setDays] = useState(60);
  const { data, isLoading } = useCorrelations(days);
  const { theme } = useTheme();
  const isLight = theme === "light";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Correlation Heatmap</h2>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}
          className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100">
          <option value={30}>30 days</option>
          <option value={60}>60 days</option>
          <option value={90}>90 days</option>
          <option value={180}>180 days</option>
        </select>
      </div>

      <p className="text-sm text-gray-400">
        Pearson correlation of daily returns between portfolio positions and top watchlist symbols.
        High correlation means positions move together — reducing effective diversification.
      </p>

      {/* Legend */}
      <div className="flex gap-2 text-xs flex-wrap">
        <span className="flex items-center gap-1"><span className={clsx("w-4 h-4 rounded", isLight ? "bg-emerald-600" : "bg-emerald-500")} /> Strong +</span>
        <span className="flex items-center gap-1"><span className={clsx("w-4 h-4 rounded", isLight ? "bg-emerald-200" : "bg-emerald-700/80")} /> Moderate +</span>
        <span className="flex items-center gap-1"><span className={clsx("w-4 h-4 rounded border", isLight ? "bg-gray-100 border-gray-300" : "bg-gray-700/50 border-gray-600")} /> Weak</span>
        <span className="flex items-center gap-1"><span className={clsx("w-4 h-4 rounded", isLight ? "bg-red-200" : "bg-red-700/80")} /> Moderate -</span>
        <span className="flex items-center gap-1"><span className={clsx("w-4 h-4 rounded", isLight ? "bg-red-600" : "bg-red-500")} /> Strong -</span>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 overflow-x-auto">
        {isLoading ? (
          <div className="h-64 animate-pulse bg-gray-800 rounded" />
        ) : !data || data.symbols.length < 2 ? (
          <p className="text-gray-500 text-sm py-8 text-center">
            Need at least 2 symbols with OHLCV data to compute correlations.
            Add symbols to watchlist and ensure market data is ingested.
          </p>
        ) : (
          <div className="overflow-x-auto">
          <table className="text-xs">
            <thead>
              <tr>
                <th className="p-2" />
                {data.symbols.map((s) => (
                  <th key={s} className="p-2 text-gray-400 font-medium" style={{ writingMode: "vertical-rl", transform: "rotate(180deg)", height: "80px" }}>
                    {s}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.symbols.map((s1, i) => (
                <tr key={s1}>
                  <td className="p-2 text-gray-400 font-medium text-right pr-3 whitespace-nowrap">{s1}</td>
                  {data.matrix[i].map((v, j) => (
                    <td key={j} className={clsx("p-2 text-center font-mono min-w-[48px]", corrColor(v, isLight))}>
                      {i === j ? "1.00" : v.toFixed(2)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>
    </div>
  );
}
