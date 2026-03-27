import { useState } from "react";
import {
  useStorageStats,
  useCleanupTable,
  useBackups,
  useCreateBackup,
  useRestoreBackup,
  useResetAllData,
} from "../hooks/queries";
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
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

function formatDateTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
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
  const { data: backups } = useBackups();
  const cleanup = useCleanupTable();
  const createBackup = useCreateBackup();
  const restoreBackup = useRestoreBackup();
  const resetAll = useResetAllData();
  const [lastResult, setLastResult] = useState<string | null>(null);
  const [resetStep, setResetStep] = useState<"idle" | "warn" | "confirm">("idle");
  const [restoreConfirm, setRestoreConfirm] = useState<string | null>(null);

  const handleCleanup = (table: string, days: number) => {
    cleanup.mutate(
      { table, older_than_days: days },
      {
        onSuccess: (result) => {
          setLastResult(
            `Deleted ${formatNumber(result.rows_deleted)} rows from ${TABLE_INFO[result.table]?.label ?? result.table}.`
          );
        },
      }
    );
  };

  const handleBackup = () => {
    createBackup.mutate(undefined, {
      onSuccess: (result) => {
        setLastResult(`Backup created: ${result.backup_path.split("/").pop()}`);
      },
    });
  };

  const handleReset = () => {
    if (resetStep === "idle") {
      setResetStep("warn");
      return;
    }
    if (resetStep === "warn") {
      setResetStep("confirm");
      return;
    }
    // Final confirm
    resetAll.mutate(undefined, {
      onSuccess: (result) => {
        setLastResult(
          `Reset complete: deleted ${formatNumber(result.total_rows_deleted)} rows across all tables.`
        );
        setResetStep("idle");
      },
      onError: () => {
        setResetStep("idle");
      },
    });
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
          Monitor database storage, manage backups, and clean up old data.
        </p>
      </div>

      {/* DB Size Cards */}
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
          <span>{lastResult}</span>
          <button onClick={() => setLastResult(null)} className="text-blue-500 hover:text-blue-400 text-xs">
            Dismiss
          </button>
        </div>
      )}

      {/* Backups Section */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-gray-300">Backups</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Automatic daily backups at 6 PM IST. Create a manual backup anytime.
            </p>
          </div>
          <button
            onClick={handleBackup}
            disabled={createBackup.isPending}
            className="px-3 py-1.5 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors shrink-0"
          >
            {createBackup.isPending ? "Creating..." : "Create Backup Now"}
          </button>
        </div>
        {backups && backups.length > 0 ? (
          <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                <th className="py-2 px-4 text-left">Filename</th>
                <th className="py-2 px-4 text-right">Size</th>
                <th className="py-2 px-4 text-right">Created</th>
                <th className="py-2 px-4 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {backups.map((b) => (
                <tr key={b.filename} className="border-b border-gray-800 hover:bg-gray-800/30">
                  <td className="py-2 px-4 font-mono text-gray-300 text-xs">{b.filename}</td>
                  <td className="py-2 px-4 text-right text-gray-400">{formatBytes(b.size_bytes)}</td>
                  <td className="py-2 px-4 text-right text-gray-400">{formatDateTime(b.created_at)}</td>
                  <td className="py-2 px-4 text-right">
                    {restoreConfirm === b.filename ? (
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={() => {
                            restoreBackup.mutate(b.filename, {
                              onSuccess: (result) => {
                                setLastResult(
                                  `Restored from ${b.filename}${result.models_restored ? ` (${result.models_restored} models restored)` : ""}. Please restart the server.`
                                );
                                setRestoreConfirm(null);
                              },
                            });
                          }}
                          disabled={restoreBackup.isPending}
                          className="px-2 py-0.5 rounded text-xs font-medium bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-50"
                        >
                          {restoreBackup.isPending ? "Restoring..." : "Confirm"}
                        </button>
                        <button
                          onClick={() => setRestoreConfirm(null)}
                          className="text-xs text-gray-500 hover:text-gray-300"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setRestoreConfirm(b.filename)}
                        className="px-2 py-0.5 rounded text-xs font-medium bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
                      >
                        Restore
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        ) : (
          <div className="px-4 py-6 text-center text-sm text-gray-500">
            No backups found. Create one before performing destructive operations.
          </div>
        )}
      </div>

      {/* Cleanable Tables */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-800">
          <h3 className="text-sm font-semibold text-gray-300">Storage by Table</h3>
        </div>
        <div className="overflow-x-auto">
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
      </div>

      {/* Read-only Tables */}
      {readOnlyTables.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-800">
            <h3 className="text-sm font-semibold text-gray-300">Other Tables (read-only)</h3>
          </div>
          <div className="overflow-x-auto">
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
        </div>
      )}

      {/* Factory Reset - Danger Zone */}
      <div className="bg-gray-900 border border-red-900/50 rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-red-900/50">
          <h3 className="text-sm font-semibold text-red-400">Danger Zone</h3>
        </div>
        <div className="p-4 space-y-4">
          <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
            <div>
              <div className="font-medium text-gray-200">Factory Reset</div>
              <p className="text-xs text-gray-500 mt-1 max-w-lg">
                Delete <span className="text-red-400 font-medium">ALL data</span> from every table and start from zero.
                This includes trades, positions, predictions, news, OHLCV history, audit logs, and agent memory.
                The database schema and migrations are preserved — the app will rebuild data from scratch on the next heartbeat.
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {resetStep !== "idle" && (
                <button
                  onClick={() => setResetStep("idle")}
                  className="px-3 py-1.5 rounded text-sm text-gray-400 hover:text-gray-200"
                >
                  Cancel
                </button>
              )}
              <button
                onClick={handleReset}
                disabled={resetAll.isPending}
                className={`px-4 py-1.5 rounded text-sm font-medium transition-colors disabled:opacity-50 ${
                  resetStep === "confirm"
                    ? "bg-red-600 hover:bg-red-700 text-white animate-pulse"
                    : resetStep === "warn"
                      ? "bg-red-700 hover:bg-red-800 text-white"
                      : "bg-gray-700 hover:bg-gray-600 text-gray-200 border border-red-900/50"
                }`}
              >
                {resetAll.isPending
                  ? "Resetting..."
                  : resetStep === "confirm"
                    ? "I understand, delete everything"
                    : resetStep === "warn"
                      ? "Are you sure?"
                      : "Reset All Data"}
              </button>
            </div>
          </div>

          {/* Warning banner shown during reset flow */}
          {resetStep === "warn" && (
            <div className="bg-amber-900/20 border border-amber-800 rounded-lg p-3 text-sm text-amber-400">
              <span className="font-semibold">Create a backup first!</span> Use the "Create Backup Now" button above
              before proceeding. This action is irreversible.
            </div>
          )}
          {resetStep === "confirm" && (
            <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-sm text-red-400">
              <span className="font-semibold">Final warning:</span> This will permanently delete all data.
              The app will start fresh on the next heartbeat cycle. Click "I understand, delete everything" to proceed.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
