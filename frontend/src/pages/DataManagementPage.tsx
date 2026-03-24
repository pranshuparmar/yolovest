import { useState } from "react";
import { useStorageStats, useCleanupTable } from "../hooks/queries";
import type { TableStats } from "../types/api";

const TABLE_INFO: Record<string, { label: string; description: string; defaultDays: number }> = {
  ohlcv: { label: "OHLCV Candles", description: "Daily and intraday price bars", defaultDays: 730 },
  news_articles: { label: "News Articles", description: "Aggregated news from all sources", defaultDays: 180 },
  economic_events: { label: "Economic Events", description: "RBI MPC, FOMC, earnings dates", defaultDays: 365 },
  audit_log: { label: "Audit Log", description: "Skill execution history", defaultDays: 365 },
  predictions: { label: "Predictions", description: "Signal predictions and outcomes", defaultDays: 365 },
};

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  try {
    return new Date(iso).toLocaleDateString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

function formatNumber(n: number): string {
  return n.toLocaleString("en-IN");
}

function TableRow({
  table,
  stats,
  onCleanup,
  cleanupLoading,
}: {
  table: string;
  stats: TableStats;
  onCleanup: (table: string, days: number) => void;
  cleanupLoading: boolean;
}) {
  const info = TABLE_INFO[table];
  if (!info) return null;

  const [days, setDays] = useState(info.defaultDays);
  const [confirming, setConfirming] = useState(false);

  const handleCleanup = () => {
    if (!confirming) {
      setConfirming(true);
      return;
    }
    onCleanup(table, days);
    setConfirming(false);
  };

  return (
    <tr className="border-b border-gray-800 hover:bg-gray-800/30">
      <td className="py-3 px-4">
        <div className="font-medium text-gray-200">{info.label}</div>
        <div className="text-xs text-gray-500">{info.description}</div>
      </td>
      <td className="py-3 px-4 text-right font-mono text-gray-300">
        {formatNumber(stats.row_count)}
      </td>
      <td className="py-3 px-4 text-center text-sm text-gray-400">
        {formatDate(stats.oldest)}
      </td>
      <td className="py-3 px-4 text-center text-sm text-gray-400">
        {formatDate(stats.newest)}
      </td>
      <td className="py-3 px-4">
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 whitespace-nowrap">Older than</span>
          <input
            type="number"
            min={1}
            value={days}
            onChange={(e) => {
              setDays(Number(e.target.value));
              setConfirming(false);
            }}
            className="w-20 px-2 py-1 text-sm bg-gray-800 border border-gray-700 rounded text-gray-300 text-right"
          />
          <span className="text-xs text-gray-500">days</span>
          <button
            onClick={handleCleanup}
            disabled={cleanupLoading || stats.row_count === 0}
            className={`px-3 py-1 rounded text-sm font-medium disabled:opacity-40 transition-colors whitespace-nowrap ${
              confirming
                ? "bg-red-600 hover:bg-red-700 text-white"
                : "bg-gray-700 hover:bg-gray-600 text-gray-200"
            }`}
          >
            {cleanupLoading ? "..." : confirming ? "Confirm Delete" : "Clean Up"}
          </button>
          {confirming && (
            <button
              onClick={() => setConfirming(false)}
              className="text-xs text-gray-500 hover:text-gray-300"
            >
              Cancel
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

export function DataManagementPage() {
  const { data, isLoading, error } = useStorageStats();
  const cleanup = useCleanupTable();
  const [lastResult, setLastResult] = useState<{ table: string; deleted: number } | null>(null);

  const handleCleanup = (table: string, days: number) => {
    cleanup.mutate(
      { table, older_than_days: days },
      {
        onSuccess: (result) => {
          setLastResult({ table, deleted: result.rows_deleted });
        },
      }
    );
  };

  if (isLoading) {
    return (
      <div className="space-y-4 p-6">
        <div className="h-8 w-48 animate-pulse bg-gray-800 rounded" />
        <div className="h-64 animate-pulse bg-gray-800 rounded" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-6">
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-4 text-red-400">
          Failed to load storage stats.
        </div>
      </div>
    );
  }

  const dbFile = data._db_file;
  const cleanableTables = Object.keys(TABLE_INFO);

  // Read-only tables (trades, agent_memory)
  const readOnlyTables = Object.entries(data)
    .filter(([k]) => !cleanableTables.includes(k) && k !== "_db_file")
    .map(([k, v]) => ({ name: k, stats: v as TableStats }));

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-bold text-gray-100">Data Management</h2>
        <p className="text-sm text-gray-500 mt-1">
          Monitor database storage and clean up old data to free space.
        </p>
      </div>

      {/* DB Size Card */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Database Size</div>
          <div className="text-2xl font-bold text-gray-100 mt-1">
            {formatBytes(dbFile.db_bytes)}
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wide">WAL Size</div>
          <div className="text-2xl font-bold text-gray-100 mt-1">
            {formatBytes(dbFile.wal_bytes)}
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Total on Disk</div>
          <div className="text-2xl font-bold text-emerald-400 mt-1">
            {formatBytes(dbFile.total_bytes)}
          </div>
        </div>
      </div>

      {/* Result Toast */}
      {lastResult && (
        <div className="bg-emerald-900/20 border border-emerald-800 rounded-lg p-3 text-sm text-emerald-400 flex items-center justify-between">
          <span>
            Deleted {formatNumber(lastResult.deleted)} rows from{" "}
            <span className="font-medium">{TABLE_INFO[lastResult.table]?.label ?? lastResult.table}</span>.
          </span>
          <button onClick={() => setLastResult(null)} className="text-emerald-600 hover:text-emerald-400 text-xs">
            Dismiss
          </button>
        </div>
      )}

      {/* Cleanable Tables */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800">
          <h3 className="text-sm font-semibold text-gray-300">Storage by Table</h3>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
              <th className="py-2 px-4 text-left">Table</th>
              <th className="py-2 px-4 text-right">Rows</th>
              <th className="py-2 px-4 text-center">Oldest</th>
              <th className="py-2 px-4 text-center">Newest</th>
              <th className="py-2 px-4 text-left">Cleanup</th>
            </tr>
          </thead>
          <tbody>
            {cleanableTables.map((table) => {
              const stats = data[table] as TableStats;
              if (!stats) return null;
              return (
                <TableRow
                  key={table}
                  table={table}
                  stats={stats}
                  onCleanup={handleCleanup}
                  cleanupLoading={cleanup.isPending}
                />
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Read-only Tables */}
      {readOnlyTables.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-800">
            <h3 className="text-sm font-semibold text-gray-300">Other Tables (read-only)</h3>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                <th className="py-2 px-4 text-left">Table</th>
                <th className="py-2 px-4 text-right">Rows</th>
                <th className="py-2 px-4 text-center">Oldest</th>
                <th className="py-2 px-4 text-center">Newest</th>
              </tr>
            </thead>
            <tbody>
              {readOnlyTables.map(({ name, stats }) => (
                <tr key={name} className="border-b border-gray-800">
                  <td className="py-3 px-4 font-medium text-gray-300">{name}</td>
                  <td className="py-3 px-4 text-right font-mono text-gray-300">
                    {formatNumber(stats.row_count)}
                  </td>
                  <td className="py-3 px-4 text-center text-sm text-gray-400">
                    {formatDate(stats.oldest)}
                  </td>
                  <td className="py-3 px-4 text-center text-sm text-gray-400">
                    {formatDate(stats.newest)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
