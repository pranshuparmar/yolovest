import { useRef, useState } from "react";
import { useMLModels, usePromoteModel, useDeleteModel, useReshadowModel, useRetireModel, useShadowComparison, useUploadModel, useImportModel } from "../hooks/queries";
import { api } from "../api/endpoints";
import clsx from "clsx";
import type { MLModelInfo } from "../types/api";

function fmt(n: number | undefined, d = 2) {
  if (n == null) return "—";
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

function MetricCard({
  label,
  value,
  color,
  suffix,
}: {
  label: string;
  value: number | undefined;
  color?: string;
  suffix?: string;
}) {
  return (
    <div className="bg-gray-800/50 rounded-lg p-3">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={clsx("text-lg font-semibold", color || "text-gray-100")}>
        {fmt(value)}
        {suffix && <span className="text-sm text-gray-400">{suffix}</span>}
      </p>
    </div>
  );
}

function ModelCard({
  model,
  type,
  status,
  onPromote,
  isPromoting,
  onDelete,
  isDeleting,
  onReshadow,
  isReshadowing,
  onRetire,
  isRetiring,
  onDownload,
  prodModel,
}: {
  model: MLModelInfo;
  type: string;
  status: "production" | "shadow" | "retired";
  onPromote?: () => void;
  isPromoting?: boolean;
  onDelete?: () => void;
  isDeleting?: boolean;
  onReshadow?: () => void;
  isReshadowing?: boolean;
  onRetire?: () => void;
  isRetiring?: boolean;
  onDownload?: () => void;
  prodModel?: MLModelInfo;
}) {
  const statusConfig = {
    production: { border: "border-gray-800", badge: "bg-emerald-900/40 text-emerald-400", label: "Production" },
    shadow: { border: "border-amber-800/50", badge: "bg-amber-900/40 text-amber-400", label: "Shadow" },
    retired: { border: "border-gray-800/50", badge: "bg-gray-800 text-gray-500", label: "Retired" },
  };
  const cfg = statusConfig[status];

  return (
    <div className={clsx("bg-gray-900 border rounded-lg p-5", cfg.border, status === "retired" && "opacity-75")}>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-medium text-gray-200 capitalize">
            {type} Model
          </h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Version: {model.version || "—"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {onDownload && model.version && (
            <button
              onClick={onDownload}
              className="px-2.5 py-1 rounded text-xs font-medium bg-gray-800 text-gray-400 hover:bg-blue-900/40 hover:text-blue-400 transition-colors"
              title="Download this model's .pkl to move it to another machine"
            >
              Download
            </button>
          )}
          {onPromote && (
            <button
              onClick={onPromote}
              disabled={isPromoting}
              className={clsx(
                "px-2.5 py-1 rounded text-xs font-medium transition-colors",
                isPromoting
                  ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                  : "bg-emerald-900/40 text-emerald-400 hover:bg-emerald-800/60"
              )}
            >
              {isPromoting ? "Promoting…" : status === "retired" ? "Restore to Production" : "Promote to Production"}
            </button>
          )}
          {onReshadow && (
            <button
              onClick={onReshadow}
              disabled={isReshadowing}
              className="px-2.5 py-1 rounded text-xs font-medium bg-amber-900/30 text-amber-400 hover:bg-amber-800/50 transition-colors disabled:opacity-30"
            >
              {isReshadowing ? "..." : "Re-shadow"}
            </button>
          )}
          {onRetire && (
            <button
              onClick={onRetire}
              disabled={isRetiring}
              className="px-2.5 py-1 rounded text-xs font-medium bg-gray-700 text-gray-400 hover:bg-gray-600 transition-colors disabled:opacity-30"
            >
              {isRetiring ? "..." : "Retire"}
            </button>
          )}
          {onDelete && (
            <button
              onClick={onDelete}
              disabled={isDeleting}
              className="px-2.5 py-1 rounded text-xs font-medium bg-red-900/30 text-red-400 hover:bg-red-800/50 transition-colors disabled:opacity-30"
            >
              {isDeleting ? "..." : "Delete"}
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <MetricCard
          label="Sharpe Ratio"
          value={model.sharpe_ratio}
          color={
            model.sharpe_ratio != null && model.sharpe_ratio > 1
              ? "text-emerald-400"
              : model.sharpe_ratio != null && model.sharpe_ratio < 0
                ? "text-red-400"
                : undefined
          }
        />
        <MetricCard
          label="Win Rate"
          value={model.win_rate != null ? model.win_rate * 100 : undefined}
          suffix="%"
          color={
            model.win_rate != null && model.win_rate > 0.5
              ? "text-emerald-400"
              : "text-red-400"
          }
        />
        <MetricCard
          label="Max Drawdown"
          value={model.max_drawdown_pct}
          suffix="%"
          color="text-red-400"
        />
        <MetricCard
          label="Profit Factor"
          value={model.profit_factor}
          color={
            model.profit_factor != null && model.profit_factor > 1
              ? "text-emerald-400"
              : "text-red-400"
          }
        />
      </div>

      {/* Comparison vs production */}
      {status === "shadow" && prodModel && (
        <ComparisonRow shadow={model} production={prodModel} />
      )}
    </div>
  );
}

function delta(shadow: number | undefined, prod: number | undefined) {
  if (shadow == null || prod == null) return null;
  return shadow - prod;
}

function DeltaBadge({ value, suffix, invert }: { value: number | null; suffix?: string; invert?: boolean }) {
  if (value == null) return <span className="text-gray-600">--</span>;
  const positive = invert ? value < 0 : value > 0;
  const color = positive ? "text-emerald-400" : value === 0 ? "text-gray-400" : "text-red-400";
  const sign = value > 0 ? "+" : "";
  return (
    <span className={clsx("text-xs font-mono", color)}>
      {sign}{value.toFixed(2)}{suffix}
    </span>
  );
}

function ComparisonRow({ shadow, production }: { shadow: MLModelInfo; production: MLModelInfo }) {
  return (
    <div className="mt-3 pt-3 border-t border-gray-800">
      <p className="text-xs text-gray-500 mb-2">vs Production ({production.version?.split("_v")[0] || "current"})</p>
      <div className="grid grid-cols-4 gap-2 text-center">
        <div>
          <p className="text-xs text-gray-600">Sharpe</p>
          <DeltaBadge value={delta(shadow.sharpe_ratio, production.sharpe_ratio)} />
        </div>
        <div>
          <p className="text-xs text-gray-600">Win Rate</p>
          <DeltaBadge value={delta(
            shadow.win_rate != null ? shadow.win_rate * 100 : undefined,
            production.win_rate != null ? production.win_rate * 100 : undefined,
          )} suffix="%" />
        </div>
        <div>
          <p className="text-xs text-gray-600">Drawdown</p>
          <DeltaBadge value={delta(shadow.max_drawdown_pct, production.max_drawdown_pct)} suffix="%" invert />
        </div>
        <div>
          <p className="text-xs text-gray-600">Profit F.</p>
          <DeltaBadge value={delta(shadow.profit_factor, production.profit_factor)} />
        </div>
      </div>
    </div>
  );
}

function ShadowLiveMetrics({ modelType }: { modelType: string }) {
  const { data } = useShadowComparison(modelType);
  if (!data || (!data.shadow?.total && !data.production?.total)) return null;

  const s = data.shadow || {};
  const p = data.production || {};

  return (
    <div className="bg-gray-900 border border-amber-800/30 rounded-lg p-4">
      <h4 className="text-xs font-medium text-amber-400 mb-3">
        Live Shadow vs Production — {modelType}
      </h4>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-gray-800">
              <th className="py-1 px-2 text-left">Metric</th>
              <th className="py-1 px-2 text-right">Production</th>
              <th className="py-1 px-2 text-right">Shadow</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-gray-800/50">
              <td className="py-1.5 px-2 text-gray-400">Predictions</td>
              <td className="py-1.5 px-2 text-right text-gray-300">{p.total ?? 0}</td>
              <td className="py-1.5 px-2 text-right text-gray-300">{s.total ?? 0}</td>
            </tr>
            <tr className="border-b border-gray-800/50">
              <td className="py-1.5 px-2 text-gray-400">Direction Accuracy</td>
              <td className="py-1.5 px-2 text-right text-gray-300">{p.direction_accuracy != null ? `${(p.direction_accuracy * 100).toFixed(1)}%` : "—"}</td>
              <td className={clsx("py-1.5 px-2 text-right font-medium",
                s.direction_accuracy != null && p.direction_accuracy != null
                  ? s.direction_accuracy > p.direction_accuracy ? "text-emerald-400" : s.direction_accuracy < p.direction_accuracy ? "text-red-400" : "text-gray-300"
                  : "text-gray-300"
              )}>{s.direction_accuracy != null ? `${(s.direction_accuracy * 100).toFixed(1)}%` : "—"}</td>
            </tr>
            <tr className="border-b border-gray-800/50">
              <td className="py-1.5 px-2 text-gray-400">Target Hit Rate</td>
              <td className="py-1.5 px-2 text-right text-gray-300">{p.target_hit_rate != null ? `${(p.target_hit_rate * 100).toFixed(1)}%` : "—"}</td>
              <td className="py-1.5 px-2 text-right text-gray-300">{s.target_hit_rate != null ? `${(s.target_hit_rate * 100).toFixed(1)}%` : "—"}</td>
            </tr>
            <tr>
              <td className="py-1.5 px-2 text-gray-400">Avg PnL %</td>
              <td className="py-1.5 px-2 text-right text-gray-300">{p.avg_pnl_pct != null ? `${(p.avg_pnl_pct * 100).toFixed(2)}%` : "—"}</td>
              <td className={clsx("py-1.5 px-2 text-right font-medium",
                s.avg_pnl_pct != null && p.avg_pnl_pct != null
                  ? s.avg_pnl_pct > p.avg_pnl_pct ? "text-emerald-400" : s.avg_pnl_pct < p.avg_pnl_pct ? "text-red-400" : "text-gray-300"
                  : "text-gray-300"
              )}>{s.avg_pnl_pct != null ? `${(s.avg_pnl_pct * 100).toFixed(2)}%` : "—"}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function MLModelsPage() {
  const { data, isLoading } = useMLModels();
  const promote = usePromoteModel();
  const deleteModel = useDeleteModel();
  const reshadow = useReshadowModel();
  const retire = useRetireModel();
  const uploadModel = useUploadModel();
  const importModel = useImportModel();
  const [promotingVersion, setPromotingVersion] = useState<string | null>(null);
  const [deletingVersion, setDeletingVersion] = useState<string | null>(null);
  const [reshadowingVersion, setReshadowingVersion] = useState<string | null>(null);
  const [retiringVersion, setRetiringVersion] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const modelUploadRef = useRef<HTMLInputElement>(null);
  const [uploadedVersion, setUploadedVersion] = useState<string | null>(null);
  const [importType, setImportType] = useState<"intraday" | "swing">("swing");
  const [importPromote, setImportPromote] = useState(false);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [schemaBlock, setSchemaBlock] = useState<string | null>(null);

  const handleModelDownload = (version: string) => {
    api.downloadModel(version).catch((err) =>
      setActionError(`Download failed: ${(err as Error).message}`),
    );
  };

  const handleModelUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setImportMsg(null);
    setActionError(null);
    uploadModel.mutate(file, {
      onSuccess: (r) => {
        setUploadedVersion(r.version);
        setImportMsg(`Uploaded ${r.filename} (${(r.size_bytes / 1024 / 1024).toFixed(1)} MB). Choose model type and import below.`);
      },
      onError: (err) => setActionError(`Upload failed: ${(err as Error).message}`),
    });
  };

  const handleImport = (force = false) => {
    if (!uploadedVersion) return;
    setActionError(null);
    setSchemaBlock(null);
    importModel.mutate(
      { model_type: importType, version: uploadedVersion, promote: importPromote, force },
      {
        onSuccess: (r) => {
          const warn = r.warnings?.length ? ` Warnings: ${r.warnings.join(" ")}` : "";
          setImportMsg(
            `Imported ${r.version} as ${r.model_type}${r.promoted ? " (promoted to production)" : " (shadow)"}${r.hot_reloaded ? ", hot-reloaded" : ""}.${warn}`,
          );
          setUploadedVersion(null);
        },
        onError: (err) => {
          const e = err as Error & { status?: number };
          // 422 = compatibility gate. Offer an explicit override instead
          // of silently failing or silently importing an incompatible model.
          if (e.status === 422) {
            setSchemaBlock(e.message);
          } else {
            setActionError(`Import failed: ${e.message}`);
          }
        },
      },
    );
  };

  const productionModels = data?.production || {};
  const shadowModels = data?.shadow || [];
  const retiredModels = data?.retired || [];

  const handleDelete = (modelType: string, version: string) => {
    if (!window.confirm(`Delete model ${version}? The .pkl file will also be removed. This cannot be undone.`)) return;
    setDeletingVersion(version);
    deleteModel.mutate(
      { modelType, version },
      { onSettled: () => setDeletingVersion(null) },
    );
  };

  const handleReshadow = (modelType: string, version: string) => {
    setActionError(null);
    setReshadowingVersion(version);
    reshadow.mutate(
      { modelType, version },
      {
        onSuccess: (result) => {
          if (result && !result.reshadowed && "error" in result) {
            setActionError(String((result as Record<string, unknown>).error));
          }
        },
        onSettled: () => setReshadowingVersion(null),
      },
    );
  };

  const handleRetire = (modelType: string, version: string) => {
    setRetiringVersion(version);
    retire.mutate(
      { modelType, version },
      { onSettled: () => setRetiringVersion(null) },
    );
  };

  const handlePromote = (modelType: string, version: string) => {
    setPromotingVersion(version);
    promote.mutate(
      { modelType, version },
      { onSettled: () => setPromotingVersion(null) },
    );
  };

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">ML Models</h2>

      {actionError && (
        <div className="bg-red-900/20 border border-red-800 rounded-lg p-3 text-sm text-red-400 flex items-center justify-between">
          <span>{actionError}</span>
          <button onClick={() => setActionError(null)} className="text-gray-500 hover:text-gray-300 text-xs">Dismiss</button>
        </div>
      )}

      {/* Import a model trained on another machine */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-300 mb-1">Import Model</h3>
        <p className="text-xs text-gray-500 mb-3">
          Trained on a higher-memory box? Upload the <code className="text-gray-400">.pkl</code> here,
          then register it as intraday/swing — optionally promoting it straight to production.
        </p>
        <input
          ref={modelUploadRef}
          type="file"
          accept=".pkl"
          onChange={handleModelUpload}
          className="hidden"
        />
        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={() => modelUploadRef.current?.click()}
            disabled={uploadModel.isPending}
            className="px-3 py-1.5 rounded text-sm font-medium bg-gray-700 hover:bg-gray-600 text-gray-200 disabled:opacity-50 transition-colors"
          >
            {uploadModel.isPending ? "Uploading..." : "Upload .pkl"}
          </button>
          {uploadedVersion && (
            <>
              <span className="text-xs font-mono text-gray-400">{uploadedVersion}</span>
              <select
                value={importType}
                onChange={(e) => setImportType(e.target.value as "intraday" | "swing")}
                className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200"
              >
                <option value="swing">swing</option>
                <option value="intraday">intraday</option>
              </select>
              <label className="flex items-center gap-1.5 text-xs text-gray-400">
                <input
                  type="checkbox"
                  checked={importPromote}
                  onChange={(e) => setImportPromote(e.target.checked)}
                  className="rounded"
                />
                Promote to production
              </label>
              <button
                onClick={() => handleImport(false)}
                disabled={importModel.isPending}
                className="px-3 py-1.5 rounded text-sm font-medium bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50 transition-colors"
              >
                {importModel.isPending ? "Importing..." : "Import"}
              </button>
            </>
          )}
        </div>
        {importMsg && <p className="text-xs text-emerald-400 mt-2">{importMsg}</p>}
        {schemaBlock && (
          <div className="mt-3 bg-amber-900/20 border border-amber-800/50 rounded p-3 text-xs">
            <p className="text-amber-300 font-medium mb-1">Compatibility check failed</p>
            <p className="text-amber-200/80 mb-2">{schemaBlock}</p>
            <div className="flex items-center gap-2">
              <button
                onClick={() => handleImport(true)}
                disabled={importModel.isPending}
                className="px-2.5 py-1 rounded font-medium bg-amber-700 hover:bg-amber-600 text-white disabled:opacity-50"
              >
                Import anyway
              </button>
              <button
                onClick={() => setSchemaBlock(null)}
                className="px-2.5 py-1 rounded text-amber-300 hover:text-amber-100"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Production models */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Production Models
        </h3>
        {isLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {[0, 1].map((i) => (
              <div key={i} className="h-48 animate-pulse bg-gray-900 rounded-lg" />
            ))}
          </div>
        ) : Object.keys(productionModels).length === 0 ? (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
            <p className="text-gray-500 text-sm">No production models deployed yet</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {Object.entries(productionModels).map(([type, model]) => (
              <ModelCard
                key={type}
                model={model}
                type={type}
                status="production"
                onDownload={() => model.version && handleModelDownload(model.version)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Shadow models (A/B testing) */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Shadow Models (A/B Testing)
        </h3>
        {isLoading ? (
          <div className="h-32 animate-pulse bg-gray-900 rounded-lg" />
        ) : shadowModels.length === 0 ? (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
            <p className="text-gray-500 text-sm">No shadow models in trial period</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {shadowModels.map((model, i) => (
              <ModelCard
                key={i}
                model={model}
                type={model.model_type || "unknown"}
                status="shadow"
                prodModel={productionModels[model.model_type || ""]}
                onPromote={() => model.model_type && model.version && handlePromote(model.model_type, model.version)}
                isPromoting={promote.isPending && promotingVersion === model.version}
                onRetire={() => model.model_type && model.version && handleRetire(model.model_type, model.version)}
                isRetiring={retire.isPending && retiringVersion === model.version}
                onDelete={() => model.model_type && model.version && handleDelete(model.model_type, model.version)}
                isDeleting={deleteModel.isPending && deletingVersion === model.version}
                onDownload={() => model.version && handleModelDownload(model.version)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Shadow live performance comparison */}
      {shadowModels.length > 0 && (
        <div className="space-y-4">
          {[...new Set(shadowModels.map((m) => m.model_type).filter(Boolean))].map((mt) => (
            <ShadowLiveMetrics key={mt} modelType={mt!} />
          ))}
        </div>
      )}

      {/* Retired models */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Retired Models
        </h3>
        {isLoading ? (
          <div className="h-32 animate-pulse bg-gray-900 rounded-lg" />
        ) : retiredModels.length === 0 ? (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
            <p className="text-gray-500 text-sm">No retired models</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {retiredModels.map((model, i) => (
              <ModelCard
                key={i}
                model={model}
                type={model.model_type || "unknown"}
                status="retired"
                onPromote={() => model.model_type && model.version && handlePromote(model.model_type, model.version)}
                isPromoting={promote.isPending && promotingVersion === model.version}
                onReshadow={() => model.model_type && model.version && handleReshadow(model.model_type, model.version)}
                isReshadowing={reshadow.isPending && reshadowingVersion === model.version}
                onDelete={() => model.model_type && model.version && handleDelete(model.model_type, model.version)}
                isDeleting={deleteModel.isPending && deletingVersion === model.version}
                onDownload={() => model.version && handleModelDownload(model.version)}
              />
            ))}
          </div>
        )}
      </div>

      {/* How it works */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-2">
          Model Lifecycle
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 text-xs text-gray-500">
          <div className="flex items-start gap-2">
            <span className="w-5 h-5 rounded-full bg-blue-900/40 text-blue-400 flex items-center justify-center shrink-0 text-xs font-bold">
              1
            </span>
            <span>
              <strong className="text-gray-300">Train</strong> — XGBoost model
              trained on walk-forward windows with Platt scaling calibration
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="w-5 h-5 rounded-full bg-amber-900/40 text-amber-400 flex items-center justify-center shrink-0 text-xs font-bold">
              2
            </span>
            <span>
              <strong className="text-gray-300">Shadow</strong> — New model runs
              in shadow mode alongside production for trial period
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="w-5 h-5 rounded-full bg-emerald-900/40 text-emerald-400 flex items-center justify-center shrink-0 text-xs font-bold">
              3
            </span>
            <span>
              <strong className="text-gray-300">Promote</strong> — If shadow
              outperforms production, it gets promoted (auto or manual)
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="w-5 h-5 rounded-full bg-red-900/40 text-red-400 flex items-center justify-center shrink-0 text-xs font-bold">
              4
            </span>
            <span>
              <strong className="text-gray-300">Retire</strong> — Old models
              kept for rollback. Delete when no longer needed.
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
