import { useMemo, useState } from "react";
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, Legend, LabelList,
} from "recharts";
import clsx from "clsx";
import { useModelDrift } from "../hooks/queries";
import { useChartTheme, useTooltipStyle } from "../hooks/useChartTheme";
import { SignalClassWidget } from "../components/SignalClassWidget";
import type { ModelDriftVersion } from "../types/api";

function pct(n: number | null | undefined, d = 1) {
  if (n == null) return "—";
  return `${(n * 100).toFixed(d)}%`;
}

function ModelView({ model }: { model: ModelDriftVersion }) {
  const ct = useChartTheme();
  const tooltipStyle = useTooltipStyle();

  const lineData = useMemo(
    () =>
      model.by_day.map((d) => ({
        date: d.date.slice(5),
        predicted: d.predicted_win_rate != null
          ? Number((d.predicted_win_rate * 100).toFixed(2))
          : null,
        realised: Number((d.realised_win_rate * 100).toFixed(2)),
        samples: d.sample_size,
      })),
    [model.by_day],
  );

  const barData = useMemo(
    () =>
      model.calibration_buckets.map((b) => ({
        bucket: b.bucket,
        predicted: b.predicted_mean != null
          ? Number((b.predicted_mean * 100).toFixed(2))
          : 0,
        realised: b.realised_rate != null
          ? Number((b.realised_rate * 100).toFixed(2))
          : 0,
        samples: b.samples,
      })),
    [model.calibration_buckets],
  );

  const totalSamples = model.by_day.reduce((acc, d) => acc + d.sample_size, 0);

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Model</p>
          <p className="text-xl font-semibold capitalize">{model.model_type}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Version</p>
          <p className="text-sm font-mono text-gray-200 truncate" title={model.version}>
            {model.version}
          </p>
          <p className="text-xs text-gray-500 mt-1">
            {model.is_production ? (
              <span className="text-emerald-400">production</span>
            ) : (
              <span className="text-gray-500">shadow / retired</span>
            )}
          </p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Scored Predictions</p>
          <p className="text-xl font-semibold">{totalSamples}</p>
        </div>
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <p className="text-xs text-gray-500">Days with Data</p>
          <p className="text-xl font-semibold">{model.by_day.length}</p>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Predicted vs Realised Win Rate (daily)
        </h3>
        {lineData.length === 0 ? (
          <p className="text-gray-500 text-sm py-8 text-center">
            No scored predictions in the selected window.
          </p>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={lineData}>
              <CartesianGrid strokeDasharray="3 3" stroke={ct.grid} />
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis
                tick={{ fontSize: 10, fill: ct.tick }}
                domain={[0, 100]}
                tickFormatter={(v: number) => `${v}%`}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                formatter={(v: number) => `${v.toFixed(1)}%`}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Line
                type="monotone"
                dataKey="predicted"
                stroke="#58a6ff"
                strokeWidth={2}
                dot={false}
                name="Predicted (mean confidence)"
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="realised"
                stroke="#3fb950"
                strokeWidth={2}
                dot={false}
                name="Realised (direction correct)"
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">
          Calibration by Confidence Bucket
        </h3>
        <p className="text-xs text-gray-500 mb-3">
          A well-calibrated model shows the realised bar roughly matching the
          predicted bar. Persistent gaps indicate the confidence score is
          mis-scaled and the model needs retraining.
        </p>
        {barData.every((b) => b.samples === 0) ? (
          <p className="text-gray-500 text-sm py-8 text-center">No calibration data.</p>
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={barData}>
              <CartesianGrid strokeDasharray="3 3" stroke={ct.grid} />
              <XAxis dataKey="bucket" tick={{ fontSize: 10, fill: ct.tick }} />
              <YAxis
                tick={{ fontSize: 10, fill: ct.tick }}
                domain={[0, 100]}
                tickFormatter={(v: number) => `${v}%`}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                formatter={(v: number) => `${v.toFixed(1)}%`}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Bar dataKey="predicted" name="Predicted mean" fill="#58a6ff">
                <LabelList
                  dataKey="samples"
                  position="top"
                  formatter={(v: unknown) => `n=${v}`}
                  style={{ fill: ct.tick, fontSize: 10 }}
                />
              </Bar>
              <Bar dataKey="realised" name="Realised rate" fill="#3fb950" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-3">Daily Detail</h3>
        {model.by_day.length === 0 ? (
          <p className="text-gray-500 text-sm">No data.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-4">Date</th>
                  <th className="pb-2 pr-4">Predicted</th>
                  <th className="pb-2 pr-4">Realised</th>
                  <th className="pb-2 pr-4">Gap</th>
                  <th className="pb-2">Samples</th>
                </tr>
              </thead>
              <tbody>
                {[...model.by_day].reverse().map((d) => {
                  const gap = d.predicted_win_rate != null
                    ? d.predicted_win_rate - d.realised_win_rate
                    : null;
                  return (
                    <tr key={d.date} className="border-b border-gray-800/50">
                      <td className="py-2 pr-4 font-medium">{d.date}</td>
                      <td className="py-2 pr-4 text-blue-400">{pct(d.predicted_win_rate)}</td>
                      <td className="py-2 pr-4 text-emerald-400">{pct(d.realised_win_rate)}</td>
                      <td
                        className={clsx(
                          "py-2 pr-4",
                          gap == null
                            ? "text-gray-500"
                            : Math.abs(gap) > 0.15
                              ? "text-red-400"
                              : "text-gray-300",
                        )}
                      >
                        {gap == null ? "—" : pct(gap)}
                      </td>
                      <td className="py-2">{d.sample_size}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export function ModelDriftPage() {
  const [days, setDays] = useState(30);
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const { data, isLoading } = useModelDrift(days);

  if (isLoading) return <div className="h-96 animate-pulse bg-gray-900 rounded-lg" />;
  if (!data) return <p className="text-gray-500">No model drift data</p>;

  const models = data.model_versions;
  const activeType = selectedType
    ?? (models[0]?.model_type ?? null);
  const activeModel = models.find((m) => m.model_type === activeType) ?? null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-lg font-semibold">Model Drift</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            Predicted vs realised win rate — flags decay before live performance silently degrades.
          </p>
        </div>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
        >
          <option value={7}>7 days</option>
          <option value={30}>30 days</option>
          <option value={90}>90 days</option>
          <option value={180}>180 days</option>
        </select>
      </div>

      {data.warning && (
        <div className="bg-red-900/30 border border-red-700 rounded-lg p-4">
          <p className="text-sm font-semibold text-red-300">Drift detected</p>
          <p className="text-sm text-red-200 mt-1">{data.warning}</p>
        </div>
      )}

      {/* Signal-class distribution — same data drift-watch monitors for
          class collapse / dominance, surfaced inline so the user can
          see it before the daily Telegram alert fires. */}
      <SignalClassWidget days={7} />

      {models.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-center">
          <p className="text-gray-400 text-sm">
            No scored predictions in the selected window. Predictions are
            scored after their holding period elapses — check back once the
            predict-track skill has had time to run.
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 border-b border-gray-800">
            {models.map((m) => (
              <button
                key={m.model_type}
                onClick={() => setSelectedType(m.model_type)}
                className={clsx(
                  "px-4 py-2 text-sm font-medium border-b-2 transition-colors capitalize",
                  m.model_type === activeType
                    ? "border-blue-500 text-blue-400"
                    : "border-transparent text-gray-500 hover:text-gray-300",
                )}
              >
                {m.model_type} model
              </button>
            ))}
          </div>
          {activeModel && <ModelView model={activeModel} />}
        </>
      )}
    </div>
  );
}
