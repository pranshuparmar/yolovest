import { useState } from "react";
import { useAlerts, useCreateAlert, useDeleteAlert } from "../hooks/queries";
import { parseUTC, getTimezone } from "../utils/datetime";
import { SymbolLink } from "../components/SymbolLink";
import clsx from "clsx";

function fmt(n: number, d = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function AlertsPage() {
  const [showAll, setShowAll] = useState(false);
  const { data: alerts, isLoading } = useAlerts(!showAll);
  const createAlert = useCreateAlert();
  const deleteAlert = useDeleteAlert();

  const [symbol, setSymbol] = useState("");
  const [price, setPrice] = useState("");
  const [direction, setDirection] = useState<"above" | "below">("above");
  const [note, setNote] = useState("");

  const handleCreate = () => {
    const sym = symbol.trim().toUpperCase();
    const p = parseFloat(price);
    if (!sym || isNaN(p)) return;
    createAlert.mutate(
      { symbol: sym, target_price: p, direction, note: note.trim() || undefined },
      { onSuccess: () => { setSymbol(""); setPrice(""); setNote(""); } }
    );
  };

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Price Alerts</h2>

      {/* Create form */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Create Alert</h3>
        <div className="flex flex-wrap gap-3 items-end">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Symbol</label>
            <input type="text" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="RELIANCE" className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-32" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Direction</label>
            <select value={direction} onChange={(e) => setDirection(e.target.value as "above" | "below")}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100">
              <option value="above">Goes above</option>
              <option value="below">Goes below</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Price</label>
            <input type="number" step="0.05" value={price} onChange={(e) => setPrice(e.target.value)}
              placeholder="2500.00" className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-32" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Note</label>
            <input type="text" value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="Optional note..." className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-48" />
          </div>
          <button onClick={handleCreate} disabled={!symbol.trim() || !price || createAlert.isPending}
            className="px-4 py-1.5 text-sm bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded transition-colors">
            {createAlert.isPending ? "Creating..." : "Create Alert"}
          </button>
        </div>
      </div>

      {/* Alerts list */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-medium text-gray-400">
            {showAll ? "All Alerts" : "Active Alerts"}
          </h3>
          <button onClick={() => setShowAll(!showAll)} className="text-xs text-gray-500 hover:text-gray-300">
            {showAll ? "Show active only" : "Show all"}
          </button>
        </div>
        {isLoading ? (
          <div className="h-32 animate-pulse bg-gray-800 rounded" />
        ) : !alerts || alerts.length === 0 ? (
          <p className="text-gray-500 text-sm py-4">No alerts</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-4">Symbol</th>
                  <th className="pb-2 pr-4">Condition</th>
                  <th className="pb-2 pr-4">Target</th>
                  <th className="pb-2 pr-4">Note</th>
                  <th className="pb-2 pr-4">Status</th>
                  <th className="pb-2 pr-4">Created</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {alerts.map((a) => (
                  <tr key={a.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="py-2 pr-4 font-medium text-emerald-400">
                      <SymbolLink symbol={a.symbol} className="text-emerald-400" />
                    </td>
                    <td className="py-2 pr-4">
                      <span className={clsx("px-1.5 py-0.5 rounded text-xs",
                        a.direction === "above" ? "bg-emerald-900/40 text-emerald-400" : "bg-red-900/40 text-red-400"
                      )}>{a.direction}</span>
                    </td>
                    <td className="py-2 pr-4">₹{fmt(a.target_price)}</td>
                    <td className="py-2 pr-4 text-gray-400 text-xs max-w-xs truncate">{a.note || "—"}</td>
                    <td className="py-2 pr-4">
                      {a.active ? (
                        <span className="text-emerald-400 text-xs">Active</span>
                      ) : a.triggered_at ? (
                        <span className="text-amber-400 text-xs">Triggered {parseUTC(a.triggered_at).toLocaleDateString("en-IN", { timeZone: getTimezone() })}</span>
                      ) : (
                        <span className="text-gray-500 text-xs">Deleted</span>
                      )}
                    </td>
                    <td className="py-2 pr-4 text-gray-500 text-xs">{parseUTC(a.created_at).toLocaleDateString("en-IN", { timeZone: getTimezone() })}</td>
                    <td className="py-2">
                      <button onClick={() => deleteAlert.mutate(a.id)}
                        className="px-2 py-0.5 text-xs bg-gray-800 hover:bg-red-900/40 text-gray-400 hover:text-red-400 rounded transition-colors">
                        Delete
                      </button>
                    </td>
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
