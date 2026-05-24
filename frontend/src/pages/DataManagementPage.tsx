import { useEffect, useMemo, useRef, useState } from "react";
import {
  useStorageStats,
  useCleanupTable,
  useBackups,
  useCreateBackup,
  useRestoreBackup,
  useDeleteBackup,
  useSetBackupLock,
  useUploadBackup,
  useResetAllData,
  useQuarantinedSymbols,
  useUnquarantineSymbol,
  useBulkUnquarantineSymbols,
  useSetReplacementSymbol,
  useBulkDelete,
  useRotationCooldown,
  useClearRotationCooldown,
} from "../hooks/queries";
import type { TableStats } from "../types/api";
import { api } from "../api/endpoints";
import { parseUTC, getTimezone } from "../utils/datetime";
import { SymbolLink } from "../components/SymbolLink";

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

function RotationCooldownSection() {
  const { data, isLoading } = useRotationCooldown();
  const clear = useClearRotationCooldown();
  const [confirming, setConfirming] = useState(false);

  const handleClearAll = () => {
    if (!confirming) {
      setConfirming(true);
      return;
    }
    clear.mutate(undefined, { onSettled: () => setConfirming(false) });
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800 flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-300">Rotation Cooldown</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Symbols benched by market-scan after consecutive no-signal
            heartbeats. Resetting forces them back into the scoring pool
            on the next market-scan.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {confirming && (
            <button
              onClick={() => setConfirming(false)}
              className="text-xs text-gray-500 hover:text-gray-300"
            >
              Cancel
            </button>
          )}
          <button
            onClick={handleClearAll}
            disabled={clear.isPending || (data?.count ?? 0) === 0}
            className={`px-3 py-1 rounded text-sm font-medium disabled:opacity-40 transition-colors whitespace-nowrap ${
              confirming
                ? "bg-red-600 hover:bg-red-700 text-white"
                : "bg-gray-700 hover:bg-gray-600 text-gray-200"
            }`}
          >
            {clear.isPending
              ? "Clearing..."
              : confirming
                ? "Confirm Clear All"
                : "Clear All"}
          </button>
        </div>
      </div>
      {isLoading ? (
        <div className="h-20 m-4 animate-pulse bg-gray-800 rounded" />
      ) : !data ? (
        <div className="px-4 py-6 text-center text-sm text-gray-500">—</div>
      ) : (
        <div className="px-4 py-3 space-y-3">
          <div className="flex flex-wrap items-center gap-4 text-xs text-gray-500">
            <span>
              Status:{" "}
              <span className={data.enabled ? "text-emerald-400" : "text-gray-400"}>
                {data.enabled ? "enabled" : "disabled"}
              </span>
            </span>
            <span>
              Threshold:{" "}
              <span className="text-gray-300 font-mono">
                {data.no_signal_threshold}
              </span>{" "}
              heartbeats
            </span>
            <span>
              Cooldown:{" "}
              <span className="text-gray-300 font-mono">
                {data.cooldown_hours}h
              </span>
            </span>
            <span>
              In cooldown:{" "}
              <span className="text-gray-300 font-mono">{data.count}</span>{" "}
              symbol{data.count === 1 ? "" : "s"}
            </span>
          </div>
          {data.count > 0 && (
            <div className="text-xs text-gray-400 leading-relaxed max-h-32 overflow-y-auto font-mono">
              {data.symbols.join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function QuarantinedSymbolsSection() {
  const { data: symbols, isLoading } = useQuarantinedSymbols();
  const unquarantine = useUnquarantineSymbol();
  const bulkUnquarantine = useBulkUnquarantineSymbols();
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Drop stale entries from the selection set whenever the listing
  // shrinks (after a bulk unblock or single unblock).
  const visible = useMemo(
    () => new Set((symbols ?? []).map((s) => s.symbol)),
    [symbols],
  );
  useEffect(() => {
    setSelected((prev) => {
      const next = new Set<string>();
      for (const s of prev) if (visible.has(s)) next.add(s);
      return next.size === prev.size ? prev : next;
    });
  }, [visible]);

  const toggle = (sym: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(sym)) next.delete(sym);
      else next.add(sym);
      return next;
    });
  const allSelected = !!symbols && symbols.length > 0 && selected.size === symbols.length;
  const toggleAll = () => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set((symbols ?? []).map((s) => s.symbol)));
  };
  const runBulkUnblock = () => {
    const picks = Array.from(selected);
    if (picks.length === 0) return;
    if (!window.confirm(`Unquarantine ${picks.length} symbol${picks.length > 1 ? "s" : ""}? They will be included in the next scan.`)) return;
    bulkUnquarantine.mutate(picks, {
      onSuccess: () => setSelected(new Set()),
    });
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-sm font-semibold text-gray-300">Quarantined Symbols</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Symbols auto-blocked after 3 consecutive data fetch failures. Set a replacement symbol to use an alternative instead of skipping.
          </p>
        </div>
        {selected.size > 0 && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-400">
              {selected.size} selected
            </span>
            <button
              onClick={runBulkUnblock}
              disabled={bulkUnquarantine.isPending}
              className="px-3 py-1 rounded text-xs font-medium bg-amber-600 hover:bg-amber-700 text-white disabled:opacity-30 transition-colors"
            >
              {bulkUnquarantine.isPending ? "Unblocking..." : `Unblock ${selected.size}`}
            </button>
          </div>
        )}
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
                <th className="py-2 px-3 text-center w-8">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    aria-label="Select all quarantined symbols"
                    className="accent-amber-500 cursor-pointer"
                  />
                </th>
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
                  <td className="py-2 px-3 text-center">
                    <input
                      type="checkbox"
                      checked={selected.has(s.symbol)}
                      onChange={() => toggle(s.symbol)}
                      aria-label={`Select ${s.symbol}`}
                      className="accent-amber-500 cursor-pointer"
                    />
                  </td>
                  <td className="py-2 px-4 font-medium text-gray-200">
                    <SymbolLink symbol={s.symbol} className="text-gray-200" />
                  </td>
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
  const { data, isLoading: storageLoading, error: storageError } = useStorageStats();
  const { data: backups } = useBackups();
  const cleanup = useCleanupTable();
  const createBackup = useCreateBackup();
  const restoreBackup = useRestoreBackup();
  const deleteBackup = useDeleteBackup();
  const setBackupLock = useSetBackupLock();
  const uploadBackup = useUploadBackup();
  const resetAll = useResetAllData();
  const [lastResult, setLastResult] = useState<string | null>(null);
  const [resetStep, setResetStep] = useState<"idle" | "warn" | "confirm">("idle");
  const [restoreConfirm, setRestoreConfirm] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const backupUploadRef = useRef<HTMLInputElement>(null);

  const handleBackupUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    uploadBackup.mutate(file, {
      onSuccess: (r) => setLastResult(`Uploaded backup ${r.filename} (${formatBytes(r.size_bytes)}). Use Restore to apply it.`),
      onError: (err) => setLastResult(`Upload failed: ${(err as Error).message}`),
    });
  };

  const handleBackupDownload = (filename: string) => {
    api.downloadBackup(filename).catch((err) =>
      setLastResult(`Download failed: ${(err as Error).message}`),
    );
  };

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

  // Storage stats is the slowest query on the page (COUNT(*) per table).
  // Render the rest of the page immediately so backups, quarantine and
  // bulk-delete are usable while the counts trickle in.
  const dbFile = data?._db_file;
  const cleanableTables = Object.keys(TABLE_INFO);
  const readOnlyTables = data
    ? Object.entries(data)
        .filter(([k]) => !cleanableTables.includes(k) && k !== "_db_file")
        .map(([k, v]) => ({ name: k, stats: v as TableStats }))
    : [];

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
            {dbFile ? formatBytes(dbFile.db_bytes) : (
              <span className="inline-block h-7 w-24 animate-pulse bg-gray-800 rounded" />
            )}
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wide">WAL Size</div>
          <div className="text-2xl font-bold text-gray-100 mt-1">
            {dbFile ? formatBytes(dbFile.wal_bytes) : (
              <span className="inline-block h-7 w-24 animate-pulse bg-gray-800 rounded" />
            )}
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Total on Disk</div>
          <div className="text-2xl font-bold text-emerald-400 mt-1">
            {dbFile ? formatBytes(dbFile.total_bytes) : (
              <span className="inline-block h-7 w-24 animate-pulse bg-gray-800 rounded" />
            )}
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
          <div className="flex items-center gap-2 shrink-0">
            <input
              ref={backupUploadRef}
              type="file"
              accept=".db"
              onChange={handleBackupUpload}
              className="hidden"
            />
            <button
              onClick={() => backupUploadRef.current?.click()}
              disabled={uploadBackup.isPending}
              className="px-3 py-1.5 rounded text-sm font-medium bg-gray-700 hover:bg-gray-600 text-gray-200 disabled:opacity-50 transition-colors"
              title="Upload a .db backup from another machine (e.g. one trained offline)"
            >
              {uploadBackup.isPending ? "Uploading..." : "Upload Backup"}
            </button>
            <button
              onClick={handleBackup}
              disabled={createBackup.isPending}
              className="px-3 py-1.5 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
            >
              {createBackup.isPending ? "Creating..." : "Create Backup Now"}
            </button>
          </div>
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
                  <td className="py-2 px-4 font-mono text-gray-300 text-xs">
                    <span className="inline-flex items-center gap-2">
                      {b.filename}
                      {b.locked && (
                        <span
                          className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-900/40 text-amber-400"
                          title="Locked — exempt from daily prune and manual delete"
                        >
                          LOCKED
                        </span>
                      )}
                    </span>
                  </td>
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
                          {restoreBackup.isPending ? "Restoring..." : "Confirm Restore"}
                        </button>
                        <button
                          onClick={() => setRestoreConfirm(null)}
                          className="text-xs text-gray-500 hover:text-gray-300"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : deleteConfirm === b.filename ? (
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={() => {
                            deleteBackup.mutate(b.filename, {
                              onSuccess: (result) => {
                                setLastResult(
                                  `Deleted ${result.filename} (${formatBytes(result.size_bytes)} freed)`,
                                );
                                setDeleteConfirm(null);
                              },
                            });
                          }}
                          disabled={deleteBackup.isPending}
                          className="px-2 py-0.5 rounded text-xs font-medium bg-red-600 hover:bg-red-700 text-white disabled:opacity-50"
                        >
                          {deleteBackup.isPending ? "Deleting..." : "Confirm Delete"}
                        </button>
                        <button
                          onClick={() => setDeleteConfirm(null)}
                          className="text-xs text-gray-500 hover:text-gray-300"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={() => {
                            setBackupLock.mutate(
                              { filename: b.filename, locked: !b.locked },
                              {
                                onSuccess: () => setLastResult(
                                  b.locked
                                    ? `Unlocked ${b.filename}`
                                    : `Locked ${b.filename} — exempt from prune & delete`,
                                ),
                              },
                            );
                          }}
                          disabled={setBackupLock.isPending}
                          className={b.locked
                            ? "px-2 py-0.5 rounded text-xs font-medium bg-amber-900/40 text-amber-400 hover:bg-amber-900/60 transition-colors disabled:opacity-50"
                            : "px-2 py-0.5 rounded text-xs font-medium bg-gray-800 hover:bg-amber-900/30 text-gray-400 hover:text-amber-400 transition-colors disabled:opacity-50"}
                          title={b.locked
                            ? "Click to unlock — backup will be eligible for prune/delete again"
                            : "Click to lock — prevents auto-prune and manual delete"}
                        >
                          {b.locked ? "Unlock" : "Lock"}
                        </button>
                        <button
                          onClick={() => handleBackupDownload(b.filename)}
                          className="px-2 py-0.5 rounded text-xs font-medium bg-gray-800 hover:bg-blue-900/40 text-gray-400 hover:text-blue-400 transition-colors"
                          title="Download this backup to move it to another machine"
                        >
                          Download
                        </button>
                        <button
                          onClick={() => { setRestoreConfirm(b.filename); setDeleteConfirm(null); }}
                          className="px-2 py-0.5 rounded text-xs font-medium bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
                        >
                          Restore
                        </button>
                        <button
                          onClick={() => { setDeleteConfirm(b.filename); setRestoreConfirm(null); }}
                          disabled={b.locked}
                          title={b.locked ? "Unlock first to delete" : undefined}
                          className="px-2 py-0.5 rounded text-xs font-medium bg-gray-800 hover:bg-red-900/40 text-gray-400 hover:text-red-400 transition-colors disabled:opacity-40 disabled:hover:bg-gray-800 disabled:hover:text-gray-400 disabled:cursor-not-allowed"
                        >
                          Delete
                        </button>
                      </div>
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
        {storageLoading ? (
          <div className="h-40 m-4 animate-pulse bg-gray-800 rounded" />
        ) : storageError || !data ? (
          <div className="px-4 py-6 text-sm text-red-400">
            Failed to load storage stats.
          </div>
        ) : (
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
        )}
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
      <RotationCooldownSection />

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
