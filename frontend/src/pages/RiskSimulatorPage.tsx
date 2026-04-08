import { useState } from "react";
import { useRiskSimulator } from "../hooks/queries";
import clsx from "clsx";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function RiskSimulatorPage() {
  const [maxExposure, setMaxExposure] = useState(0.4);
  const [maxSingleStock, setMaxSingleStock] = useState(0.1);
  const [maxPositions, setMaxPositions] = useState(10);
  const [initialCapital, setInitialCapital] = useState(100000);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const simulate = useRiskSimulator();

  const handleRun = () => {
    simulate.mutate({
      max_exposure_pct: maxExposure,
      max_single_stock_pct: maxSingleStock,
      max_positions: maxPositions,
      initial_capital: initialCapital,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
    });
  };

  const r = simulate.data?.results;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Risk Simulator</h2>
      <p className="text-sm text-gray-400">
        Replay historical signals against modified risk parameters to see how outcomes would change.
      </p>

      {/* Parameters */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-4">Parameters</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4 mb-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1">From Date</label>
            <input type="date" value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">To Date</label>
            <input type="date" value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full" />
          </div>
          <div className="flex items-end">
            <p className="text-xs text-gray-500">Leave empty for all available history</p>
          </div>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Max Exposure %</label>
            <input type="range" min="0.1" max="1" step="0.05" value={maxExposure}
              onChange={(e) => setMaxExposure(parseFloat(e.target.value))}
              className="w-full accent-emerald-500" />
            <span className="text-sm text-gray-300">{(maxExposure * 100).toFixed(0)}%</span>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Max Single Stock %</label>
            <input type="range" min="0.02" max="0.5" step="0.02" value={maxSingleStock}
              onChange={(e) => setMaxSingleStock(parseFloat(e.target.value))}
              className="w-full accent-emerald-500" />
            <span className="text-sm text-gray-300">{(maxSingleStock * 100).toFixed(0)}%</span>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Max Positions</label>
            <input type="range" min="1" max="30" step="1" value={maxPositions}
              onChange={(e) => setMaxPositions(parseInt(e.target.value))}
              className="w-full accent-emerald-500" />
            <span className="text-sm text-gray-300">{maxPositions}</span>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Initial Capital</label>
            <input type="number" step="10000" value={initialCapital}
              onChange={(e) => setInitialCapital(parseInt(e.target.value) || 100000)}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full" />
          </div>
        </div>
        <button onClick={handleRun} disabled={simulate.isPending}
          className="mt-4 px-6 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-700 text-white rounded text-sm font-medium transition-colors">
          {simulate.isPending ? "Running..." : "Run Simulation"}
        </button>
      </div>

      {/* Results */}
      {r && (
        <div className="space-y-4">
          <p className="text-xs text-gray-500">
            Simulated against {simulate.data?.signals_available ?? "?"} historical signals
            {simulate.data?.params.date_from && (
              <> from {simulate.data.params.date_from}</>
            )}
            {simulate.data?.params.date_to && (
              <> to {simulate.data.params.date_to}</>
            )}
          </p>
          {r.trades_taken === 0 && r.trades_skipped === 0 && (simulate.data?.signals_without_pnl ?? 0) > 0 && (
            <div className="bg-amber-900/30 border border-amber-700 rounded-lg p-3">
              <p className="text-sm text-amber-400">
                {simulate.data?.signals_without_pnl} signal(s) found but none have trade outcome data (PnL).
                Signals need to be executed and positions closed before they appear in simulation results.
              </p>
            </div>
          )}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">Final Capital</p>
              <p className={clsx("text-xl font-semibold", r.final_capital >= initialCapital ? "text-emerald-400" : "text-red-400")}>
                ₹{fmt(r.final_capital, 0)}
              </p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">Return</p>
              <p className={clsx("text-xl font-semibold", r.return_pct >= 0 ? "text-emerald-400" : "text-red-400")}>
                {r.return_pct >= 0 ? "+" : ""}{fmt(r.return_pct, 1)}%
              </p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">Total PnL</p>
              <p className={clsx("text-xl font-semibold", r.total_pnl >= 0 ? "text-emerald-400" : "text-red-400")}>
                ₹{fmt(r.total_pnl, 0)}
              </p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">Max Drawdown</p>
              <p className="text-xl font-semibold text-red-400">{fmt(r.max_drawdown_pct, 1)}%</p>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">Trades Taken</p>
              <p className="text-xl font-semibold">{r.trades_taken}</p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">Trades Skipped</p>
              <p className="text-xl font-semibold text-amber-400">{r.trades_skipped}</p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">Win Rate</p>
              <p className={clsx("text-xl font-semibold", r.win_rate >= 0.5 ? "text-emerald-400" : "text-red-400")}>
                {fmt(r.win_rate * 100, 1)}%
              </p>
            </div>
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <p className="text-xs text-gray-500">W / L</p>
              <p className="text-xl font-semibold">
                <span className="text-emerald-400">{r.wins}</span>
                {" / "}
                <span className="text-red-400">{r.losses}</span>
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
