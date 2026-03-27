import { useState } from "react";
import { useMLModels, usePromoteModel } from "../hooks/queries";
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
  isShadow,
  onPromote,
  isPromoting,
  prodModel,
}: {
  model: MLModelInfo;
  type: string;
  isShadow?: boolean;
  onPromote?: () => void;
  isPromoting?: boolean;
  prodModel?: MLModelInfo;
}) {
  return (
    <div
      className={clsx(
        "bg-gray-900 border rounded-lg p-5",
        isShadow ? "border-amber-800/50" : "border-gray-800"
      )}
    >
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
          {isShadow && onPromote && (
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
              {isPromoting ? "Promoting…" : "Promote to Production"}
            </button>
          )}
          <span
            className={clsx(
              "px-2 py-0.5 rounded text-xs font-medium",
              isShadow
                ? "bg-amber-900/40 text-amber-400"
                : model.status === "production"
                  ? "bg-emerald-900/40 text-emerald-400"
                  : "bg-gray-800 text-gray-400"
            )}
          >
            {isShadow ? "Shadow" : model.status || "Production"}
          </span>
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
          value={
            model.win_rate != null ? model.win_rate * 100 : undefined
          }
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
      {isShadow && prodModel && (
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

export function MLModelsPage() {
  const { data, isLoading } = useMLModels();
  const promote = usePromoteModel();
  const [promotingVersion, setPromotingVersion] = useState<string | null>(null);

  const productionModels = data?.production || {};
  const shadowModels = data?.shadow || [];

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">ML Models</h2>

      {/* Production models */}
      <div>
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Production Models
        </h3>
        {isLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {[0, 1].map((i) => (
              <div
                key={i}
                className="h-48 animate-pulse bg-gray-900 rounded-lg"
              />
            ))}
          </div>
        ) : Object.keys(productionModels).length === 0 ? (
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-6">
            <p className="text-gray-500 text-sm">
              No production models deployed yet
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {Object.entries(productionModels).map(([type, model]) => (
              <ModelCard key={type} model={model} type={type} />
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
            <p className="text-gray-500 text-sm">
              No shadow models in trial period
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {shadowModels.map((model, i) => (
              <ModelCard
                key={i}
                model={model}
                type={model.model_type || "unknown"}
                isShadow
                prodModel={productionModels[model.model_type || ""]}
                onPromote={() => {
                  if (model.model_type && model.version) {
                    setPromotingVersion(model.version);
                    promote.mutate(
                      { modelType: model.model_type, version: model.version },
                      { onSettled: () => setPromotingVersion(null) }
                    );
                  }
                }}
                isPromoting={promote.isPending && promotingVersion === model.version}
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
              outperforms production, it gets promoted automatically
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="w-5 h-5 rounded-full bg-red-900/40 text-red-400 flex items-center justify-center shrink-0 text-xs font-bold">
              4
            </span>
            <span>
              <strong className="text-gray-300">Retire</strong> — Old models
              retired, predictions tracked for continuous improvement
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
