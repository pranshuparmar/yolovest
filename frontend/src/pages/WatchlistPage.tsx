import { useState } from "react";
import { SectorMap } from "../components/SectorMap";
import {
  useWatchlist,
  useSectors,
  useAddWatchlistSymbol,
  useRemoveWatchlistSymbol,
} from "../hooks/queries";

export function WatchlistPage() {
  const { data: watchlist, isLoading: wlLoading } = useWatchlist();
  const { data: sectors, isLoading: secLoading } = useSectors();
  const addSymbol = useAddWatchlistSymbol();
  const removeSymbol = useRemoveWatchlistSymbol();

  const [newSymbol, setNewSymbol] = useState("");
  const [newSector, setNewSector] = useState("");
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);

  const handleAdd = () => {
    const sym = newSymbol.trim().toUpperCase();
    if (!sym) return;
    addSymbol.mutate(
      { symbol: sym, sector: newSector.trim() || undefined },
      {
        onSuccess: () => {
          setNewSymbol("");
          setNewSector("");
        },
      }
    );
  };

  const handleRemove = (symbol: string) => {
    if (confirmRemove === symbol) {
      removeSymbol.mutate(symbol);
      setConfirmRemove(null);
    } else {
      setConfirmRemove(symbol);
      // Auto-clear confirm after 3s
      setTimeout(() => setConfirmRemove(null), 3000);
    }
  };

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Watchlist</h2>

      {/* Add symbol form */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Add Symbol to Watchlist
        </h3>
        <div className="flex flex-wrap gap-3 items-end">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Symbol</label>
            <input
              type="text"
              value={newSymbol}
              onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
              placeholder="e.g. RELIANCE"
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-40 focus:outline-none focus:border-emerald-500"
              onKeyDown={(e) => e.key === "Enter" && handleAdd()}
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">
              Sector (optional)
            </label>
            <input
              type="text"
              value={newSector}
              onChange={(e) => setNewSector(e.target.value)}
              placeholder="e.g. IT"
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-32 focus:outline-none focus:border-emerald-500"
              onKeyDown={(e) => e.key === "Enter" && handleAdd()}
            />
          </div>
          <button
            onClick={handleAdd}
            disabled={!newSymbol.trim() || addSymbol.isPending}
            className="px-4 py-1.5 text-sm bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded transition-colors"
          >
            {addSymbol.isPending ? "Adding..." : "Add"}
          </button>
          {addSymbol.isError && (
            <span className="text-xs text-red-400">
              Failed to add symbol
            </span>
          )}
        </div>
      </div>

      {secLoading ? (
        <div className="h-40 animate-pulse bg-gray-900 rounded-lg" />
      ) : sectors ? (
        <SectorMap data={sectors} />
      ) : null}

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Scored Watchlist
        </h3>
        {wlLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : !watchlist || watchlist.length === 0 ? (
          <p className="text-gray-500 text-sm py-4">
            No symbols in watchlist. Add some above.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-4">Symbol</th>
                  <th className="pb-2 pr-4">Composite</th>
                  <th className="pb-2 pr-4">Technical</th>
                  <th className="pb-2 pr-4">Volume</th>
                  <th className="pb-2 pr-4">Sentiment</th>
                  <th className="pb-2 pr-4">Fundamental</th>
                  <th className="pb-2 pr-4">Sector</th>
                  <th className="pb-2 pr-4">Updated</th>
                  <th className="pb-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {watchlist.map((item) => (
                  <tr
                    key={item.symbol}
                    className="border-b border-gray-800/50 hover:bg-gray-800/30"
                  >
                    <td className="py-2 pr-4 font-medium text-emerald-400">
                      {item.symbol}
                    </td>
                    <td className="py-2 pr-4">
                      {item.composite_score?.toFixed(2) ?? "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-400">
                      {item.technical_score?.toFixed(2) ?? "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-400">
                      {item.volume_momentum_score?.toFixed(2) ?? "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-400">
                      {item.news_sentiment_score?.toFixed(2) ?? "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-400">
                      {item.fundamental_score?.toFixed(2) ?? "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-500 text-xs">
                      {item.sector || "—"}
                    </td>
                    <td className="py-2 pr-4 text-gray-500 text-xs">
                      {item.updated_at
                        ? new Date(item.updated_at).toLocaleDateString("en-IN")
                        : "—"}
                    </td>
                    <td className="py-2">
                      <button
                        onClick={() => handleRemove(item.symbol)}
                        disabled={removeSymbol.isPending}
                        className={
                          confirmRemove === item.symbol
                            ? "px-2 py-0.5 text-xs bg-red-600 hover:bg-red-500 text-white rounded transition-colors"
                            : "px-2 py-0.5 text-xs bg-gray-800 hover:bg-red-900/40 text-gray-400 hover:text-red-400 rounded transition-colors"
                        }
                      >
                        {confirmRemove === item.symbol
                          ? "Confirm?"
                          : "Remove"}
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
