import { useState } from "react";
import { SectorMap } from "../components/SectorMap";
import { Pagination } from "../components/Pagination";
import { SymbolLink } from "../components/SymbolLink";
import clsx from "clsx";
import {
  useWatchlist,
  useUserWatchlist,
  useSectors,
  useAddUserWatchlistSymbol,
  useRemoveUserWatchlistSymbol,
  useUniverseSymbols,
} from "../hooks/queries";
import { parseUTC, getTimezone } from "../utils/datetime";

function score(n: number | null | undefined) {
  return n != null ? n.toFixed(2) : "--";
}

export function WatchlistPage() {
  const { data: algoWatchlist, isLoading: algoLoading } = useWatchlist();
  const { data: userWatchlist, isLoading: userLoading } = useUserWatchlist();
  const { data: sectors, isLoading: secLoading } = useSectors();
  const { data: allSymbols } = useUniverseSymbols();
  const addSymbol = useAddUserWatchlistSymbol();
  const removeSymbol = useRemoveUserWatchlistSymbol();

  const [newSymbol, setNewSymbol] = useState("");
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [newSector, setNewSector] = useState("");
  const [newNotes, setNewNotes] = useState("");
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"user" | "algo">("user");
  const [userPage, setUserPage] = useState(0);
  const [algoPage, setAlgoPage] = useState(0);
  const pageSize = 20;
  const pagedUser = (userWatchlist ?? []).slice(
    userPage * pageSize, (userPage + 1) * pageSize,
  );
  const pagedAlgo = (algoWatchlist ?? []).slice(
    algoPage * pageSize, (algoPage + 1) * pageSize,
  );
  const algoOffset = algoPage * pageSize;

  const handleAdd = () => {
    const sym = newSymbol.trim().toUpperCase();
    if (!sym) return;
    addSymbol.mutate(
      { symbol: sym, sector: newSector.trim() || undefined, notes: newNotes.trim() || undefined },
      {
        onSuccess: () => {
          setNewSymbol("");
          setNewSector("");
          setNewNotes("");
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
      setTimeout(() => setConfirmRemove(null), 3000);
    }
  };

  // Check which user symbols are also in the algo list
  const algoSymbols = new Set((algoWatchlist ?? []).map((i) => i.symbol));

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold">Watchlist</h2>
        <p className="text-sm text-gray-500 mt-1">
          Your watchlist symbols are always included in signal generation. The algorithm independently
          maintains its own shortlist from the full NSE universe.
        </p>
      </div>

      {/* Add symbol form */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Add to Your Watchlist
        </h3>
        <div className="flex flex-wrap gap-3 items-end">
          <div className="relative">
            <label className="block text-xs text-gray-500 mb-1">Symbol</label>
            <input
              type="text"
              value={newSymbol}
              onChange={(e) => {
                setNewSymbol(e.target.value.toUpperCase());
                setShowSuggestions(true);
              }}
              onFocus={() => setShowSuggestions(true)}
              onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
              placeholder="Search NSE symbols..."
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-48 focus:outline-none focus:border-emerald-500"
              onKeyDown={(e) => {
                if (e.key === "Enter") { setShowSuggestions(false); handleAdd(); }
                if (e.key === "Escape") setShowSuggestions(false);
              }}
            />
            {showSuggestions && newSymbol.length >= 1 && allSymbols && (
              <div className="absolute z-50 mt-1 w-full max-h-48 overflow-auto bg-gray-800 border border-gray-700 rounded shadow-lg">
                {allSymbols
                  .filter((s) => s.includes(newSymbol))
                  .slice(0, 12)
                  .map((s) => (
                    <button
                      key={s}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => {
                        setNewSymbol(s);
                        setShowSuggestions(false);
                      }}
                      className="w-full text-left px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 transition-colors"
                    >
                      {s}
                    </button>
                  ))}
              </div>
            )}
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Sector</label>
            <input
              type="text"
              value={newSector}
              onChange={(e) => setNewSector(e.target.value)}
              placeholder="e.g. IT"
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-24 focus:outline-none focus:border-emerald-500"
              onKeyDown={(e) => e.key === "Enter" && handleAdd()}
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Notes</label>
            <input
              type="text"
              value={newNotes}
              onChange={(e) => setNewNotes(e.target.value)}
              placeholder="Why tracking?"
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-100 w-full sm:w-44 focus:outline-none focus:border-emerald-500"
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
            <span className="text-xs text-red-400">Failed to add symbol</span>
          )}
        </div>
      </div>

      {/* Tab switcher */}
      <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
        <button
          onClick={() => setActiveTab("user")}
          className={clsx(
            "px-4 py-1.5 rounded text-sm font-medium transition-colors",
            activeTab === "user"
              ? "bg-emerald-600 text-white"
              : "text-gray-400 hover:text-gray-200"
          )}
        >
          Your Watchlist
          {userWatchlist && (
            <span className="ml-1.5 text-xs opacity-70">({userWatchlist.length})</span>
          )}
        </button>
        <button
          onClick={() => setActiveTab("algo")}
          className={clsx(
            "px-4 py-1.5 rounded text-sm font-medium transition-colors",
            activeTab === "algo"
              ? "bg-blue-600 text-white"
              : "text-gray-400 hover:text-gray-200"
          )}
        >
          Algorithm Shortlist
          {algoWatchlist && (
            <span className="ml-1.5 text-xs opacity-70">({algoWatchlist.length})</span>
          )}
        </button>
      </div>

      {/* User Watchlist Tab */}
      {activeTab === "user" && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="flex items-center gap-2 mb-3">
            <h3 className="text-sm font-medium text-gray-300">Your Watchlist</h3>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-400 font-medium">
              Always included in signals
            </span>
          </div>
          {userLoading ? (
            <div className="h-32 animate-pulse bg-gray-800 rounded" />
          ) : !userWatchlist || userWatchlist.length === 0 ? (
            <p className="text-gray-500 text-sm py-4">
              No symbols in your watchlist. Add some above — they'll always be
              included in signal generation, even if the algorithm doesn't rank them.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                    <th className="pb-2 pr-3">Symbol</th>
                    <th className="pb-2 pr-3">Composite</th>
                    <th className="pb-2 pr-3">Technical</th>
                    <th className="pb-2 pr-3">Volume</th>
                    <th className="pb-2 pr-3">Sentiment</th>
                    <th className="pb-2 pr-3">Fundamental</th>
                    <th className="pb-2 pr-3">Notes</th>
                    <th className="pb-2 pr-3">In Algo?</th>
                    <th className="pb-2">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {pagedUser.map((item) => (
                    <tr
                      key={item.symbol}
                      className="border-b border-gray-800/50 hover:bg-gray-800/30"
                    >
                      <td className="py-2 pr-3 font-medium text-emerald-400">
                        <SymbolLink symbol={item.symbol} className="text-emerald-400" />
                      </td>
                      <td className="py-2 pr-3 text-gray-300">
                        {score(item.composite_score)}
                      </td>
                      <td className="py-2 pr-3 text-gray-400">
                        {score(item.technical_score)}
                      </td>
                      <td className="py-2 pr-3 text-gray-400">
                        {score(item.volume_momentum_score)}
                      </td>
                      <td className="py-2 pr-3 text-gray-400">
                        {score(item.news_sentiment_score)}
                      </td>
                      <td className="py-2 pr-3 text-gray-400">
                        {score(item.fundamental_score)}
                      </td>
                      <td className="py-2 pr-3 text-gray-500 text-xs max-w-[160px] truncate">
                        {item.notes || "--"}
                      </td>
                      <td className="py-2 pr-3 text-center">
                        {algoSymbols.has(item.symbol) ? (
                          <span className="text-xs text-blue-400">Yes</span>
                        ) : (
                          <span className="text-xs text-gray-600">No</span>
                        )}
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
                          {confirmRemove === item.symbol ? "Confirm?" : "Remove"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <Pagination
                total={userWatchlist.length}
                page={userPage}
                pageSize={pageSize}
                onPageChange={setUserPage}
              />
            </div>
          )}
        </div>
      )}

      {/* Algorithmic Watchlist Tab */}
      {activeTab === "algo" && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="flex items-center gap-2 mb-3">
            <h3 className="text-sm font-medium text-gray-300">Algorithm Shortlist</h3>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/40 text-blue-400 font-medium">
              Auto-refreshed every 15 min during market hours
            </span>
          </div>
          <p className="text-xs text-gray-500 mb-3">
            Top stocks ranked by composite score (technical, volume, sentiment, fundamental).
            This list is fully autonomous — you cannot add or remove from it.
          </p>
          {algoLoading ? (
            <div className="h-32 animate-pulse bg-gray-800 rounded" />
          ) : !algoWatchlist || algoWatchlist.length === 0 ? (
            <p className="text-gray-500 text-sm py-4">
              Algorithmic shortlist is empty. It will populate during the next market-scan heartbeat.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                    <th className="pb-2 pr-3">#</th>
                    <th className="pb-2 pr-3">Symbol</th>
                    <th className="pb-2 pr-3">Composite</th>
                    <th className="pb-2 pr-3">Technical</th>
                    <th className="pb-2 pr-3">Volume</th>
                    <th className="pb-2 pr-3">Sentiment</th>
                    <th className="pb-2 pr-3">Fundamental</th>
                    <th className="pb-2 pr-3">Sector</th>
                    <th className="pb-2">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {pagedAlgo.map((item, idx) => {
                    const inUser = (userWatchlist ?? []).some((u) => u.symbol === item.symbol);
                    return (
                      <tr
                        key={item.symbol}
                        className={clsx(
                          "border-b border-gray-800/50 hover:bg-gray-800/30",
                          inUser && "bg-emerald-900/10"
                        )}
                      >
                        <td className="py-2 pr-3 text-gray-600 text-xs">
                          {algoOffset + idx + 1}
                        </td>
                        <td className="py-2 pr-3 font-medium">
                          <SymbolLink symbol={item.symbol} className="text-blue-400" />
                          {inUser && (
                            <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-emerald-900/30 text-emerald-500">
                              yours
                            </span>
                          )}
                        </td>
                        <td className="py-2 pr-3 text-gray-300 font-medium">
                          {score(item.composite_score)}
                        </td>
                        <td className="py-2 pr-3 text-gray-400">
                          {score(item.technical_score)}
                        </td>
                        <td className="py-2 pr-3 text-gray-400">
                          {score(item.volume_momentum_score)}
                        </td>
                        <td className="py-2 pr-3 text-gray-400">
                          {score(item.news_sentiment_score)}
                        </td>
                        <td className="py-2 pr-3 text-gray-400">
                          {score(item.fundamental_score)}
                        </td>
                        <td className="py-2 pr-3 text-gray-500 text-xs">
                          {item.sector || "--"}
                        </td>
                        <td className="py-2 text-gray-500 text-xs">
                          {item.updated_at
                            ? parseUTC(item.updated_at).toLocaleDateString("en-IN", { timeZone: getTimezone() })
                            : "--"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <Pagination
                total={algoWatchlist.length}
                page={algoPage}
                pageSize={pageSize}
                onPageChange={setAlgoPage}
              />
            </div>
          )}
        </div>
      )}

      {/* Sector map */}
      {secLoading ? (
        <div className="h-40 animate-pulse bg-gray-900 rounded-lg" />
      ) : sectors ? (
        <SectorMap data={sectors} />
      ) : null}
    </div>
  );
}
