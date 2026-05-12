import { useState } from "react";
import {
  useStorageStats,
  useCleanupTable,
  useBackups,
  useCreateBackup,
  useRestoreBackup,
  useResetAllData,
  useQuarantinedSymbols,
  useUnquarantineSymbol,
  useSetReplacementSymbol,
  useBulkDelete,
} from "../hooks/queries";
import type { TableStats } from "../types/api";
import { parseUTC, getTimezone } from "../utils/datetime";

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
    return parseUTC(iso).toLocaleDateString("en-IN", {
      timeZone: getTimezone(),
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
    return parseUTC(iso).toLocaleString("en-IN", {
      timeZone: getTimezone(),
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

function ReplacementInput({ symbol, current }: { symbol: string; current: string | null }) {
  const [value, setValue] = useState(current ?? "");
  const [dirty, setDirty] = useState(false);
  const setReplacement = useSetReplacementSymbol();

  const save = () => {
    const trimmed = value.trim().toUpperCase();
    setReplacement.mutate(
      { symbol, replacement: trimmed || null },
      { onSuccess: () => setDirty(false) },
    );
  };

  return (
    <div className="flex items-center gap-1">
      <input
        type="text"
        value={value}
        onChange={(e) => { setValue(e.target.value); setDirty(true); }}
        onKeyDown={(e) => { if (e.key === "Enter") save(); }}
        placeholder="e.g. TMPV"
        className="w-20 px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700 text-xs text-gray-200 placeholder-gray-600 focus:border-blue-500 focus:outline-none"
      />
      {dirty && (
        <button
          onClick={save}
          disabled={setReplacement.isPending}
          className="px-1.5 py-0.5 rounded text-[10px] bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-30"
        >
          {setReplacement.isPending ? "..." : "Set"}
        </button>
      )}
      {!dirty && current && (
        <span className="text-[10px] text-green-500">active</span>
      )}
    </div>
  );
}

const BULK_GROUPS = [
  { id: "paper", label: "Paper Mode Data", description: "Paper-mode trades, predictions, signals, and pending approvals", color: "amber" },
  { id: "live", label: "Live Mode Data", description: "Live-mode trades, predictions, signals, and pending approvals", color: "red" },
  { id: "dry_runs", label: "Dry Runs", description: "All dry run signal previews", color: "amber" },
  { id: "predictions", label: "Predictions — All Modes", description: "All predictions, scoreboard, and failure analyses across both paper and live", color: "amber" },
  { id: "signals", label: "Signals — All Modes", description: "All generated signals across both paper and live (today's dedup will reset)", color: "amber" },
  { id: "pending_trades", label: "Pending Trades — All Modes", description: "All queued pending approvals across both paper and live", color: "amber" },
] as const;

function BulkDeleteSection() {
  const bulkDelete = useBulkDelete();
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800">
        <h3 className="text-sm font-semibold text-gray-300">Bulk Delete</h3>
        <p className="text-xs text-gray-500 mt-0.5">Delete groups of related data. Individual trades can be deleted from the trade detail page.</p>
      </div>
      <div className="p-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {BULK_GROUPS.map((g) => (
          <div key={g.id} className="flex items-center justify-between border border-gray-800 rounded px-3 py-2">
            <div>
              <p className="text-sm text-gray-200">{g.label}</p>
              <p className="text-[10px] text-gray-500">{g.description}</p>
            </div>
            <button
              onClick={() => {
                const msg = g.id === "live"
                  ? `DELETE ALL LIVE DATA? This includes real trades and cannot be undone!`
                  : `Delete all ${g.label.toLowerCase()}? This cannot be undone.`;
                if (!window.confirm(msg)) return;
                if (g.id === "live" && !window.confirm("Are you absolutely sure? This deletes REAL trade history.")) return;
                bulkDelete.mutate(g.id, {
                  onSuccess: (data) => alert(`Deleted ${data.total} rows from: ${Object.entries(data.deleted).filter(([,v]) => v > 0).map(([k,v]) => `${k}(${v})`).join(", ") || "nothing"}`),
                });
              }}
              disabled={bulkDelete.isPending}
              className={`px-2 py-1 rounded text-xs shrink-0 disabled:opacity-50 transition-colors ${
                g.color === "red"
                  ? "bg-red-900/60 hover:bg-red-800 text-red-400"
                  : "bg-amber-900/60 hover:bg-amber-800 text-amber-400"
              }`}
            >
              {bulkDelete.isPending ? "..." : "Delete"}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function QuarantinedSymbolsSection() {
  const { data: symbols, isLoading } = useQuarantinedSymbols();
  const unquarantine = useUnquarantineSymbol();

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800">
        <h3 className="text-sm font-semibold text-gray-300">Quarantined Symbols</h3>
        <p className="text-xs text-gray-500 mt-0.5">
          Symbols auto-blocked after 3 consecutive data fetch failures. Set a replacement symbol to use an alternative instead of skipping.
        </p>
      </div>
      {isLoading ? (
        <div className="h-20 animate-pulse bg-gray-800 m-4 rounded" />
      ) : !symbols || symbols.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-gray-500">
          No quarantined symbols.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-800">
                <th className="py-2 px-4 text-left">Symbol</th>
                <th className="py-2 px-4 text-right">Failures</th>
                <th className="py-2 px-4 text-left">Replacement</th>
                <th className="py-2 px-4 text-left">Last Error</th>
                <th className="py-2 px-4 text-right">Quarantined</th>
                <th className="py-2 px-4 text-center">Actions</th>
              </tr>
            </thead>
            <tbody>
              {symbols.map((s) => (
                <tr key={s.symbol} className="border-b border-gray-800 hover:bg-gray-800/30">
                  <td className="py-2 px-4 font-medium text-gray-200">{s.symbol}</td>
                  <td className="py-2 px-4 text-right text-red-400">{s.consecutive_failures}</td>
                  <td className="py-2 px-4">
                    <ReplacementInput symbol={s.symbol} current={s.replacement_symbol} />
                  </td>
                  <td className="py-2 px-4 text-gray-400 text-xs max-w-xs truncate">{s.last_error}</td>
                  <td className="py-2 px-4 text-right text-gray-400 text-xs">
                    {s.quarantined_at
                      ? parseUTC(s.quarantined_at).toLocaleString("en-IN", {
                          timeZone: getTimezone(),
                          day: "2-digit",
                          month: "short",
                          hour: "2-digit",
                          minute: "2-digit",
                        })
                      : "--"}
                  </td>
                  <td className="py-2 px-4 text-center">
                    <button
                      onClick={() => {
                        if (!window.confirm(`Unquarantine ${s.symbol}? It will be included in the next scan.`)) return;
                        unquarantine.mutate(s.symbol);
                      }}
                      disabled={unquarantine.isPending}
                      className="px-2 py-1 rounded text-xs bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-30 transition-colors"
                    >
                      {unquarantine.isPending ? "..." : "Unblock"}
                    </button>
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

      {/* Quarantined Symbols */}
      <QuarantinedSymbolsSection />

      {/* Bulk Delete */}
      <BulkDeleteSection />

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
