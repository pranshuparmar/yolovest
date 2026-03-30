import { useState } from "react";
import { TradesTable } from "../components/TradesTable";
import { CSVExportButton } from "../components/CSVExportButton";
import { useTrades } from "../hooks/queries";

export function TradesPage() {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [symbol, setSymbol] = useState("");
  const [limit, setLimit] = useState(50);
  const [page, setPage] = useState(0);

  const { data, isLoading } = useTrades({
    start: start || undefined,
    end: end || undefined,
    symbol: symbol || undefined,
    limit,
  });

  // Client-side pagination (server returns up to `limit` rows)
  const totalRows = data?.length ?? 0;
  const pageSize = 20;
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
  const paged = data?.slice(page * pageSize, (page + 1) * pageSize) ?? [];

  // Reset page when filters change
  const resetPage = () => setPage(0);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Trade History</h2>
        <CSVExportButton
          data={(data || []) as unknown as Record<string, unknown>[]}
          filename={`trades-${new Date().toISOString().split("T")[0]}`}
        />
      </div>

      <div className="flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Start Date</label>
          <input
            type="date"
            value={start}
            onChange={(e) => { setStart(e.target.value); resetPage(); }}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:outline-none focus:border-emerald-500"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">End Date</label>
          <input
            type="date"
            value={end}
            onChange={(e) => { setEnd(e.target.value); resetPage(); }}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:outline-none focus:border-emerald-500"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Symbol</label>
          <input
            type="text"
            value={symbol}
            onChange={(e) => { setSymbol(e.target.value.toUpperCase()); resetPage(); }}
            placeholder="e.g. RELIANCE"
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:outline-none focus:border-emerald-500 w-full sm:w-36"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Limit</label>
          <select
            value={limit}
            onChange={(e) => { setLimit(Number(e.target.value)); resetPage(); }}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 focus:outline-none focus:border-emerald-500"
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={250}>250</option>
            <option value={500}>500</option>
          </select>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        {isLoading ? (
          <div className="h-40 animate-pulse bg-gray-800 rounded" />
        ) : (
          <TradesTable trades={paged} />
        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between mt-3 pt-3 border-t border-gray-800">
            <span className="text-xs text-gray-500">
              Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, totalRows)} of {totalRows}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage(Math.max(0, page - 1))}
                disabled={page === 0}
                className="px-2 py-1 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                Prev
              </button>
              <span className="text-xs text-gray-400">
                {page + 1} / {totalPages}
              </span>
              <button
                onClick={() => setPage(Math.min(totalPages - 1, page + 1))}
                disabled={page >= totalPages - 1}
                className="px-2 py-1 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
